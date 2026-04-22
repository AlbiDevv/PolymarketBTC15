"""Map human-readable exit strings from orchestrator/audit to stable codes."""

from __future__ import annotations


def map_exit_reason_from_audit(reason: str | None) -> str:
    if not reason:
        return "unknown"
    u = reason.upper()
    if "STOP-LOSS" in u or "STOP_LOSS" in u:
        return "stop_loss"
    if "TAKE-PROFIT" in u or "TAKE_PROFIT" in u:
        return "take_profit"
    if "TIME-EXIT" in u or "TIME_EXIT" in u:
        return "time_exit"
    if "REVERSAL" in u or ("SIGNAL" in u and "EXIT" in u):
        return "signal_reversal"
    return "unknown"
