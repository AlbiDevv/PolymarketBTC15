"""Compact Telegram analytics messages for the trading lab."""

from __future__ import annotations

from datetime import datetime
from html import escape

from .stats_service import StatusSnapshot


def _ts(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()[:19]
    return str(value or "n/a")


def _money(value: float | None) -> str:
    return f"${(value or 0.0):+.2f}"


def _fmt_ab_groups(groups: dict[str, dict] | None) -> list[str]:
    if not groups:
        return []
    lines: list[str] = ["<b>A/B</b>"]
    for key in ("control", "learned"):
        item = (groups or {}).get(key)
        if not item:
            continue
        lines.append(
            f"- {escape(key)}: equity=${float(item.get('equity') or 0.0):.2f} "
            f"realized={_money(float(item.get('realized_pnl') or 0.0))} "
            f"trades={int(item.get('closed_trades') or 0)} "
            f"fill={float(item.get('fill_rate') or 0.0):.2%} "
            f"forced={float(item.get('forced_taker_exit_ratio') or 0.0):.2%}"
        )
    return lines


def _status_bankroll_line(st: StatusSnapshot) -> str:
    groups = st.ab_groups or {}
    if not groups:
        return f"Bankroll ${st.bankroll:.2f} | today {_money(st.realized_pnl_today)} | u {_money(st.unrealized_pnl)}"
    if "control" not in groups and len(groups) <= 1:
        return f"Bankroll ${st.bankroll:.2f} | today {_money(st.realized_pnl_today)} | u {_money(st.unrealized_pnl)}"

    combined = float(st.bankroll or 0.0)
    control = groups.get("control") or {}
    control_equity = float(control.get("equity") or 0.0)
    learned_groups = {k: v for k, v in groups.items() if k != "control"}
    best_learned_key = None
    best_learned_equity = 0.0
    if learned_groups:
        best_learned_key, best_learned = max(
            learned_groups.items(),
            key=lambda item: float((item[1] or {}).get("equity") or 0.0),
        )
        best_learned_equity = float((best_learned or {}).get("equity") or 0.0)
    parts = []
    if control:
        parts.append(f"control ${control_equity:.2f}")
    if best_learned_key is not None:
        parts.append(f"best {escape(str(best_learned_key))} ${best_learned_equity:.2f}")
    parts.append(f"combined ${combined:.2f}")
    return " | ".join(parts) + f" | today {_money(st.realized_pnl_today)} | u {_money(st.unrealized_pnl)}"


def fmt_crypto15m(data: dict) -> str:
    manifest = data.get("manifest") or {}
    best = data.get("best_portfolio") or {}
    reasons = data.get("reject_reasons_24h") or {}
    model_meta = data.get("latest_model_meta") or {}
    assets = data.get("assets") or {}
    thresholds = data.get("thresholds") or {}
    active_thresholds = list(data.get("active_thresholds") or [])
    trade_assets = list(data.get("trade_assets") or ["BTC"])
    lines = [
        "<b>Crypto15m BTC-only</b>",
        f"Active: {escape('/'.join(str(asset) for asset in trade_assets) or 'BTC')} | "
        f"{escape('/'.join(active_thresholds) or 'n/a')}",
        f"Model: {'OK' if manifest.get('accepted') else 'REJECTED'} | "
        f"{int(manifest.get('coverage_days') or 0)}d / {int(manifest.get('markets_used') or 0)} markets",
        f"WS {'OK' if data.get('ws_connected') else 'NO'} h={float(data.get('ws_health_score') or 0.0):.2f} "
        f"| OHLCV {float(data.get('latest_ohlcv_age_sec') or 0.0):.0f}s "
        f"{escape(str(data.get('latest_ohlcv_exchange') or 'n/a'))}",
        f"24h: cand {int(data.get('candidate_count_24h') or 0)} | "
        f"orders {int(data.get('orders_count') or 0)} | fills {int(data.get('fills_count') or 0)}",
    ]
    if data.get("last_decision_ts"):
        lines.append(
            f"Last decision: <code>{escape(_ts(data.get('last_decision_ts')))}</code> "
            f"| {escape(str(data.get('last_decision_reason') or data.get('last_decision_stage') or 'n/a'))}"
        )
    if best:
        lines.append(
            f"Best: <code>{escape(str(best.get('key') or 'n/a'))}</code> "
            f"equity=${float(best.get('equity') or 0.0):.2f} "
            f"realized={_money(float(best.get('realized_pnl') or 0.0))}"
        )
    for asset in ("BTC", "ETH", "Other"):
        item = assets.get(asset) or {}
        if asset == "Other" and not any(float(item.get(key) or 0.0) for key in ("realized_pnl", "unrealized_pnl", "orders", "fills", "open_positions")):
            continue
        title = asset if asset == "BTC" else f"{asset} disabled / historical only"
        lines.append(
            f"<b>{title}</b>: pnl {_money(float(item.get('realized_pnl') or 0.0))} "
            f"u {_money(float(item.get('unrealized_pnl') or 0.0))} | "
            f"o/f {int(item.get('orders') or 0)}/{int(item.get('fills') or 0)} | "
            f"open {int(item.get('open_positions') or 0)} | "
            f"best {escape(str(item.get('best_threshold') or 'n/a'))}"
        )
        if item.get("last_reject_reason"):
            lines.append(f"last {asset}: <code>{escape(str(item.get('last_reject_reason'))[:60])}</code>")
    if thresholds:
        active_set = set(active_thresholds)
        preferred = ("control", "t65", "t65_ai", "t70", "t70_ai", "t75", "t75_ai", "t80", "t80_ai", "t90", "t90_ai", "t95", "t95_ai")
        ordered = [key for key in preferred if key in thresholds and (key == "control" or key.replace("_ai", "") in active_set)]
        ordered.extend(
            key for key in sorted(thresholds)
            if key not in ordered and (key == "control" or key.replace("_ai", "") in active_set)
        )
        parts = []
        for key in ordered:
            item = thresholds[key]
            parts.append(
                f"{key}: eq ${float(item.get('equity') or 0.0):.2f}, "
                f"r {_money(float(item.get('realized_pnl') or 0.0))}, fills {int(item.get('fills') or 0)}"
            )
        if parts:
            lines.append("<b>Thresholds</b>")
            lines.append("<code>" + escape(" | ".join(parts)) + "</code>")
    if model_meta:
        lines.append(
            f"Last model: pYES={float(model_meta.get('model_yes_probability') or 0.0):.3f} "
            f"EV={float(model_meta.get('expected_net_ev') or 0.0):+.4f} "
            f"thr={float(model_meta.get('threshold') or 0.0):.2f}"
        )
    if reasons:
        top = ", ".join(f"{key}:{value}" for key, value in list(reasons.items())[:3])
        lines.append(f"Rejects: <code>{escape(top)}</code>")
    return "\n".join(lines)


def fmt_status(st: StatusSnapshot, runtime: dict | None = None) -> str:
    lines = [
        "<b>Status</b>",
        f"{escape(st.mode)} | WS {'OK' if st.ws_connected else 'NO'} h={st.ws_health_score:.2f} subs={st.subscribed_tokens_last}",
        _status_bankroll_line(st),
        f"Open {st.open_positions_count} | trades {st.trades_today} | DD {st.drawdown_pct:.2%} | forced {st.forced_taker_exit_ratio:.2%}",
        f"Cycle <code>{escape(_ts(st.last_cycle_ts))}</code>",
    ]
    if st.last_decision_ts:
        decision_line = f"Last decision <code>{escape(_ts(st.last_decision_ts))}</code>"
        if st.latest_ohlcv_age_sec is not None:
            decision_line += f" | OHLCV {float(st.latest_ohlcv_age_sec):.0f}s"
        lines.append(decision_line)
    if st.candidate_count_24h or st.accepted_count_24h or st.reject_count_24h:
        lines.append(
            f"Crypto15m 24h c/a/r "
            f"{int(st.candidate_count_24h)}/{int(st.accepted_count_24h)}/{int(st.reject_count_24h)}"
        )
    if st.top_reject_reason:
        lines.append(f"Top reject <code>{escape(st.top_reject_reason)}</code>")
    # /crypto15m carries the detailed threshold table; keep /status readable.
    if st.learned_artifact_key:
        lines.append(f"Model artifact: <code>{escape(st.learned_artifact_key)}</code>")
    if st.last_cycle_error:
        lines.append(f"Last error: <code>{escape(st.last_cycle_error[:250])}</code>")
    return "\n".join(lines)


def fmt_pnl(data: dict) -> str:
    lines = [
        "<b>PnL</b>",
        f"Today: <b>{_money(data['today_realized'])}</b>",
        f"7d: {_money(data['d7_realized'])}",
        f"Realized total: {_money(data['all_time_realized'])}",
        f"Unrealized: {_money(data['unrealized'])}",
        f"Bankroll: ${data['bankroll']:.2f}",
        f"Trades: {data['trades_total']} (W{data['wins']}/L{data['losses']})",
        f"Avg PnL/trade: {data['avg_pnl']:+.4f}",
        f"Best / worst: {data['best_trade']} / {data['worst_trade']}",
    ]
    lines.extend(_fmt_ab_groups(data.get("ab_groups")))
    return "\n".join(lines)


def _dedupe_positions(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple, dict] = {}
    for row in rows:
        key = (
            row.get("question", ""),
            row.get("side", ""),
            round(float(row.get("entry", 0.0)), 4),
            round(float(row.get("mark", 0.0)), 4),
        )
        current = grouped.setdefault(key, {
            "question": row.get("question", ""),
            "side": row.get("side", ""),
            "entry": float(row.get("entry", 0.0)),
            "mark": float(row.get("mark", 0.0)),
            "size": 0.0,
            "unrealized_pnl": 0.0,
            "hold_hours": 0.0,
            "portfolio_keys": [],
        })
        current["size"] += float(row.get("size", 0.0))
        current["unrealized_pnl"] += float(row.get("unrealized_pnl", 0.0))
        current["hold_hours"] = max(current["hold_hours"], float(row.get("hold_hours", 0.0)))
        if row.get("portfolio_key"):
            current["portfolio_keys"].append(str(row["portfolio_key"]))
    return list(grouped.values())


def fmt_positions(rows: list[dict]) -> str:
    if not rows:
        return "<b>Open positions</b>\nNo open positions."
    lines = ["<b>Open positions</b>"]
    for row in _dedupe_positions(rows)[:15]:
        portfolios = ", ".join(sorted(set(row["portfolio_keys"])))
        lines.append(
            "- "
            f"{escape(row['side'])} size={row['size']:.2f} "
            f"entry={row['entry']:.4f} mark={row['mark']:.4f} "
            f"uPnL={_money(row['unrealized_pnl'])} hold={row['hold_hours']:.1f}h"
        )
        if portfolios:
            lines.append(f"  <i>{escape(portfolios)}</i>")
        lines.append(f"  {escape((row['question'] or '')[:140])}")
    return "\n".join(lines)


def fmt_trades(rows: list[dict]) -> str:
    if not rows:
        return "<b>Recent closed</b>\nNo closed trades."
    lines = ["<b>Recent closed</b>"]
    for row in rows[:15]:
        closed_at = _ts(row.get("closed_at"))
        forced = " forced" if row.get("forced_exit") else ""
        group_suffix = f" [{row.get('ab_group')}]" if row.get("ab_group") else ""
        lines.append(
            "- "
            f"{escape(closed_at[:16])} {escape(row.get('portfolio_key', ''))}{group_suffix} "
            f"{escape(row.get('side', ''))} "
            f"exit=<code>{escape(row.get('exit_reason', 'unknown'))}</code>{forced} "
            f"PnL={_money(row.get('pnl'))} "
            f"entry={float(row.get('entry', 0.0)):.4f} "
            f"exit={float(row.get('exit', 0.0)):.4f}"
        )
        if row.get("question"):
            lines.append(f"  {escape(str(row['question'])[:120])}")
    return "\n".join(lines)


def fmt_rejections(rows: list[dict]) -> str:
    if not rows:
        return "<b>Rejected candidates</b>\nNo fresh reject rows."
    lines = ["<b>Rejected candidates</b>"]
    for row in rows[:12]:
        meta = row.get("meta") or {}
        reason_items = list(row.get("reasons") or [])
        if meta.get("reason"):
            reason_items.append(str(meta.get("reason")))
        if meta.get("stage"):
            reason_items.append(str(meta.get("stage")))
        reasons = ", ".join(dict.fromkeys(reason_items)) or "n/a"
        lines.append(
            "- "
            f"{escape(row.get('portfolio_key') or row.get('track') or 'reject')} "
            f"score={float(row.get('quality_score', 0.0)):.1f} "
            f"net={float(row.get('expected_net_edge', 0.0)):+.4f}"
        )
        lines.append(f"  {escape(reasons)}")
        lines.append(f"  {escape(str(row.get('question', ''))[:120])}")
    return "\n".join(lines)


def fmt_candidates(rows: list[dict]) -> str:
    if not rows:
        return "<b>Candidates</b>\nNo fresh candidates."
    lines = ["<b>Candidates</b>"]
    for row in rows[:12]:
        reasons = ", ".join(row.get("reasons") or [])
        lines.append(
            "- "
            f"{escape(row.get('portfolio_key') or row.get('track') or '')} "
            f"<code>{escape(row.get('decision', 'candidate'))}</code> "
            f"{escape(row.get('side') or '')} "
            f"edge={float(row.get('edge', 0.0)):+.4f} "
            f"score={float(row.get('quality_score', 0.0)):.1f}"
        )
        lines.append(f"  {escape(str(row.get('question', ''))[:120])}")
        if reasons:
            lines.append(f"  <i>{escape(reasons)}</i>")
    return "\n".join(lines)


def fmt_portfolios(rows: list[dict]) -> str:
    lines = ["<b>Portfolios</b>"]
    for row in rows[:16]:
        group = row.get("ab_group", "single")
        lines.append(
            "- "
            f"{escape(row['key'])} [{escape(row['track'])}/{escape(group)}] "
            f"realized={_money(row['realized_pnl'])} "
            f"closed={row['closed_trades']} "
            f"seen/sig/acc/fill={row.get('markets_seen_24h', 0)}/{row.get('signals_24h', 0)}/{row.get('accepted_count_24h', 0)}/{row.get('fills_count', 0)} "
            f"cand/rej 24h={row['candidate_count_24h']}/{row['reject_count_24h']}"
        )
    return "\n".join(lines)


def fmt_gates(data: dict) -> str:
    started = _ts(data.get("paper_started_at"))
    return "\n".join([
        "<b>Gates</b>",
        f"Started: {escape(started[:16])}",
        f"Days: {data.get('paper_days_completed')}",
        f"Trades: {data.get('paper_trades_count')}",
        f"PnL: {_money(data.get('paper_realized_pnl', 0.0))}",
        f"Errors: {data.get('paper_errors_count')}",
        f"Status: {escape(str(data.get('gate_status')))}",
    ])


def fmt_health(status: StatusSnapshot, stats: object, synthetic_note: str = "") -> str:
    lines = [
        "<b>Health</b>",
        f"Last cycle: {escape(_ts(status.last_cycle_ts))}",
        f"WS: {'yes' if status.ws_connected else 'no'}",
        f"WS health: {status.ws_health_score:.2f}",
        f"Entries frozen: {'yes' if status.entries_frozen else 'no'}",
        f"Forced taker ratio: {status.forced_taker_exit_ratio:.2%}",
        f"Markets in DB: {stats.markets_count()}",
        f"Audit errors 24h: {stats.error_counts(24)}",
        f"Shortlist: {status.markets_fetched_last}",
        f"WS subscriptions: {status.subscribed_tokens_last}",
    ]
    lines.extend(_fmt_ab_groups(status.ab_groups))
    if status.learned_artifact_key:
        lines.append(f"Artifact: <code>{escape(status.learned_artifact_key)}</code>")
    if status.last_cycle_error:
        lines.append(f"Error: <code>{escape(status.last_cycle_error[:250])}</code>")
    if synthetic_note:
        lines.append(f"Note: {escape(synthetic_note)}")
    return "\n".join(lines)


def fmt_daily_summary(
    bankroll: float,
    realized_day: float,
    unrealized: float,
    trades: int,
    hit: float,
    open_n: int,
    dd: float,
    gate: dict,
    err_24: int,
    exit_dist: dict[str, int] | None = None,
) -> str:
    lines = [
        "<b>Daily summary UTC</b>",
        f"Bankroll: ${bankroll:.2f}",
        f"Realized today: {_money(realized_day)}",
        f"Unrealized: {_money(unrealized)}",
        f"Closed trades: {trades}",
        f"Win rate: {hit:.1%}" if trades else "Win rate: n/a",
        f"Open positions: {open_n}",
        f"Drawdown: {dd:.2%}",
        f"Gate: {escape(str(gate.get('gate_status')))} | errors 24h: {err_24}",
    ]
    if exit_dist:
        lines.append(f"Exit reasons: {escape(str(exit_dist))}")
    return "\n".join(lines)


def fmt_dashboard_hint(*, base_url: str, ssh_hint: str, local_url: str, mode: str) -> str:
    if mode == "ssh_hint":
        return "\n".join([
            "<b>Dashboard</b>",
            "Mini app is disabled because there is no public HTTPS URL.",
            f"1. Open a tunnel on your laptop: <code>{escape(ssh_hint)}</code>",
            f"2. Then open: <code>{escape(local_url)}</code>",
            f"Current base_url: <code>{escape(base_url)}</code>",
            "On a phone this requires an SSH client with local port forwarding.",
        ])
    return f"<b>Dashboard</b>\n{escape(base_url)}"
