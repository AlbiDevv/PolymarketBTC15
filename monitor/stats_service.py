"""
Aggregates trading statistics from DB (single source of truth). Read-only.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from db.gate_state import get_or_create_gate_state
from db.models import AuditRow, MarketRow, PnlLogRow, PositionRow


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class StatusSnapshot:
    mode: str
    bankroll: float
    realized_pnl_today: float
    unrealized_pnl: float
    open_positions_count: int
    trades_today: int
    drawdown_pct: float
    last_cycle_ts: Any
    ws_connected: bool
    gate_status: str
    paper_days: int
    last_cycle_error: str | None = None
    markets_fetched_last: int = 0
    subscribed_tokens_last: int = 0
    ws_health_score: float = 0.0
    entries_frozen: bool = False
    forced_taker_exit_ratio: float = 0.0
    learned_artifact_key: str | None = None
    ab_groups: dict[str, Any] = field(default_factory=dict)
    candidate_count_24h: int = 0
    accepted_count_24h: int = 0
    reject_count_24h: int = 0
    top_reject_reason: str | None = None
    last_decision_ts: Any = None
    latest_ohlcv_age_sec: float | None = None


class StatsService:
    def __init__(self, db_url: str, initial_bankroll: float, mode: str):
        self._db_url = db_url
        self._initial = initial_bankroll
        self._mode = mode

    def _session(self) -> Session:
        from db.session import get_session

        return get_session(self._db_url)

    def bankroll_latest(self, session: Session) -> float:
        last = session.query(PnlLogRow).order_by(PnlLogRow.date.desc()).first()
        if last:
            return float(last.bankroll)
        return self._initial

    def unrealized_open(self, session: Session) -> tuple[float, int]:
        open_p = session.query(PositionRow).filter(PositionRow.status == "open").all()
        u = 0.0
        for p in open_p:
            ep = p.entry_price
            cp = p.current_price if p.current_price is not None else ep
            u += p.size * (cp - ep)
        return u, len(open_p)

    def realized_since(self, session: Session, since: datetime) -> float:
        closed = (
            session.query(PositionRow)
            .filter(PositionRow.status == "closed")
            .filter(PositionRow.closed_at.isnot(None))
            .filter(PositionRow.closed_at >= since)
            .all()
        )
        return sum(p.pnl or 0.0 for p in closed)

    def closed_trades_since(self, session: Session, since: datetime) -> list[PositionRow]:
        return (
            session.query(PositionRow)
            .filter(PositionRow.status == "closed")
            .filter(PositionRow.closed_at.isnot(None))
            .filter(PositionRow.closed_at >= since)
            .order_by(PositionRow.closed_at.desc())
            .all()
        )

    def drawdown_vs_peak(self, session: Session, bankroll: float) -> float:
        peak = session.query(func.max(PnlLogRow.bankroll)).scalar()
        peak = float(peak) if peak else bankroll
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - bankroll) / peak)

    def get_status(self, runtime: dict[str, Any]) -> StatusSnapshot:
        session = self._session()
        try:
            br = self.bankroll_latest(session)
            u, n_open = self.unrealized_open(session)
            today = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            realized_day = self.realized_since(session, today)
            trades_today = (
                session.query(PositionRow)
                .filter(PositionRow.status == "closed")
                .filter(PositionRow.closed_at >= today)
                .count()
            )
            dd = self.drawdown_vs_peak(session, br)
            gs = get_or_create_gate_state(session)
            return StatusSnapshot(
                mode=self._mode,
                bankroll=br,
                realized_pnl_today=realized_day,
                unrealized_pnl=u,
                open_positions_count=n_open,
                trades_today=trades_today,
                drawdown_pct=dd,
                last_cycle_ts=runtime.get("last_cycle_ts"),
                ws_connected=bool(runtime.get("ws_connected")),
                gate_status=gs.gate_status,
                paper_days=int(gs.paper_days_completed or 0),
            )
        finally:
            session.close()

    def pnl_breakdown(self) -> dict[str, Any]:
        session = self._session()
        try:
            now = _utcnow()
            d7 = now - timedelta(days=7)
            d0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
            closed_all = session.query(PositionRow).filter(PositionRow.status == "closed").all()
            pnl_all = sum(p.pnl or 0.0 for p in closed_all)
            wins = [p for p in closed_all if (p.pnl or 0) > 0]
            losses = [p for p in closed_all if (p.pnl or 0) <= 0]
            closed_7d = [
                p for p in closed_all
                if _as_utc(p.closed_at) and _as_utc(p.closed_at) >= d7
            ]
            pnl_7d = sum(p.pnl or 0.0 for p in closed_7d)
            closed_day = [
                p for p in closed_all
                if _as_utc(p.closed_at) and _as_utc(p.closed_at) >= d0
            ]
            pnl_day = sum(p.pnl or 0.0 for p in closed_day)
            trades_today = len(closed_day)
            wins_today = sum(1 for p in closed_day if (p.pnl or 0) > 0)
            best = max((p.pnl for p in closed_all if p.pnl is not None), default=None)
            worst = min((p.pnl for p in closed_all if p.pnl is not None), default=None)
            br = self.bankroll_latest(session)
            u, _ = self.unrealized_open(session)
            return {
                "today_realized": pnl_day,
                "trades_today": trades_today,
                "wins_today": wins_today,
                "d7_realized": pnl_7d,
                "all_time_realized": pnl_all,
                "unrealized": u,
                "bankroll": br,
                "trades_total": len(closed_all),
                "wins": len(wins),
                "losses": len(losses),
                "avg_pnl": (pnl_all / len(closed_all)) if closed_all else 0.0,
                "best_trade": best,
                "worst_trade": worst,
            }
        finally:
            session.close()

    def open_positions_detail(self) -> list[dict[str, Any]]:
        session = self._session()
        try:
            rows = session.query(PositionRow).filter(PositionRow.status == "open").all()
            out = []
            for p in rows:
                m = session.get(MarketRow, p.market_id)
                ep = p.entry_price
                cp = p.current_price if p.current_price is not None else ep
                u_pnl = p.size * (cp - ep)
                opened = p.opened_at
                hold_h = 0.0
                if opened:
                    o = opened.replace(tzinfo=timezone.utc) if opened.tzinfo is None else opened
                    hold_h = (_utcnow() - o).total_seconds() / 3600.0
                out.append({
                    "question": (m.question[:80] + "...") if m and len(m.question) > 80 else (m.question if m else ""),
                    "market_id": p.market_id,
                    "token_id": p.token_id,
                    "side": p.side,
                    "entry": ep,
                    "mark": cp,
                    "size": p.size,
                    "unrealized_pnl": u_pnl,
                    "hold_hours": hold_h,
                })
            return out
        finally:
            session.close()

    def recent_trades(self, n: int = 15) -> list[dict[str, Any]]:
        session = self._session()
        try:
            closed = (
                session.query(PositionRow)
                .filter(PositionRow.status == "closed")
                .order_by(PositionRow.closed_at.desc())
                .limit(n)
                .all()
            )
            out = []
            for p in closed:
                m = session.get(MarketRow, p.market_id)
                cp = p.current_price if p.current_price is not None else p.entry_price
                out.append({
                    "opened_at": p.opened_at,
                    "closed_at": p.closed_at,
                    "side": p.side,
                    "size": p.size,
                    "entry": p.entry_price,
                    "exit": cp,
                    "pnl": p.pnl,
                    "exit_reason": getattr(p, "exit_reason", None) or "unknown",
                    "question": (m.question[:60] + "...") if m and m.question else "",
                })
            return out
        finally:
            session.close()

    def exit_reason_counts_today(self) -> dict[str, int]:
        session = self._session()
        try:
            today = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            closed = (
                session.query(PositionRow)
                .filter(PositionRow.status == "closed")
                .filter(PositionRow.closed_at.isnot(None))
                .filter(PositionRow.closed_at >= today)
                .all()
            )
            c: Counter[str] = Counter()
            for p in closed:
                c[getattr(p, "exit_reason", None) or "unknown"] += 1
            return dict(c)
        finally:
            session.close()

    def gate_snapshot(self) -> dict[str, Any]:
        session = self._session()
        try:
            gs = get_or_create_gate_state(session)
            return {
                "paper_started_at": gs.paper_started_at,
                "paper_days_completed": gs.paper_days_completed,
                "paper_trades_count": gs.paper_trades_count,
                "paper_realized_pnl": gs.paper_realized_pnl,
                "paper_errors_count": gs.paper_errors_count,
                "gate_status": gs.gate_status,
            }
        finally:
            session.close()

    def error_counts(self, hours: int = 24) -> int:
        session = self._session()
        try:
            since = _utcnow() - timedelta(hours=hours)
            return (
                session.query(AuditRow)
                .filter(AuditRow.timestamp >= since)
                .filter(AuditRow.event_type.like("%error%"))
                .count()
            )
        finally:
            session.close()

    def markets_count(self) -> int:
        session = self._session()
        try:
            return session.query(MarketRow).count()
        finally:
            session.close()
