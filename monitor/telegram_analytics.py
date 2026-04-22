from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_settings
from db.models import LabRuntimeStatusRow
from db.session import get_session
from lab import LabStatsService
from monitor import runtime_state
from monitor.stats_service import StatsService
from monitor.telegram_acl import TelegramACL
from monitor.telegram_formatters import (
    fmt_crypto15m,
    fmt_candidates,
    fmt_dashboard_hint,
    fmt_gates,
    fmt_health,
    fmt_pnl,
    fmt_positions,
    fmt_portfolios,
    fmt_rejections,
    fmt_status,
    fmt_trades,
)
from monitor.telegram_templates_ru import access_denied_ru


async def _tg_api(token: str, method: str, **kwargs) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.post(url, json=kwargs)
        response.raise_for_status()
        return response.json()


async def _send_message(
    token: str,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
) -> None:
    body: dict = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML"}
    if reply_markup:
        body["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=30.0) as client:
        await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=body,
        )


def _build_stats_service(settings):
    if _shadow_lab_mode(settings):
        return LabStatsService(settings.database.url, settings.bankroll.initial, settings.mode, settings=settings)
    return StatsService(settings.database.url, settings.bankroll.initial, settings.mode)


def _shadow_lab_mode(settings) -> bool:
    if settings.mode == "shadow_maker":
        return True
    session = get_session(settings.database.url)
    try:
        row = session.query(LabRuntimeStatusRow).order_by(LabRuntimeStatusRow.id.asc()).first()
        return bool(row and row.mode == "shadow_maker")
    finally:
        session.close()


async def poll_loop(settings):
    token = settings.alerts.telegram_bot_token
    primary = settings.alerts.telegram_chat_id
    admin_ids = list(settings.alerts.telegram_admin_chat_ids or [])
    if not token or not primary:
        logger.info("Telegram analytics bot disabled: token/chat is empty.")
        return

    acl = TelegramACL(PROJECT_ROOT, primary_chat_id=primary, admin_chat_id=admin_ids)
    stats = _build_stats_service(settings)
    offset = 0

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            try:
                url = f"https://api.telegram.org/bot{token}/getUpdates"
                response = await client.get(url, params={"offset": offset, "timeout": 25})
                response.raise_for_status()
                data = response.json()

                for update in data.get("result", []):
                    offset = update["update_id"] + 1

                    if "callback_query" in update:
                        callback = update["callback_query"]
                        callback_id = callback["id"]
                        await _tg_api(token, "answerCallbackQuery", callback_query_id=callback_id)
                        continue

                    message = update.get("message") or {}
                    chat = message.get("chat") or {}
                    uid = str(chat.get("id", ""))
                    text = (message.get("text") or "").strip()
                    if not uid:
                        continue

                    if not acl.is_allowed(uid):
                        if acl.multi_user_mode:
                            logger.debug(f"Ignoring telegram message from non-admin user {uid}")
                        else:
                            await _send_message(token, uid, access_denied_ru())
                        continue

                    if not text.startswith("/"):
                        continue

                    cmd = text.split()[0].lower().split("@")[0]
                    runtime = runtime_state.snapshot()

                    reply = ""
                    if cmd == "/status":
                        reply = fmt_status(stats.get_status(runtime), runtime)
                    elif cmd == "/pnl":
                        reply = fmt_pnl(stats.pnl_breakdown())
                    elif cmd == "/positions":
                        reply = fmt_positions(stats.open_positions_detail())
                    elif cmd == "/trades":
                        reply = fmt_trades(stats.recent_trades(15))
                    elif cmd == "/rejections" and hasattr(stats, "rejections"):
                        reply = fmt_rejections(stats.rejections(15))
                    elif cmd == "/candidates" and hasattr(stats, "candidates"):
                        reply = fmt_candidates(stats.candidates(15))
                    elif cmd == "/portfolios" and hasattr(stats, "portfolio_catalog"):
                        reply = fmt_portfolios(stats.portfolio_catalog())
                    elif cmd == "/crypto15m" and hasattr(stats, "crypto15m_snapshot"):
                        reply = fmt_crypto15m(stats.crypto15m_snapshot())
                    elif cmd == "/gates":
                        reply = fmt_gates(stats.gate_snapshot())
                    elif cmd == "/health":
                        reply = fmt_health(stats.get_status(runtime), stats, "shadow lab/live trading metrics")
                    elif cmd == "/dashboard":
                        if settings.telegram.dashboard_mode == "ssh_hint":
                            reply = fmt_dashboard_hint(
                                base_url=settings.dashboard.base_url,
                                ssh_hint=settings.dashboard.ssh_tunnel_hint,
                                local_url=settings.dashboard.local_url_hint,
                                mode="ssh_hint",
                            )
                        else:
                            dashboard_url = settings.telegram.webapp_url or settings.dashboard.base_url
                            if dashboard_url and settings.telegram.webapp_url:
                                await _send_message(
                                    token,
                                    uid,
                                    "<b>Dashboard</b>\nОткрыть mini app.",
                                    reply_markup={
                                        "inline_keyboard": [[{
                                            "text": "Open Dashboard",
                                            "web_app": {"url": settings.telegram.webapp_url},
                                        }]],
                                    },
                                )
                                continue
                            reply = fmt_dashboard_hint(
                                base_url=dashboard_url or settings.dashboard.base_url,
                                ssh_hint=settings.dashboard.ssh_tunnel_hint,
                                local_url=settings.dashboard.local_url_hint,
                                mode="direct",
                            )
                    elif cmd in ("/start", "/help"):
                        reply = "\n".join([
                            "<b>Commands</b>: /status /crypto15m /pnl /positions /trades /rejections /candidates /portfolios /gates /health /dashboard",
                            "Read only. No trading commands.",
                        ])
                    else:
                        reply = "Unknown command. /help"

                    await _send_message(token, uid, reply)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"telegram poll: {exc}")
                await asyncio.sleep(3)


