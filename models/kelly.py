from __future__ import annotations


def kelly_stake(
    p: float,
    price: float,
    bankroll: float,
    k: float = 0.25,
    stake_min: float = 1.0,
    stake_max: float = 2.0,
) -> float:
    """
    Fractional Kelly criterion for binary contracts.

    Args:
        p: Model's probability of winning.
        price: Contract price (cost per share).
        bankroll: Current bankroll.
        k: Kelly scaling factor (0.25 = quarter Kelly, conservative).
        stake_min: Minimum stake (below this — don't trade).
        stake_max: Maximum stake per trade.

    Returns:
        Recommended stake in dollars. Returns 0 if no edge.

    Kelly formula:
        f = (b*p - q) / b
        where b = (1 - price) / price   (payout odds)
              q = 1 - p
        stake = f * bankroll * k
    """
    if price <= 0 or price >= 1 or p <= 0 or p >= 1:
        return 0.0

    b = (1 - price) / price  # odds
    q = 1 - p
    f = (b * p - q) / b

    if f <= 0:
        return 0.0

    raw_stake = f * bankroll * k
    raw_stake = min(raw_stake, stake_max)

    if raw_stake < stake_min:
        return 0.0

    return round(raw_stake, 2)


def kelly_stake_with_drawdown_adjustment(
    p: float,
    price: float,
    bankroll: float,
    initial_bankroll: float,
    k_base: float = 0.25,
    stake_min: float = 1.0,
    stake_max: float = 2.0,
) -> float:
    """
    Kelly with automatic drawdown-based scaling.
    When in drawdown > 10%, reduce k proportionally.
    """
    drawdown = (initial_bankroll - bankroll) / initial_bankroll if initial_bankroll > 0 else 0

    if drawdown > 0.20:
        return 0.0  # total stop

    if drawdown > 0.10:
        # Scale k down linearly: at 10% DD → full k, at 20% DD → 0
        scale = max(0, 1 - (drawdown - 0.10) / 0.10)
        k_adjusted = k_base * scale
    else:
        k_adjusted = k_base

    return kelly_stake(
        p=p,
        price=price,
        bankroll=bankroll,
        k=k_adjusted,
        stake_min=stake_min,
        stake_max=stake_max,
    )
