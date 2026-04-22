from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from research.trade_costs import polymarket_taker_fee_per_share


@dataclass
class EVResult:
    ev_yes: float
    ev_no: float
    edge_yes: float
    edge_no: float
    best_side: Literal["YES", "NO"]
    best_edge: float
    recommended: bool  # True if best_edge >= threshold


def calculate_ev(
    p_model: float,
    price_ask: float,
    price_bid: float,
    fee: float = 0.02,
    edge_threshold: float = 0.05,
    *,
    no_ask: float | None = None,
) -> EVResult:
    """
    Calculate Expected Value for both sides of a binary contract.

    Args:
        p_model: Model's estimated probability of YES outcome.
        price_ask: Current ask price (cost to buy YES).
        price_bid: Current bid price on the YES token book.
        fee: Platform fee (Polymarket: ~2% of profit).
        edge_threshold: Minimum edge to recommend a trade.
        no_ask: If set, cost to buy NO from the NO token orderbook (native).

    When ``no_ask`` is omitted, NO entry cost defaults to ``1 - price_bid``
    (complement of the YES bid), which matches a single YES-only book.

    EV formula for YES at ask price:
        EV(YES) = p - ask - fee_per_share

    EV formula for NO (buy NO token at ``no_ask``):
        EV(NO) = (1-p) - no_ask - fee_per_share
    """
    q = 1 - p_model

    # Buy YES at ask price
    yes_fee = polymarket_taker_fee_per_share(price_ask, fee_rate=fee)
    ev_yes = p_model - price_ask - yes_fee
    edge_yes = ev_yes  # normalized per $1 stake

    # Buy NO: prefer native NO-token ask; else derive from YES bid
    if no_ask is not None:
        no_cost = no_ask
    else:
        no_cost = 1 - price_bid
    no_fee = polymarket_taker_fee_per_share(no_cost, fee_rate=fee)
    ev_no = q - no_cost - no_fee
    edge_no = ev_no

    if edge_yes >= edge_no:
        best_side: Literal["YES", "NO"] = "YES"
        best_edge = edge_yes
    else:
        best_side = "NO"
        best_edge = edge_no

    return EVResult(
        ev_yes=ev_yes,
        ev_no=ev_no,
        edge_yes=edge_yes,
        edge_no=edge_no,
        best_side=best_side,
        best_edge=best_edge,
        recommended=best_edge >= edge_threshold,
    )
