"""
Main Entry Point — Polymarket Reverse (Yes Bias) Bot.

Boot sequence:
1. Load config → validate env
2. Init database → apply migrations
3. Setup logging
4. Init auth (L1 → L2, sync server time)
5. Init API clients (Gamma, CLOB, WebSocket)
6. Init strategy components (scanner, validator, decision, position manager)
7. Init risk components (kill switch, health monitor, limits)
8. Start WebSocket listener
9. Start scanner loop
10. Start position monitor loop

CLI flags:
  --kill     Manual kill switch — cancels all orders and exits
  --paper    Force paper trade mode (overrides .env)
  --version  Print bot version and exit
"""

import asyncio
import argparse
import signal
import sys
import os
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.config import load_config, BotConfig, load_runtime_overrides
from src.logs.log_config import setup_logging, get_logger
from src.db.queries import init_db, close_db, log_event, log_scan, get_performance_summary
from src.auth.l1_auth import L1Auth
from src.auth.l2_auth import L2Auth
from src.api.rate_limiter import RateLimiter
from src.api.gamma_client import GammaClient
from src.api.clob_client import ClobClient
from src.api.websocket import WebSocketClient
from src.strategy.scanner import MarketScanner
from src.strategy.validator import SignalValidator
from src.strategy.decision import DecisionEngine
from src.strategy.position_mgr import PositionManager
from src.risk.kill_switch import KillSwitch
from src.risk.health import HealthMonitor
from src.risk.limits import RiskLimits
from src.api.telegram_client import TelegramClient
from src.api.dashboard import start_dashboard_server, set_engine as set_dashboard_engine
from dotenv import load_dotenv


logger = None  # Initialized after setup_logging


def parse_args():
    parser = argparse.ArgumentParser(
        description="Polymarket Reverse (Yes Bias) Bot"
    )
    parser.add_argument(
        "--kill", action="store_true",
        help="Trigger kill switch — cancel all orders and exit"
    )
    parser.add_argument(
        "--paper", action="store_true",
        help="Force paper trade mode (overrides .env)"
    )
    parser.add_argument(
        "--version", action="store_true",
        help="Print bot version and exit"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print performance statistics and exit"
    )
    parser.add_argument(
        "--env", type=str, default=None,
        help="Path to custom .env file"
    )
    return parser.parse_args()


class TradingEngine:
    """
    Encapsulates the trading loop logic to allow for remote management.
    """
    def __init__(self, config, db, scanner, validator, decision, position_mgr, kill_switch, health_monitor, risk_limits, l2_auth, gamma_client, clob_client, ws_client):
        self.config = config
        self.db = db
        self.scanner = scanner
        self.validator = validator
        self.decision = decision
        self.position_mgr = position_mgr
        self.kill_switch = kill_switch
        self.health_monitor = health_monitor
        self.risk_limits = risk_limits
        self.l2_auth = l2_auth
        self.gamma_client = gamma_client
        self.clob_client = clob_client
        self.ws_client = ws_client
        self._task = None

    async def start(self) -> bool:
        if self._task and not self._task.done():
            return False
        
        # Reset kill switch flags to allow restart
        self.kill_switch.is_active.clear()
        
        self._task = asyncio.create_task(self.run())
        return True

    async def stop(self) -> bool:
        if not self._task or self._task.done():
            return False
        
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        return True

    async def run(self):
        """The core trading loop moved from main()."""
        consecutive_errors = 0
        
        # Ensure secondary background tasks are up
        await self.ws_client.start()
        await self.health_monitor.start()
        await self.scanner.start_discovery()

        mode = "PAPER" if self.config.paper_trade else "🔴 LIVE"
        logger.info("engine_started", mode=mode, message="Trading engine is now active")

        try:
            while not self.kill_switch.is_triggered:
                try:
                    cycle_start = time.time()

                    # Hot-reload runtime params from dashboard on every cycle
                    self.config = load_runtime_overrides(self.config)

                    # Check risk limits before scanning
                    risk_ok, risk_reason = await self.risk_limits.check_all(
                        paper=self.config.paper_trade
                    )
                    if not risk_ok:
                        should_kill, kill_reason = await self.risk_limits.should_kill_switch(
                            paper=self.config.paper_trade
                        )
                        if should_kill:
                            await self.kill_switch.trigger("DAILY_LOSS_LIMIT", {"reason": kill_reason})
                            break

                    # Run scan cycle
                    scanned, flagged, signals = await self.scanner.run_scan_cycle()

                    # Process each confirmed signal
                    orders_placed = 0
                    for signal_market in signals:
                        if self.kill_switch.is_triggered:
                            break

                        success, reason = await self.decision.process_signal(
                            signal_market,
                            no_token_id=signal_market.get("no_token_id"),
                        )
                        if success:
                            orders_placed += 1
                        else:
                            logger.info("position_rejected", market_id=signal_market.get("market_id"), reason=reason)

                    # Log scan cycle
                    await log_scan(
                        self.db,
                        markets_scanned=scanned,
                        markets_flagged=flagged,
                        signals_fired=len(signals),
                        orders_placed=orders_placed,
                        bot_version=self.config.bot_version,
                    )

                    # Monitor existing positions
                    await self.position_mgr.monitor_positions()

                    # Check auth kill trigger
                    if self.l2_auth.should_kill:
                        await self.kill_switch.trigger(
                            "AUTH_401_THRESHOLD",
                            {"consecutive_401s": self.l2_auth.consecutive_401s},
                        )
                        break

                    # Reset error counter on successful cycle
                    consecutive_errors = 0
                    self.kill_switch.reset_api_errors()

                    # Wait for next poll
                    elapsed = time.time() - cycle_start
                    wait = max(0, self.config.poll_interval_seconds - elapsed)
                    if wait > 0 and not self.kill_switch.is_triggered:
                        await asyncio.sleep(wait)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(
                        "main_loop_error",
                        error=str(e),
                        error_type=type(e).__name__,
                        consecutive_errors=consecutive_errors,
                        message=f"Main loop error #{consecutive_errors}: {e}",
                    )

                    # Track in kill switch
                    should_kill = self.kill_switch.record_api_error()
                    if should_kill:
                        await self.kill_switch.trigger(
                            "CONSECUTIVE_API_ERRORS",
                            {"consecutive_errors": consecutive_errors, "last_error": str(e)},
                        )
                        break

                    # Brief pause before retry
                    await asyncio.sleep(5.0)

        except asyncio.CancelledError:
            logger.info("engine_loop_cancelled")
        except Exception as e:
            logger.critical("engine_unhandled_exception", error=str(e))
            await self.kill_switch.trigger("UNHANDLED_EXCEPTION", {"error": str(e)})
        finally:
            logger.info("engine_cleanup")
            # We don't close all connections here because the Telegram bot might stay alive
            # Cleanup is handled in main()'s finally block or triggered by kill switch


