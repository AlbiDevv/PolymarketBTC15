from __future__ import annotations

from datetime import datetime

import httpx
from loguru import logger

from .telegram_templates_ru import (
    notify_api_error,
    notify_cycle_failed,
    notify_cycle_recovered,
    notify_daily_report as tpl_daily,
    notify_drawdown_stop,
    notify_loss_limit,
    notify_position_exit,
    notify_settlement as tpl_settlement,
    notify_shutdown as tpl_shutdown,
    notify_startup as tpl_startup,
    notify_trade_opened,
)


class TelegramAlerter:
    """
    Алерты в Telegram (HTML). Язык интерфейса — русский.
    """

    BASE_URL = "https://api.telegram.org/bot{token}"

    def __init__(self, bot_token: str, chat_id: str, enabled: bool = True):
        self._token = bot_token
        self._chat_id = chat_id
        self._enabled = enabled and bool(bot_token) and bool(chat_id)
        self._http = httpx.AsyncClient(timeout=10.0) if self._enabled else None

    async def close(self):
        if self._http:
            await self._http.aclose()

    async def send_message(self, text: str, parse_mode: str | None = "HTML"):
        if not self._enabled:
            return
        try:
            url = f"{self.BASE_URL.format(token=self._token)}/sendMessage"
            body: dict = {"chat_id": self._chat_id, "text": text[:4000]}
            if parse_mode:
                body["parse_mode"] = parse_mode
            await self._http.post(url, json=body)
        except Exception as e:
            logger.error(f"Telegram send failed: {e}")

    async def send_plain(self, text: str):
        await self.send_message(text, parse_mode=None)

    async def notify_startup(
        self, mode: str, bankroll: float, hypotheses: list[str], cycle_sec: int,
    ):
        await self.send_message(tpl_startup(mode, bankroll, hypotheses, cycle_sec))

    async def notify_shutdown(self, mode: str, cycles: int, bankroll: float):
        await self.send_message(tpl_shutdown(mode, cycles, bankroll))

    async def notify_cycle_failure(self, err: str):
        await self.send_message(notify_cycle_failed(err))

    async def notify_recovery(self):
        await self.send_message(notify_cycle_recovered())

    async def alert_loss_limit(self, daily_pnl: float, bankroll: float):
        await self.send_message(notify_loss_limit(daily_pnl, bankroll))

    async def alert_total_drawdown(self, drawdown_pct: float, bankroll: float):
        await self.send_message(notify_drawdown_stop(drawdown_pct, bankroll))

    async def alert_api_error(self, error: str):
        await self.send_message(notify_api_error(error))

    async def send_daily_report(
        self,
        date: datetime,
        pnl: float,
        trades_count: int,
        hit_rate: float,
        bankroll: float,
        open_positions: int,
        max_drawdown: float,
    ):
        text = tpl_daily(
            date.strftime("%Y-%m-%d"),
            pnl,
            trades_count,
            hit_rate,
            bankroll,
            open_positions,
            max_drawdown,
        )
        await self.send_message(text)

    async def send_settlement(self, question: str, outcome: str, pnl: float, side: str):
        await self.send_message(tpl_settlement(question, outcome, side, pnl))

    async def send_position_exit(self, question: str, reason: str, pnl: float):
        await self.send_message(notify_position_exit(question, reason, pnl))

    async def send_trade_opened(
        self,
        question: str,
        side: str,
        stake: float,
        contracts: float,
        price: float,
        edge: float,
        hypothesis_id: str,
    ):
        await self.send_message(
            notify_trade_opened(question, side, stake, contracts, price, edge, hypothesis_id)
        )
