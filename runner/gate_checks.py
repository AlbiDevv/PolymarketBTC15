"""
Gate checks for mode transitions: dry_run → paper → live.

Uses structured ``GateStateRow`` (id=1) — no parsing of audit log text.
Run: python -m runner.gate_checks
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy import func

from db.models import PositionRow, AuditRow, SignalRow
from db.gate_state import get_or_create_gate_state


@dataclass
class GateResult:
    gate: str
    passed: bool
    detail: str


def check_dry_to_paper(session: Session) -> list[GateResult]:
    """
    Criteria to move from dry_run to paper:
      1. At least 3 days of data collection (audit log entries)
      2. At least 50 signals generated
      3. No unresolved crashes in last 24h
    """
    results = []
    now = datetime.now(timezone.utc)

    first_audit = session.query(func.min(AuditRow.timestamp)).scalar()
    if first_audit:
        age = (
            now - first_audit.replace(tzinfo=timezone.utc)
            if first_audit.tzinfo is None
            else now - first_audit
        )
        days = age.days
        results.append(GateResult(
            "running_days", days >= 3,
            f"Bot running for {days} days (need >= 3)",
        ))
    else:
        results.append(GateResult("running_days", False, "No audit data found"))

    signal_count = session.query(func.count(SignalRow.id)).scalar() or 0
    results.append(GateResult(
        "signal_count", signal_count >= 50,
        f"{signal_count} signals generated (need >= 50)",
    ))

    recent_errors = (
        session.query(AuditRow)
        .filter(AuditRow.timestamp >= now - timedelta(hours=24))
        .filter(AuditRow.event_type.like("%error%"))
        .count()
    )
    results.append(GateResult(
        "no_recent_errors", recent_errors == 0,
        f"{recent_errors} error events in last 24h",
    ))

    return results


def check_paper_to_live(session: Session) -> list[GateResult]:
    """
    Criteria to move from paper to live (structured + DB fallbacks):
      1. paper_days_completed >= 7 (from GateStateRow)
      2. paper_trades_count >= 20
      3. paper_realized_pnl > 0
      4. Hit rate >= 40% (closed positions)
      5. No drawdown stop events
      6. paper_errors_count == 0 (optional strictness)
    """
    results = []
    gs = get_or_create_gate_state(session)

    days = gs.paper_days_completed
    if gs.paper_started_at:
        t0 = gs.paper_started_at
        t0 = t0.replace(tzinfo=timezone.utc) if t0.tzinfo is None else t0
        days = max(days, (datetime.now(timezone.utc) - t0).days)

    results.append(GateResult(
        "paper_days", days >= 7,
        f"Paper days (computed): {days} (need >= 7)",
    ))

    trades = gs.paper_trades_count
    if trades < 1:
        closed = session.query(PositionRow).filter(PositionRow.status == "closed").count()
        trades = closed
    results.append(GateResult(
        "paper_trades_count", trades >= 20,
        f"{trades} paper/closed trades (need >= 20)",
    ))

    pnl = gs.paper_realized_pnl
    if pnl == 0.0:
        closed = session.query(PositionRow).filter(PositionRow.status == "closed").all()
        pnl = sum(p.pnl or 0 for p in closed)
    results.append(GateResult(
        "positive_pnl", pnl > 0,
        f"Paper realized PnL: ${pnl:+.2f}",
    ))

    closed = session.query(PositionRow).filter(PositionRow.status == "closed").all()
    trade_count = len(closed)
    wins = sum(1 for p in closed if (p.pnl or 0) > 0)
    hit_rate = wins / trade_count if trade_count else 0
    results.append(GateResult(
        "hit_rate", hit_rate >= 0.40,
        f"Hit rate: {hit_rate:.1%} (need >= 40%)",
    ))

    dd_stops = (
        session.query(AuditRow)
        .filter(AuditRow.event_type == "drawdown_stop")
        .count()
    )
    results.append(GateResult(
        "no_drawdown_stop", dd_stops == 0,
        f"{dd_stops} drawdown stop events",
    ))

    err_n = gs.paper_errors_count
    results.append(GateResult(
        "paper_errors", err_n == 0,
        f"paper_errors_count={err_n} (need 0)",
    ))

    return results


def run_all_gates(session: Session, current_mode: str) -> dict:
    """Run appropriate gates and return summary."""
    if current_mode == "dry_run":
        gates = check_dry_to_paper(session)
        target = "paper"
    elif current_mode == "paper":
        gates = check_paper_to_live(session)
        target = "live"
    else:
        return {"current_mode": current_mode, "message": "Already in live mode"}

    all_passed = all(g.passed for g in gates)

    for g in gates:
        status = "PASS" if g.passed else "FAIL"
        logger.info(f"  [{status}] {g.gate}: {g.detail}")

    return {
        "current_mode": current_mode,
        "target_mode": target,
        "all_passed": all_passed,
        "gates": [{"gate": g.gate, "passed": g.passed, "detail": g.detail} for g in gates],
    }


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from config import load_settings
    from db.session import get_session, init_db

    settings = load_settings()
    init_db(settings.database.url)
    session = get_session(settings.database.url)

    logger.info(f"Running gate checks for mode: {settings.mode}")
    result = run_all_gates(session, settings.mode)

    if result.get("all_passed"):
        logger.info(f"All gates PASSED. Safe to switch to {result['target_mode']}.")
    else:
        logger.warning("Some gates FAILED. Not ready to advance.")

    session.close()