async def daily_loop(settings, hour_utc: int = 23):
    token = settings.alerts.telegram_bot_token
    chat = settings.alerts.telegram_chat_id
    if not token or not chat or not settings.alerts.telegram_enabled:
        return

    stats = _build_stats_service(settings)
    from monitor.telegram_formatters import fmt_daily_summary

    while True:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep(max(1.0, (target - now).total_seconds()))

        pnl = stats.pnl_breakdown()
        status = stats.get_status(runtime_state.snapshot())
        gates = stats.gate_snapshot()
        trade_count = int(pnl.get("trades_today") or 0)
        wins = int(pnl.get("wins_today") or 0)
        hit_rate = wins / trade_count if trade_count else 0.0

        text = fmt_daily_summary(
            bankroll=pnl["bankroll"],
            realized_day=pnl["today_realized"],
            unrealized=pnl["unrealized"],
            trades=trade_count,
            hit=hit_rate,
            open_n=status.open_positions_count,
            dd=status.drawdown_pct,
            gate=gates,
            err_24=stats.error_counts(24),
            exit_dist=stats.exit_reason_counts_today() if trade_count else None,
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": text[:4000], "parse_mode": "HTML"},
            )


async def main_async(settings, with_daily: bool):
    tasks = [asyncio.create_task(poll_loop(settings))]
    if with_daily:
        tasks.append(asyncio.create_task(daily_loop(settings)))
    await asyncio.gather(*tasks)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--no-daily", action="store_true", help="Disable daily summary")
    args = parser.parse_args()

    settings = load_settings(args.config)
    if not settings.alerts.telegram_bot_token:
        logger.info("TELEGRAM_BOT_TOKEN is empty, exit.")
        return

    runtime_state.mark_started()
    asyncio.run(main_async(settings, with_daily=not args.no_daily))


if __name__ == "__main__":
    main()
