import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
import structlog

from src.logs.log_config import get_logger

logger = get_logger("telegram")

class TelegramClient:
    """
    Telegram Bot client for remote control and notifications.
    Uses aiogram 3.x for async interaction.
    """
    def __init__(
        self,
        token: str,
        chat_id: str,
        db,
        position_mgr,
        ws_client,
        kill_switch,
        config,
        start_engine_callback,
        stop_engine_callback
    ):
        self.token = token
        self.chat_id = chat_id
        self.db = db
        self.pm = position_mgr
        self.ws = ws_client
        self.ks = kill_switch
        self.config = config
        self.start_engine = start_engine_callback
        self.stop_engine = stop_engine_callback
        
        self.bot = Bot(token=token)
        self.dp = Dispatcher()
        self._is_running = False
        self._report_task = None
        
        self._register_handlers()

    def _register_handlers(self):
        @self.dp.message(Command("start", "menu"))
        async def cmd_start(message: types.Message):
            if str(message.chat.id) != str(self.chat_id):
                return
            await self.send_menu("🎮 Reverse Bot Control Panel")

        @self.dp.message(F.text == "📊 Status")
        async def btn_status(message: types.Message):
            if str(message.chat.id) != str(self.chat_id): return
            status_msg = await self._get_status_report()
            await message.answer(status_msg, parse_mode="HTML")

        @self.dp.message(F.text == "📈 Performance")
        async def btn_perf(message: types.Message):
            if str(message.chat.id) != str(self.chat_id): return
            perf_msg = await self._get_perf_report()
            await message.answer(perf_msg, parse_mode="HTML")

        @self.dp.message(F.text == "⚡️ Positions")
        async def btn_positions(message: types.Message):
            if str(message.chat.id) != str(self.chat_id): return
            pos_msg = await self._get_positions_report()
            await message.answer(pos_msg, parse_mode="HTML")

        @self.dp.message(F.text == "🛑 STOP ENGINE")
        async def btn_stop(message: types.Message):
            if str(message.chat.id) != str(self.chat_id): return
            success = await self.stop_engine()
            if success:
                await message.answer("🛑 <b>Trading Engine STOPPED.</b>", parse_mode="HTML")
            else:
                await message.answer("⚠️ Engine already stopped or failed to stop.")

        @self.dp.message(F.text == "🚀 START ENGINE")
        async def btn_start(message: types.Message):
            if str(message.chat.id) != str(self.chat_id): return
            success = await self.start_engine()
            if success:
                await message.answer("🚀 <b>Trading Engine STARTED.</b>", parse_mode="HTML")
            else:
                await message.answer("⚠️ Engine already running or failed to start.")

        @self.dp.message(F.text == "💀 KILL SWITCH")
        async def btn_kill(message: types.Message):
            if str(message.chat.id) != str(self.chat_id): return
            await self.ks.trigger("TELEGRAM_MANUAL", {"user": message.from_user.username if message.from_user else "unknown"})
            await message.answer("💀 <b>KILL SWITCH TRIGGERED.</b> Orders cancelled. Bot exiting.", parse_mode="HTML")

    async def send_menu(self, text: str):
        builder = ReplyKeyboardBuilder()
        builder.add(KeyboardButton(text="📊 Status"))
        builder.add(KeyboardButton(text="📈 Performance"))
        builder.add(KeyboardButton(text="⚡️ Positions"))
        builder.add(KeyboardButton(text="🚀 START ENGINE"))
        builder.add(KeyboardButton(text="🛑 STOP ENGINE"))
        builder.add(KeyboardButton(text="💀 KILL SWITCH"))
        builder.adjust(2, 1, 2, 1)
        
        await self.bot.send_message(
            self.chat_id, 
            text, 
            reply_markup=builder.as_markup(resize_keyboard=True), 
            parse_mode="HTML"
        )

    async def _get_status_report(self) -> str:
        engine_status = "RUNNING 🟢" if self.pm._paper or True else "STOPPED 🔴" # Simplified
        ws_status = "CONNECTED 🟢" if self.ws.is_connected else "DISCONNECTED 🔴"
        ks_status = "ARMED ✅" if not self.ks.is_triggered else "TRIGGERED 🛑"
        
        msg = (
            f"<b>🤖 Bot Status Report</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Engine:</b> {engine_status}\n"
            f"🌐 <b>WebSocket:</b> {ws_status}\n"
            f"🛡 <b>Kill Switch:</b> {ks_status}\n"
            f"🕒 <b>Server Time:</b> {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC\n"
            f"📦 <b>Version:</b> {self.config.bot_version}\n"
        )
        return msg

    async def _get_perf_report(self) -> str:
        from src.db.queries import get_performance_summary
        stats = await get_performance_summary(self.db, paper=self.config.paper_trade)
        
        mode = "PAPER" if stats['is_paper'] else "LIVE"
        msg = (
            f"<b>📈 Performance ({mode})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Total P&L:</b> ${stats['total_realised_pnl']:.4f}\n"
            f"✅ <b>Wins:</b> {stats['wins']}\n"
            f"❌ <b>Losses:</b> {stats['losses']}\n"
            f"🎯 <b>Win Rate:</b> {stats['win_rate']}%\n"
            f"📊 <b>Trades:</b> {stats['total_closed']}\n"
        )
        return msg

    async def _get_positions_report(self) -> str:
        from src.db.queries import get_active_positions
        positions = await get_active_positions(self.db, paper=self.config.paper_trade)
        
        if not positions:
            return "<b>⚡️ No active positions.</b>"
            
        msg = "<b>⚡️ Active Positions</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        for p in positions:
            p_id = p['position_id'][:8]
            pnl = p.get('unrealised_pnl', 0.0)
            msg += f"• <code>{p_id}</code>: {p['market_id']} | <b>${pnl:+.2f}</b>\n"
            
        return msg

    def _escape_html(self, text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    async def _scheduled_report_loop(self):
        """Send a summary every 4 hours."""
        while self._is_running:
            try:
                # Wait until next 4-hour mark (00, 04, 08, 12, 16, 20)
                now = datetime.now(timezone.utc)
                next_hour = (now.hour // 4 + 1) * 4
                if next_hour >= 24:
                    wait_hours = 24 - now.hour
                else:
                    wait_hours = next_hour - now.hour
                
                # Periodic summary
                perf = await self._get_perf_report()
                await self.bot.send_message(self.chat_id, f"<b>🕒 Scheduled Report</b>\n{perf}", parse_mode="HTML")
                
                await asyncio.sleep(4 * 3600) # 4 hours
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("telegram_scheduler_error", error=str(e))
                await asyncio.sleep(60)

    async def notify_trade_entry(
        self,
        market_name: str,
        entry_price: float,
        size_usdc: float,
        shares: float,
        position_id: str,
        paper: bool = False
    ):
        """Send a verbose notification for a new trade entry."""
        mode = "PAPER" if paper else "LIVE"
        msg = (
            f"🚀 <b>New Trade Opened ({mode})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>Market:</b> {self._escape_html(market_name)}\n"
            f"🆔 <b>Position ID:</b> <code>{position_id}</code>\n"
            f"💰 <b>Size:</b> <code>${size_usdc:.2f}</code>\n"
            f"🎫 <b>Shares:</b> <code>{shares:.2f}</code>\n"
            f"🏷 <b>Entry Price:</b> <code>${entry_price:.4f}</code>\n"
            f"📊 <b>Side:</b> <code>NO (Yes Bias Reverse)</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        try:
            await self.bot.send_message(self.chat_id, msg, parse_mode="HTML")
        except Exception as e:
            logger.error("telegram_notify_entry_error", error=str(e))

    async def notify_trade_exit(
        self,
        market_name: str,
        exit_price: float,
        realised_pnl: float,
        reason: str,
        position_id: str,
        paper: bool = False
    ):
        """Send a verbose notification for a trade exit."""
        mode = "PAPER" if paper else "LIVE"
        emoji = "📈" if realised_pnl >= 0 else "📉"
        
        msg = (
            f"{emoji} <b>Trade Closed ({mode})</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 <b>Market:</b> {self._escape_html(market_name)}\n"
            f"🆔 <b>Position ID:</b> <code>{position_id}</code>\n"
            f"🏁 <b>Exit Price:</b> <code>${exit_price:.4f}</code>\n"
            f"💰 <b>Realised P&L:</b> <b>${realised_pnl:+.4f}</b>\n"
            f"🛑 <b>Reason:</b> <code>{reason}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🕒 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        try:
            await self.bot.send_message(self.chat_id, msg, parse_mode="HTML")
        except Exception as e:
            logger.error("telegram_notify_exit_error", error=str(e))

    async def start(self):
        self._is_running = True
        self._report_task = asyncio.create_task(self._scheduled_report_loop())
        logger.info("telegram_bot_starting", chat_id=self.chat_id)
        # We don't block here, just start polling
        asyncio.create_task(self.dp.start_polling(self.bot))
        await self.send_menu("🤖 Bot Online. Control dashboard activated.")

    async def stop(self):
        self._is_running = False
        if self._report_task:
            self._report_task.cancel()
        await self.bot.session.close()
        logger.info("telegram_bot_stopped")
