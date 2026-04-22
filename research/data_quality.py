"""
Row-level validation for research dataset v1.

Returns warnings (non-fatal) and fatal issues for invalid rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from .dataset_row import ResearchDatasetRowV1


@dataclass
class ValidationResult:
    ok: bool
    warnings: list[str]
    errors: list[str]


def validate_row_v1(row: ResearchDatasetRowV1) -> ValidationResult:
    warnings: list[str] = []
    errors: list[str] = []

    if row.resolved_outcome_for_side not in (0, 1):
        errors.append("resolved_outcome_for_side must be 0 or 1")

    if not (0.0 <= row.p_market <= 1.0):
        errors.append("p_market must be in [0, 1]")

    if row.time_to_resolution_sec < 0:
        errors.append("time_to_resolution_sec must be >= 0")

    if row.spread < 0:
        warnings.append("negative spread — check token book convention")

    src = getattr(row, "p_market_source", "") or ""
    if row.side == "NO" and src == "complement_fallback":
        warnings.append("NO row uses complement fallback — segment separately in evaluate")
    elif row.side == "NO" and src == "legacy_unlabeled":
        warnings.append("NO row legacy_unlabeled — rebuild dataset for p_market_source")

    if row.source == "mixed":
        warnings.append("mixed source — document provenance in extra metadata")

    return ValidationResult(ok=len(errors) == 0, warnings=warnings, errors=errors)


def dedupe_key(row: ResearchDatasetRowV1) -> tuple:
    return (row.dataset_version, row.market_id, row.token_id, row.side, row.decision_ts.isoformat())
