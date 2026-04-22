"""HTML-шаблоны уведомлений на русском (Telegram parse_mode=HTML)."""

from __future__ import annotations

import html


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def notify_startup(mode: str, bankroll: float, hypotheses: list[str], cycle_sec: int) -> str:
    hs = ", ".join(esc(x) for x in hypotheses)
    return (
        "🚀 <b>Трейдер запущен</b>\n"
        f"Режим: <code>{esc(mode)}</code>\n"
        f"Банкролл: <b>${bankroll:.2f}</b>\n"
        f"Гипотезы: {hs or '—'}\n"
        f"Интервал цикла: {cycle_sec} с\n"
        "<i>Ожидание рыночных данных…</i>"
    )


def notify_shutdown(mode: str, cycles: int, bankroll: float) -> str:
    return (
        "🛑 <b>Остановка трейдера</b>\n"
        f"Режим: <code>{esc(mode)}</code>\n"
        f"Циклов: {cycles}\n"
        f"Банкролл: <b>${bankroll:.2f}</b>"
    )


def notify_cycle_failed(msg: str) -> str:
    return (
        "🔴 <b>Сбой цикла</b>\n"
        f"<pre>{esc(msg[:3500])}</pre>"
    )


def notify_cycle_recovered() -> str:
    return "🟢 <b>Цикл восстановлен</b>\n<i>Система снова в норме.</i>"


def notify_position_exit(question: str, reason: str, pnl: float) -> str:
    emoji = "🟢" if pnl >= 0 else "🔴"
    kind = "выход"
    ru = reason.upper()
    if "TAKE-PROFIT" in ru or "TAKE_PROFIT" in ru:
        kind = "тейк-профит"
    elif "STOP-LOSS" in ru or "STOP_LOSS" in ru:
        kind = "стоп-лосс"
    elif "TIME-EXIT" in ru or "TIME_EXIT" in ru:
        kind = "выход по времени"
    return (
        f"{emoji} <b>Закрытие позиции ({kind})</b>\n"
        f"{esc(question[:200])}\n"
        f"<i>{esc(reason[:300])}</i>\n"
        f"P&amp;L: <b>${pnl:+.2f}</b>"
    )


def notify_trade_opened(
    question: str, side: str, stake: float, contracts: float, price: float, edge: float, hyp: str,
) -> str:
    return (
        "📥 <b>Сделка (симуляция)</b>\n"
        f"{esc(question[:180])}\n"
        f"Сторона: <b>{esc(side)}</b> | Цена: <b>{price:.4f}</b>\n"
        f"Ставка: ${stake:.2f} | Контракты: {contracts:.2f}\n"
        f"Edge: {edge:.3f} | Гипотеза: <code>{esc(hyp)}</code>"
    )


def notify_settlement(question: str, outcome: str, side: str, pnl: float) -> str:
    emoji = "✅" if pnl > 0 else "❌"
    return (
        f"{emoji} <b>Исход (settlement)</b>\n"
        f"{esc(question[:180])}\n"
        f"Исход рынка: <b>{esc(outcome)}</b> | Ваша сторона: <b>{esc(side)}</b>\n"
        f"P&amp;L: <b>${pnl:+.2f}</b>"
    )


def notify_loss_limit(daily_pnl: float, bankroll: float) -> str:
    return (
        "🚨 <b>Дневной лимит убытка</b>\n"
        f"P&amp;L за день: <b>${daily_pnl:.2f}</b>\n"
        f"Банкролл: ${bankroll:.2f}\n"
        "<i>Торговля приостановлена до завтра.</i>"
    )


def notify_drawdown_stop(dd_pct: float, bankroll: float) -> str:
    return (
        "⛔ <b>Стоп по просадке</b>\n"
        f"Просадка от пика: <b>{dd_pct:.1%}</b>\n"
        f"Банкролл: ${bankroll:.2f}\n"
        "<b>Торговля остановлена. Нужен ручной разбор.</b>"
    )


def notify_daily_report(
    date_str: str, pnl: float, trades: int, hit: float, bankroll: float, open_n: int, dd: float,
) -> str:
    emoji = "📈" if pnl >= 0 else "📉"
    return (
        f"{emoji} <b>Дневной отчёт — {esc(date_str)}</b>\n\n"
        f"P&amp;L: <b>${pnl:+.2f}</b>\n"
        f"Сделок: {trades}\n"
        f"Win rate: {hit:.1%}\n"
        f"Банкролл: ${bankroll:.2f}\n"
        f"Открыто позиций: {open_n}\n"
        f"Просадка от пика: {dd:.1%}"
    )


def notify_api_error(err: str) -> str:
    return f"⚠️ <b>Ошибка API</b>\n<pre>{esc(err[:500])}</pre>"


def access_denied_ru() -> str:
    return (
        "⛔ <b>Нет доступа</b>\n"
        "Запрос отправлен администратору или используйте одобренный аккаунт.\n"
        "Команда: /start"
    )


def access_pending_ru() -> str:
    return "⏳ <b>Заявка на рассмотрении</b>\nОжидайте решения администратора."


def access_approved_ru() -> str:
    return "✅ <b>Доступ разрешён</b>\nМожно пользоваться командами: /status /help"


def admin_approve_request_ru(user_id: str, username: str | None) -> str:
    u = f"@{esc(username)}" if username else "без username"
    return (
        "🔔 <b>Запрос доступа к боту</b>\n"
        f"ID: <code>{esc(user_id)}</code> {u}\n"
        "Выберите действие:"
    )
