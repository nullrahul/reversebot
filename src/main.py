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

from config.config import load_config, BotConfig
from config.runtime import RuntimeConfig
from src.logs.log_config import setup_logging, get_logger
from src.db.queries import init_db, close_db, log_event, log_scan, get_performance_summary
from src.auth.l1_auth import L1Auth
from src.auth.l2_auth import L2Auth
from src.api.rate_limiter import RateLimiter
from src.api.gamma_client import GammaClient
from src.api.clob_client import ClobClient
from src.api.websocket import WebSocketClient
from src.api.binance_ws import BinanceWSClient
from src.strategy.scanner import MarketScanner
from src.strategy.validator import SignalValidator
from src.strategy.decision import DecisionEngine
from src.strategy.position_mgr import PositionManager
from src.db.rejections import RejectionLogger
from src.risk.kill_switch import KillSwitch
from src.risk.health import HealthMonitor
from src.risk.limits import RiskLimits
from src.risk.ghost_guard import GhostGuard
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
        self.ghost_guard = None
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
        
        if hasattr(self.decision, "binance_ws") and self.decision.binance_ws:
            await self.decision.binance_ws.stop()
        return True

    async def run(self):
        """The core trading loop moved from main()."""
        consecutive_errors = 0
        
        # Ensure secondary background tasks are up
        await self.ws_client.start()
        await self.health_monitor.start()
        await self.scanner.start_discovery()
        await self.position_mgr.start()
        if hasattr(self.decision, "binance_ws") and self.decision.binance_ws:
            await self.decision.binance_ws.start()
        if self.ghost_guard:
            await self.ghost_guard.start()

        mode = "PAPER" if self.config.paper_trade else "🔴 LIVE"
        logger.info("engine_started", mode=mode, message="Trading engine is now active")

        try:
            last_scan_time = 0.0
            last_resolution_check_time = 0.0
            last_position_monitor_time = 0.0
            last_regime_time = 0.0
            while not self.kill_switch.is_triggered:
                try:
                    cycle_start = time.time()

                    # Hot-reload runtime params from dashboard on every cycle
                    # RuntimeConfig.reload_from_disk() updates in-place —
                    # all sub-engines (scanner, decision, PM, validator) see changes instantly
                    self.config.reload_from_disk()

                    # ─── FAST LANE: MONITOR POSITIONS & SIGNALS ───
                    # Monitor active positions every 2.0 seconds to eliminate DB queries
                    if cycle_start - last_position_monitor_time >= 2.0:
                        last_position_monitor_time = cycle_start
                        await self.position_mgr.monitor_positions()
                    
                    signals = await self.scanner.get_entry_signals()
                    # A4: Position Manager Priority Logic - Sort YES signals before NO signals
                    signals = sorted(signals, key=lambda s: 0 if s.get("target_side") == "YES" else 1)
                    orders_placed = 0
                    for signal_market in signals:
                        if self.kill_switch.is_triggered: break
                        
                        # Skip if already evaluated in this scan cycle
                        market_id = signal_market.get("market_id")
                        if market_id:
                            # Flaw B Fix: Clear evaluated markets cache every 1.0 seconds instead of waiting for 5s REST scan count
                            now_time = time.time()
                            if not hasattr(self.decision, "_last_cache_clear_time"):
                                self.decision._last_cache_clear_time = now_time
                            if now_time - self.decision._last_cache_clear_time >= 1.0:
                                self.decision._evaluated_markets.clear()
                                self.decision._last_cache_clear_time = now_time
                            if market_id in self.decision._evaluated_markets:
                                continue
                            self.decision._evaluated_markets.add(market_id)

                        # Flaw A Fix: Wrap the entire signal evaluation in the global entry lock to serialize risk/exposure checks
                        from src.strategy.decision import _global_entry_lock
                        async with _global_entry_lock:
                            success, reason = await self.decision.process_signal(
                                signal_market,
                                target_token_id=signal_market.get("target_token_id"),
                                side=signal_market.get("target_side", "NO"),
                            )
                        if success: orders_placed += 1
                        else: logger.info("position_rejected", market_id=signal_market.get("market_id"), reason=reason)

                    # ─── SLOW LANE: MARKET DISCOVERY (THROTTLED) ───
                    # Uses REST API calls, limited to 90/min
                    discovery_interval = 5.0 # Seconds
                    if cycle_start - last_scan_time >= discovery_interval:
                        last_scan_time = cycle_start
                        
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

                        # Run scan cycle (Updates market list from Gamma)
                        scanned, flagged = await self.scanner.run_scan_cycle()
                        self.last_scan_duration_ms = int((time.time() - cycle_start) * 1000)

                        # Log scan cycle
                        await log_scan(
                            self.db,
                            markets_scanned=scanned,
                            markets_flagged=flagged,
                            signals_fired=len(signals),
                            orders_placed=orders_placed,
                            bot_version=self.config.bot_version,
                        )

                    # ─── RESOLUTION CHECK LANE (THROTTLED) ───
                    # Polled every 10 seconds to avoid API rate limits when positions are expiring
                    resolution_interval = 10.0 # Seconds
                    if cycle_start - last_resolution_check_time >= resolution_interval:
                        last_resolution_check_time = cycle_start
                        await self.position_mgr.check_resolutions()

                    # ─── MARKET REGIME SNAPSHOT LANE (60s) ───
                    regime_interval = 60.0 # Seconds
                    if cycle_start - last_regime_time >= regime_interval:
                        last_regime_time = cycle_start
                        try:
                            from src.db.queries import log_market_regime
                            binance_ws = self.decision.binance_ws
                            if binance_ws:
                                btc_price = binance_ws.get_last_price("BTC")
                                eth_price = binance_ws.get_last_price("ETH")
                                
                                cvd_60s_btc, cvd_5m_btc = binance_ws.get_cvd_metrics("BTC")
                                cvd_60s_eth, cvd_5m_eth = binance_ws.get_cvd_metrics("ETH")
                                
                                rolling_spread_median = 0.005 # neutral default
                                spread_multiplier = 1.0
                                iv_proxy = 0.0
                                label = "NORMAL"
                                session = "UNKNOWN"
                                
                                await log_market_regime(
                                    self.db,
                                    symbol="BTC",
                                    btc_price=btc_price,
                                    eth_price=eth_price,
                                    cvd_60s_btc=cvd_60s_btc,
                                    cvd_60s_eth=cvd_60s_eth,
                                    cvd_5m_btc=cvd_5m_btc,
                                    cvd_5m_eth=cvd_5m_eth,
                                    rolling_spread_median=rolling_spread_median,
                                    spread_multiplier=spread_multiplier,
                                    iv_proxy=iv_proxy,
                                    label=label,
                                    session=session
                                )
                                logger.info("market_regime_logged", btc_price=btc_price, eth_price=eth_price, cvd_btc_60s=cvd_60s_btc)
                        except Exception as reg_err:
                            logger.error("market_regime_logging_failed", error=str(reg_err))

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
                    # We target 0.1s monitoring speed regardless of poll_interval_seconds
                    # (poll_interval_seconds will now act as a fallback minimum)
                    wait = max(0, 0.1 - elapsed)
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
    clob_client.ws = ws_client
    binance_ws = BinanceWSClient()
    
    # ─── New: Init Rejection Logger ───
    rejection_logger = RejectionLogger(db_path="data/rejections_rd.db")
    await rejection_logger.init()

    # ─── Step 6: Init Strategy ───
    # Wrap config in RuntimeConfig so all sub-engines share a single
    # mutable reference. Dashboard hot-reloads propagate instantly.
    runtime_config = RuntimeConfig(config)
    
    scanner = MarketScanner(runtime_config, gamma_client, ws_client, clob_client, db)
    validator = SignalValidator(runtime_config, clob_client, db, rejection_logger=rejection_logger)
    position_mgr = PositionManager(runtime_config, clob_client, db, ws_client=ws_client, gamma_client=gamma_client)
    decision = DecisionEngine(runtime_config, validator, position_mgr, clob_client, db, rejection_logger=rejection_logger, scanner=scanner, binance_ws=binance_ws)

    # ─── Step 7: Init Risk ───
    kill_switch = KillSwitch(clob_client, ws_client, db, config.bot_version)
    health_monitor = HealthMonitor(ws_client, kill_switch, runtime_config)
    risk_limits = RiskLimits(runtime_config, db)
    
    ghost_guard = None
    if config.ghost_guard_enabled:
        ghost_guard = GhostGuard(config.polygon_rpc, l2_address, kill_switch)

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
        config=runtime_config,
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
    engine.ghost_guard = ghost_guard

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
            # Link TG to PositionManager for real-time notifications
            position_mgr.tg = tg_client
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
        message=f"Bot is LIVE — WebSocket-driven 1s loop | Dashboard: :{dashboard_port}",
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
        await position_mgr.stop()
        if binance_ws:
            await binance_ws.stop()
        if ghost_guard:
            await ghost_guard.stop()
        
        if ws_client:
            await ws_client.disconnect()
            logger.info("ws_closed")
            
        await gamma_client.close()
        await clob_client.close()
        
        if db:
            from src.db.queries import close_db
            await close_db(db)
            logger.info("db_closed")

        if rejection_logger:
            await rejection_logger.close()
            logger.info("rejection_db_closed")

        logger.info(
            "bot_stopped",
            message="═══ Polymarket Reverse Bot stopped ═══",
        )
        
        # Force terminate process to prevent hung event loops
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
