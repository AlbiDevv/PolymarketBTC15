"""
Formal definitions for research dataset v1: p_market (side-aware) and target.

These functions are the single source of truth for labeling — not ad-hoc notebook logic.
"""

from __future__ import annotations

from typing import Literal

Outcome = Literal["YES", "NO"]
Side = Literal["YES", "NO"]


def resolved_outcome_for_side(side: Side, final_outcome: Outcome) -> int:
    """
    Target for the chosen side (one row = one side at decision time).

    - YES row: 1 iff the market resolved YES.
    - NO row: 1 iff the market resolved NO.

    Never use the YES outcome label directly as target for a NO row without transformation.
    """
    if side == "YES":
        return 1 if final_outcome == "YES" else 0
    return 1 if final_outcome == "NO" else 0


def p_market_from_token_mid(yes_mid: float | None, no_mid: float | None, side: Side) -> float | None:
    """
    Preferred v1 policy: p_market = mid on the **native** token book for that side.

    Pass the mid price already computed for the YES book and NO book respectively.
    Returns None if the required mid is missing.
    """
    if side == "YES":
        return yes_mid if yes_mid is not None else None
    return no_mid if no_mid is not None else None


def p_market_fallback_no_from_yes_complement(
    yes_mid: float | None,
) -> float | None:
    """
    Fallback only when NO-token book is unavailable: p_NO ≈ 1 - p_YES (mid).
    Must set execution_price_assumption / source metadata to document fallback use.
    """
    if yes_mid is None:
        return None
    return max(0.0, min(1.0, 1.0 - yes_mid))


def clip_probability(p: float, lo: float = 0.01, hi: float = 0.99) -> float:
    return max(lo, min(hi, p))
