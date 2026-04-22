"""
Process-local metrics for Telegram /health (updated by orchestrator each cycle).
Not durable — for durable history use audit_log / DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

_state: dict[str, Any] = {
    "last_cycle_ts": None,
    "last_cycle_ok": True,
    "last_cycle_error": None,
    "cycle_failures_in_row": 0,
    "ws_connected": False,
    "markets_fetched_last": 0,
    "started_at": None,
}


def mark_started():
    if _state["started_at"] is None:
        _state["started_at"] = datetime.now(timezone.utc)


def update_cycle_ok(
    *,
    markets_count: int = 0,
    ws_connected: bool = False,
):
    _state["last_cycle_ts"] = datetime.now(timezone.utc)
    _state["last_cycle_ok"] = True
    _state["last_cycle_error"] = None
    _state["cycle_failures_in_row"] = 0
    _state["markets_fetched_last"] = markets_count
    _state["ws_connected"] = ws_connected


def update_cycle_error(exc: str):
    _state["last_cycle_ts"] = datetime.now(timezone.utc)
    _state["last_cycle_ok"] = False
    _state["last_cycle_error"] = exc[:2000]
    _state["cycle_failures_in_row"] = int(_state.get("cycle_failures_in_row", 0)) + 1


def set_ws_connected(connected: bool):
    _state["ws_connected"] = connected


def snapshot() -> dict[str, Any]:
    return dict(_state)


def uptime_sec() -> float | None:
    t0 = _state.get("started_at")
    if t0 is None:
        return None
    return (datetime.now(timezone.utc) - t0).total_seconds()
