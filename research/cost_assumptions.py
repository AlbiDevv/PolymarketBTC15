"""
Versioned cost / EV assumptions for research.evaluate (NOT live execution).

EV in reports is a **research proxy**, not realized trading EV:
  ev_proxy = y - p_model - FLAT_FEE_PER_UNIT_NOTIONAL

Limitations (honest):
  - No bid-ask crossing model per row; p_market is mid- or policy-defined.
  - No per-trade slippage walk; see PaperBroker for execution simulation.
  - FLAT fee does not model Polymarket profit-only fee on exits.

Bump COST_ASSUMPTIONS_VERSION when changing formulas or defaults.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

COST_ASSUMPTIONS_VERSION = "ev_proxy_v1"

# Single flat fee subtracted in EV proxy (aligns with strategy.fee_rate default for comparability)
DEFAULT_FLAT_FEE = 0.02


@dataclass(frozen=True)
class CostAssumptions:
    version: str = COST_ASSUMPTIONS_VERSION
    flat_fee_per_unit: float = DEFAULT_FLAT_FEE
    doc: str = "ev_proxy = y - p - flat_fee (binary long, per share notional)"

    def to_dict(self) -> dict:
        return {
            "cost_assumptions_version": self.version,
            "flat_fee_per_unit": self.flat_fee_per_unit,
            "formula": "ev_proxy_i = y_i - p_i - flat_fee",
            "limitations": [
                "Research proxy only; not realized PnL from PaperBroker",
                "Does not include spread crossing or partial fills",
                "Polymarket charges fee on profit; flat fee is a deliberate simplification",
            ],
        }


DEFAULT_ASSUMPTIONS = CostAssumptions()


def ev_proxy_per_row(
    y: np.ndarray,
    p: np.ndarray,
    fee: float = DEFAULT_FLAT_FEE,
) -> np.ndarray:
    """Vectorized EV proxy for binary outcome y in {0,1}, probability p in (0,1)."""
    return y.astype(float) - p.astype(float) - fee
