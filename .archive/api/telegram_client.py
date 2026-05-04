"""
Telegram Client — Remote Control & Reporting for Polymarket Reverse Bot.
Uses aiogram v3 for async interaction.
Features:
1. Dynamic UI: Single-message dashboard using edit_text
2. Remote Start/Stop of the trading engine
3. Real-time Position Reporting with auto-refresh (1 min)
4. 6-hourly automated performance reports
5. Restricted access (Only responds to configured CHAT_ID)
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Callable

from aiogram import Bot, Dispatcher, types, F, exceptions
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from src.logs.log_config import get_logger

logger = get_logger("telegram")

class TelegramClient:
    """
    Manages the Telegram bot interface with a dynamic dashboard.
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
        start_engine_callback: Callable,
        stop_engine_callback: Callable,
    ):
        self.bot = Bot(token=token)
        self.dp = Dispatcher()
        self.chat_id = int(chat_id)
        self.db = db
        self.position_mgr = position_mgr
        self.ws = ws_client
        self.kill_switch = kill_switch
        self.config = config
        self.start_engine = start_engine_callback
        self.stop_engine = stop_engine_callback
        
        self._report_task = None
        self._refresh_task = None
        self._is_running = False

        # Register handlers
        self._register_handlers()

    def _register_handlers(self):
        """Register all message and callback handlers."""

        @self.dp.message(Command("start"))
        async def cmd_start(message: types.Message):
            if message.chat.id != self.chat_id:
                return
            await self._stop_refresh()
            await self.send_menu("Welcome to ReverseBot Manual Control. Select an action:")

        @self.dp.callback_query(F.data == "menu_main")
        async def cb_main(callback: types.CallbackQuery):
            if callback.message.chat.id != self.chat_id:
                return
            await self._stop_refresh()
            await callback.answer()
            try:
                await callback.message.edit_text(
                    "Bot Dashboard. Select an action:",
                    reply_markup=self.get_main_keyboard()
                )
            except exceptions.TelegramBadRequest:
                pass # Already on main menu

        @self.dp.callback_query(F.data == "menu_start")
        async def cb_start(callback: types.CallbackQuery):
            if callback.message.chat.id != self.chat_id:
                return
            await callback.answer("Starting engine...")
            success = await self.start_engine()
            status = "✅ Engine Started" if success else "❌ Start Failed - Check Logs"
            try:
                await callback.message.edit_text(
                    f"{status}\n\nSelect an action:",
                    reply_markup=self.get_main_keyboard()
                )
            except exceptions.TelegramBadRequest:
                pass

        @self.dp.callback_query(F.data == "menu_stop")
        async def cb_stop(callback: types.CallbackQuery):
            if callback.message.chat.id != self.chat_id:
                return
            await callback.answer("Stopping engine & closing positions...")
            await self.stop_engine()
            await self.kill_switch.trigger("REMOTE_STOP", {"triggered_by": "Telegram"})
            closed_count = await self.position_mgr.close_all(reason="REMOTE_STOP")
            
            try:
                await callback.message.edit_text(
                    f"🛑 Engine Stopped. Closed {closed_count} positions.\n\nSelect an action:",
                    reply_markup=self.get_main_keyboard()
                )
            except exceptions.TelegramBadRequest:
                pass

        @self.dp.callback_query(F.data == "menu_positions")
        async def cb_positions(callback: types.CallbackQuery):
            if callback.message.chat.id != self.chat_id:
                return
            await callback.answer()
            # Start auto-refresh task if not already running
            await self._start_refresh(callback.message)

        @self.dp.callback_query(F.data == "menu_summary")
        async def cb_summary(callback: types.CallbackQuery):
            if callback.message.chat.id != self.chat_id:
                return
            await callback.answer()
            await self._stop_refresh()
            report = await self.generate_summary_report()
            try:
                await callback.message.edit_text(
                    report,
                    parse_mode="HTML",
                    reply_markup=self.get_back_keyboard()
                )
            except exceptions.TelegramBadRequest:
                pass

    def get_main_keyboard(self):
        builder = InlineKeyboardBuilder()
        builder.button(text="🟢 Start Bot", callback_data="menu_start")
        builder.button(text="🔴 Stop Bot", callback_data="menu_stop")
        builder.button(text="📋 Positions", callback_data="menu_positions")
        builder.button(text="📊 Summary", callback_data="menu_summary")
        builder.adjust(2)
        return builder.as_markup()

    def get_back_keyboard(self):
        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Back to Menu", callback_data="menu_main")
        return builder.as_markup()

    async def send_menu(self, text: str):
        await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
            reply_markup=self.get_main_keyboard()
        )

    async def _start_refresh(self, message: types.Message):
        """Start or restart the auto-refresh loop for positions."""
        if self._refresh_task:
            self._refresh_task.cancel()
        
        self._refresh_task = asyncio.create_task(self._refresh_loop(message))

    async def _stop_refresh(self):
        """Stop the auto-refresh loop."""
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None

    async def _refresh_loop(self, message: types.Message):
        """Edit the positions message every 60 seconds."""
        while True:
            try:
                report = await self.generate_positions_report()
                try:
                    await message.edit_text(
                        report,
                        parse_mode="HTML",
                        reply_markup=self.get_back_keyboard()
                    )
                except exceptions.TelegramBadRequest as e:
                    # Message might be identical or deleted
                    if "message is not modified" not in str(e).lower():
                        logger.debug("tg_refresh_error", error=str(e))
                
                # Auto-update every 60 seconds
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("tg_refresh_task_error", error=str(e))
                await asyncio.sleep(5)

    def _escape_html(self, text: str) -> str:
        """Escape HTML-sensitive characters in dynamic text."""
        if not text:
            return ""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    async def generate_positions_report(self) -> str:
        from src.db.queries import get_open_positions, get_market
        
        positions = await get_open_positions(self.db, paper=self.config.paper_trade)
        if not positions:
            return "ℹ️ <b>No open positions running.</b>\nClick 'Back' to return to menu."

        mode = "PAPER" if self.config.paper_trade else "LIVE"
        lines = [f"📋 <b>Active Positions ({mode})</b>", ""]
        
        total_value = 0.0
        total_pnl = 0.0
        total_cost = 0.0

        for pos in positions:
            market_id = pos["market_id"]
            market = await get_market(self.db, market_id)
            market_name = market.get("question", "Unknown Market") if market else market_id
            market_name = self._escape_html(market_name)
            
            closes_at = market.get("closes_at", 0) if market else 0
            time_left_sec = closes_at - int(time.time())
            if time_left_sec > 0:
                days = time_left_sec // 86400
                hours = (time_left_sec % 86400) // 3600
                time_left_str = f"{days}d {hours}h left"
            else:
                time_left_str = "Resolved/Closing"

            entry_price = pos["entry_price"]
            shares = pos["shares_bought"]
            size_usdc = pos["size_usdc"]
            total_cost += size_usdc
            
            current_price = self.ws.price_cache.get(market_id)
            if current_price is None:
                current_price_str = "⌛️ Waiting for WS..."
                pnl_line = "   └ P&L: <code>---</code>"
            else:
                current_val = current_price * shares
                total_value += current_val
                pnl = current_val - size_usdc
                total_pnl += pnl
                pnl_pct = (pnl / size_usdc) * 100
                emoji = "📈" if pnl >= 0 else "📉"
                current_price_str = f"${current_price:.4f}"
                pnl_line = f"   └ P&L: <b>{emoji} ${pnl:+.2f} ({pnl_pct:+.1f}%)</b>"

            lines.append(f"📍 <b>{market_name[:50]}...</b>")
            lines.append(f"   ├ Shares: <code>{shares:.2f}</code> (@ <code>${entry_price:.4f}</code>)")
            lines.append(f"   ├ Current: <code>{current_price_str}</code>")
            lines.append(pnl_line)
            lines.append(f"   └ Time Left: <code>{time_left_str}</code>")
            lines.append("")

        if total_cost > 0:
            lines.append("━━━━━━━━━━━━━━━━━━━━")
            lines.append(f"💰 <b>Total Managed Value:</b> <code>${total_value:.2f}</code>")
            lines.append(f"💵 <b>Total Entry Cost:</b> <code>${total_cost:.2f}</code>")
            total_pnl_pct = (total_pnl / total_cost) * 100
            emoji = "🚀" if total_pnl >= 0 else "🛑"
            lines.append(f"{emoji} <b>Floating P&L:</b> <code>${total_pnl:+.2f} ({total_pnl_pct:+.1f}%)</code>")
            lines.append("━━━━━━━━━━━━━━━━━━━━")

        lines.append(f"\n<i>Last update: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</i>")
        return "\n".join(lines)

    async def generate_summary_report(self) -> str:
        from src.db.queries import get_performance_summary
        
        stats = await get_performance_summary(self.db, paper=self.config.paper_trade)
        mode = "PAPER" if stats['is_paper'] else "LIVE"
        
        lines = [
            f"📊 <b>Bot Summary ({mode})</b>",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"✅ Trades: <code>{stats['total_closed']}</code>",
            f"🏆 Win Rate: <code>{stats['win_rate']}%</code>",
            f"💰 Realised P&L: <code>${stats['total_realised_pnl']:+.4f}</code>",
            f"📂 Open Exposure: <code>${stats['open_exposure']:.2f}</code> ({stats['open_count']} pos)",
            f"━━━━━━━━━━━━━━━━━━━━",
            f"🕒 Updated: <code>{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC</code>"
        ]
        return "\n".join(lines)

    async def _scheduled_report_loop(self):
        while self._is_running:
            try:
                await asyncio.sleep(21600)
                report = await self.generate_summary_report()
                await self.bot.send_message(self.chat_id, "🔔 <b>Scheduled 6-Hour Report</b>\n" + report, parse_mode="HTML")
            except Exception as e:
                logger.error("telegram_scheduler_error", error=str(e))
                await asyncio.sleep(60)

    async def start(self):
        self._is_running = True
        self._report_task = asyncio.create_task(self._scheduled_report_loop())
        logger.info("telegram_bot_starting", chat_id=self.chat_id)
        asyncio.create_task(self.dp.start_polling(self.bot))
        await self.send_menu("🤖 Bot Online. Control dashboard activated.")

    async def stop(self):
        self._is_running = False
        await self._stop_refresh()
        if self._report_task:
            self._report_task.cancel()
        await self.bot.session.close()
        logger.info("telegram_bot_stopped")