async def main():
    global logger

    args = parse_args()

    # ─── Step 1: Load Config ───
    try:
        config = load_config(env_path=args.env)
    except (ValueError, SystemExit) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.version:
        print(f"Polymarket Reverse Bot {config.bot_version}")
        sys.exit(0)

    if args.paper:
        # Override paper_trade in config
        object.__setattr__(config, 'paper_trade', True)


    # ─── Step 2: Setup Logging ───
    setup_logging(
        log_dir=config.log_dir,
        log_level=config.log_level,
        bot_version=config.bot_version,
    )
    logger = get_logger("main", config.bot_version)

    mode = "PAPER" if config.paper_trade else "🔴 LIVE"
    logger.info(
        "bot_starting",
        version=config.bot_version,
        mode=mode,
        message=f"═══ Polymarket Reverse Bot {config.bot_version} starting in {mode} mode ═══",
    )

    # ─── Step 3: Init Database ───
    db = await init_db(config.db_path)
    if args.stats:
        stats = await get_performance_summary(db, paper=config.paper_trade)
        
        mode_str = "PAPER TRADE" if stats['is_paper'] else "LIVE TRADING"
        print(f"\n{'='*50}")
        print(f" REVERSE BOT PERFORMANCE SUMMARY ({mode_str})")
        print(f"{'='*50}\n")
        
        print(f"  Total Trades (Closed):  {stats['total_closed']}")
        print(f"  Wins:                   {stats['wins']}")
        print(f"  Losses:                 {stats['losses']}")
        print(f"  Win Rate:               {stats['win_rate']}%")
        print(f"  Total Realised P&L:     ${stats['total_realised_pnl']:.4f}")
        print(f"  Avg P&L per Trade:      ${(stats['total_realised_pnl'] / stats['total_closed']):.4f}" if stats['total_closed'] > 0 else "  Avg P&L per Trade:      $0.0000")
        print("-" * 50)
        print(f"  Current Open Positions: {stats['open_count']}")
        print(f"  Total Open Exposure:    ${stats['open_exposure']:.2f}")
        print(f"\n{'='*50}\n")

    # ─── Step 4: Init Auth ───
    l1_auth = L1Auth(config.private_key, config.chain_id)
    l2_address = config.poly_address if config.poly_address else l1_auth.address
    l2_auth = L2Auth(
        api_key=config.poly_api_key,
        api_secret=config.poly_api_secret,
        passphrase=config.poly_passphrase,
        address=l2_address,
        clob_api_url=config.clob_api_url,
    )

    # Sync server time (clock drift guard)
    await l2_auth.sync_server_time()

    # ─── Step 5: Init API Clients ───
    rate_limiter = RateLimiter(
        orders_per_min=config.rate_limit_orders_per_min,
        api_calls_per_min=config.rate_limit_api_calls_per_min,
    )

    gamma_client = GammaClient(
        gamma_api_url=config.gamma_api_url,
        rate_limiter=rate_limiter,
    )

    clob_client = ClobClient(
        auth=l2_auth,
        clob_api_url=config.clob_api_url,
        rate_limiter=rate_limiter,
        paper_trade=config.paper_trade,
    )

    ws_client = WebSocketClient()

    # ─── Step 6: Init Strategy ───
    scanner = MarketScanner(config, gamma_client, ws_client, db)
    validator = SignalValidator(config, clob_client, db)
    position_mgr = PositionManager(config, clob_client, db, ws_client=ws_client)
    decision = DecisionEngine(config, validator, position_mgr, clob_client, db)

    # ─── Step 7: Init Risk ───
    kill_switch = KillSwitch(clob_client, ws_client, db, config.bot_version)
    health_monitor = HealthMonitor(ws_client, kill_switch, config)
    risk_limits = RiskLimits(config, db)

    # ─── Handle --kill flag ───
    if args.kill:
        logger.critical("manual_kill", message="Manual kill switch triggered via CLI")
        await kill_switch.trigger("MANUAL_CLI", {"triggered_by": "python main.py --kill"})
        sys.exit(0)

    # ─── Step 8: Setup Signal Handlers ───
    loop = asyncio.get_running_loop()

    def signal_handler(sig):
        logger.critical(
            "signal_received",
            signal=sig.name,
            message=f"Signal {sig.name} received — initiating shutdown",
        )
        # Fire-and-forget task; monitored by main loop via is_active event
        asyncio.create_task(kill_switch.trigger(
            f"SIGNAL_{sig.name}",
            {"signal": sig.name},
        ))

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))

    # ─── Initialize Engine ───
    engine = TradingEngine(
        config=config,
        db=db,
        scanner=scanner,
        validator=validator,
        decision=decision,
        position_mgr=position_mgr,
        kill_switch=kill_switch,
        health_monitor=health_monitor,
        risk_limits=risk_limits,
        l2_auth=l2_auth,
        gamma_client=gamma_client,
        clob_client=clob_client,
        ws_client=ws_client
    )

    # ─── Step 9: Initialize Telegram ───
    if os.getenv("DISABLE_TELEGRAM", "false").lower() == "true":
        logger.info("telegram_disabled", message="Telegram control disabled by config")
    else:
        telegram_env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "telegram", ".env")
        load_dotenv(telegram_env_path)
        tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
        tg_chat_id = os.getenv("TELEGRAM_CHAT_ID")

        if tg_token and tg_chat_id:
            tg_client = TelegramClient(
                token=tg_token,
                chat_id=tg_chat_id,
                db=db,
                position_mgr=position_mgr,
                ws_client=ws_client,
                kill_switch=kill_switch,
                config=config,
                start_engine_callback=engine.start,
                stop_engine_callback=engine.stop
            )
            await tg_client.start()
            logger.info("telegram_bot_integrated", message="Telegram control enabled")
        else:
            logger.warning("telegram_config_missing", message="Telegram token or chat_id not found in telegram/.env")
