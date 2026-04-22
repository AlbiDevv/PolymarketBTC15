from __future__ import annotations

from loguru import logger

from config import Settings
from models.ev import calculate_ev, EVResult
from models.kelly import kelly_stake_with_drawdown_adjustment
from .limits import RiskLimits, LimitCheckResult, PositionInfo


class RiskManager:
    """
    Combines EV calculation, Kelly sizing, and risk limits
    into a single decision: trade or skip.
    """

    def __init__(self, settings: Settings, initial_bankroll: float):
        self._settings = settings
        self._initial_bankroll = initial_bankroll
        self._limits = RiskLimits(
            max_positions=settings.risk.max_positions,
            max_concentration=settings.risk.max_concentration,
            daily_loss_limit=settings.risk.daily_loss_limit,
            weekly_loss_limit=settings.risk.weekly_loss_limit,
            total_drawdown_stop=settings.risk.total_drawdown_stop,
            max_correlated_exposure=settings.risk.max_correlated_exposure,
        )

    def evaluate_trade(
        self,
        p_model: float,
        price_ask: float,
        price_bid: float,
        bankroll: float,
        open_positions: list[PositionInfo],
        event_id: str | None = None,
        fee: float = 0.02,
        no_ask: float | None = None,
    ) -> TradeDecision:
        ev = calculate_ev(
            p_model=p_model,
            price_ask=price_ask,
            price_bid=price_bid,
            fee=fee,
            edge_threshold=self._settings.strategy.edge_threshold,
            no_ask=no_ask,
        )

        if not ev.recommended:
            return TradeDecision(
                action="skip",
                reason=f"Edge {ev.best_edge:.3f} < threshold {self._settings.strategy.edge_threshold}",
                ev=ev,
            )

        # Kelly sizing — native NO-token ask when available
        if ev.best_side == "YES":
            price = price_ask
        elif no_ask is not None:
            price = no_ask
        else:
            price = 1 - price_bid
        p = p_model if ev.best_side == "YES" else (1 - p_model)

        stake = kelly_stake_with_drawdown_adjustment(
            p=p,
            price=price,
            bankroll=bankroll,
            initial_bankroll=self._initial_bankroll,
            k_base=self._settings.strategy.kelly_fraction,
            stake_min=self._settings.strategy.stake_min,
            stake_max=self._settings.strategy.stake_max,
        )

        if stake <= 0:
            return TradeDecision(
                action="skip",
                reason="Kelly stake = 0 (no edge or drawdown stop)",
                ev=ev,
            )

        # Risk limits
        limit_check = self._limits.check_all(
            bankroll=bankroll,
            initial_bankroll=self._initial_bankroll,
            open_positions=open_positions,
            new_event_id=event_id,
            new_stake=stake,
        )

        if not limit_check.allowed:
            return TradeDecision(
                action="skip",
                reason=f"Risk limit: {limit_check.reason}",
                ev=ev,
                stake=stake,
            )

        return TradeDecision(
            action="trade",
            side=ev.best_side,
            stake=stake,
            price=price,
            ev=ev,
        )

    def record_pnl(self, pnl_change: float):
        self._limits.update_pnl(pnl_change)


class TradeDecision:
    def __init__(
        self,
        action: str,
        reason: str = "",
        side: str = "",
        stake: float = 0,
        price: float = 0,
        ev: EVResult | None = None,
    ):
        self.action = action
        self.reason = reason
        self.side = side
        self.stake = stake
        self.price = price
        self.ev = ev

    def __repr__(self):
        if self.action == "trade":
            return (
                f"TradeDecision(TRADE {self.side} stake=${self.stake:.2f} "
                f"@ {self.price:.3f}, edge={self.ev.best_edge:.3f})"
            )
        return f"TradeDecision(SKIP: {self.reason})"
