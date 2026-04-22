from __future__ import annotations

from typing import Any


def coerce_bool(value: Any, *, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    rendered = str(value).strip().lower()
    if rendered in {"true", "1", "yes", "y"}:
        return True
    if rendered in {"false", "0", "no", "n"}:
        return False
    return default


def coerce_fee_rate(value: Any, *, default: float) -> float:
    if value in (None, "", "nan"):
        return default
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return default
    if rate > 1.0:
        return rate / 10000.0
    return max(0.0, rate)


def fee_rate_bps_to_fraction(value: Any, *, default_bps: float = 0.0) -> float:
    try:
        bps = float(value)
    except (TypeError, ValueError):
        bps = float(default_bps)
    return max(0.0, bps) / 10000.0


def polymarket_taker_fee_per_share(
    price: float,
    *,
    fee_rate: float,
    fees_enabled: bool = True,
) -> float:
    if not fees_enabled:
        return 0.0
    p = max(0.0, min(1.0, float(price)))
    return max(0.0, float(fee_rate)) * p * (1.0 - p)


def polymarket_taker_fee_usdc(
    contracts: float,
    price: float,
    *,
    fee_rate_bps: float,
    fees_enabled: bool = True,
) -> float:
    return max(0.0, float(contracts)) * polymarket_taker_fee_per_share(
        price,
        fee_rate=fee_rate_bps_to_fraction(fee_rate_bps),
        fees_enabled=fees_enabled,
    )


def net_ev_per_share(
    *,
    win_probability: float,
    entry_price: float,
    fee_rate: float,
    fees_enabled: bool = True,
    slippage: float = 0.0,
) -> float:
    p_win = max(0.0, min(1.0, float(win_probability)))
    price = max(0.0, min(1.0, float(entry_price)))
    fee = polymarket_taker_fee_per_share(price, fee_rate=fee_rate, fees_enabled=fees_enabled)
    return p_win - price - fee - max(0.0, float(slippage))