# Start the engine automatically initially
    # Start the engine automatically initially
    await engine.start()

    # ─── Step 10: Start Web Dashboard ───
    from src.api.dashboard import start_dashboard_server, set_engine as set_dashboard_engine
    set_dashboard_engine(engine, kill_switch, config)

    dashboard_port = int(os.getenv("DASHBOARD_PORT", 8080))
    asyncio.create_task(start_dashboard_server(port=dashboard_port))
    logger.info(
        "dashboard_started",
        port=dashboard_port,
        message=f"Web dashboard available at http://0.0.0.0:{dashboard_port}",
    )

    logger.info(
        "bot_running",
        message="══════════════════════════════════════════",
    )
    logger.info(
        "bot_running",
        mode=mode,
        poll_interval=config.poll_interval_seconds,
        message=f"Bot is LIVE — scanning every {config.poll_interval_seconds}s | Dashboard: :{dashboard_port}",
    )

    # ─── Main Execution ───
    try:
        # Wait until the kill switch is triggered (via signal or logic error)
        # This replaces the blocking while True loop
        await kill_switch.is_active.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("shutdown_received_via_exception")
    finally:
        # ═══════════════════════════════════════════════════════════
        # FINAL CLEANUP SEQUENCE
        # ═══════════════════════════════════════════════════════════
        logger.info("initiating_final_shutdown_sequence")
        
        # 1. Stop the engine tasks
        await engine.stop()
        
        # 2. Fully await the kill switch trigger sequence
        # If it was already triggered (signal/error), this will await its completion.
        await kill_switch.trigger("SHUTDOWN", {"reason": "main_exit"})

        # 3. Shutdown other background services
        await health_monitor.stop()
        await scanner.stop_discovery()
        
        if ws_client:
            await ws_client.disconnect()
            logger.info("ws_closed")
            
        await gamma_client.close()
        await clob_client.close()
        
        if db:
            from src.db.queries import close_db
            await close_db(db)
            logger.info("db_closed")

        logger.info(
            "bot_stopped",
            message="═══ Polymarket Reverse Bot stopped ═══",
        )
        
        # Force terminate process to prevent hung event loops
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
