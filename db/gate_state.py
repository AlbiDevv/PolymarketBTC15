"""Structured gate metrics (single row id=1)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .models import GateStateRow


def get_or_create_gate_state(session: Session) -> GateStateRow:
    row = session.get(GateStateRow, 1)
    if row is None:
        row = GateStateRow(
            id=1,
            gate_status="dry_run",
        )
        session.add(row)
        session.flush()
    return row
