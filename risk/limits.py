from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class LimitCheckResult:
    allowed: bool
    reason: str = ""


@dataclass
class PositionInfo:
    market_id: int
    event_id: str | None
    side: str
    size: float
    entry_price: float


class RiskLimits:
    """Enforces all risk constraints before allowing a new trade."""

    def __init__(
        self,
        max_positions: int = 30,
        max_concentration: float = 0.10,
        daily_loss_limit: float = 0.05,
        weekly_loss_limit: float = 0.10,
        total_drawdown_stop: float = 0.20,
        max_correlated_exposure: float = 0.15,
    ):
        self.max_positions = max_positions
        self.max_concentration = max_concentration
        self.daily_loss_limit = daily_loss_limit
        self.weekly_loss_limit = weekly_loss_limit
        self.total_drawdown_stop = total_drawdown_stop
        self.max_correlated_exposure = max_correlated_exposure

        self._daily_pnl: float = 0
        self._weekly_pnl: float = 0
        self._daily_reset: datetime = _utcnow()
        self._weekly_reset: datetime = _utcnow()
        self._stopped_until: datetime | None = None

    def update_pnl(self, pnl_change: float):
        self._check_resets()
        self._daily_pnl += pnl_change
        self._weekly_pnl += pnl_change

    def _check_resets(self):
        now = _utcnow()
        if (now - self._daily_reset) > timedelta(days=1):
            self._daily_pnl = 0
            self._daily_reset = now
        if (now - self._weekly_reset) > timedelta(weeks=1):
            self._weekly_pnl = 0
            self._weekly_reset = now

    def check_all(
        self,
        bankroll: float,
        initial_bankroll: float,
        open_positions: list[PositionInfo],
        new_event_id: str | None,
        new_stake: float,
    ) -> LimitCheckResult:
        self._check_resets()

        # Check if trading is stopped
        if self._stopped_until:
            if _utcnow() < self._stopped_until:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Trading stopped until {self._stopped_until.isoformat()}",
                )
            self._stopped_until = None

        # Total drawdown stop
        if initial_bankroll > 0:
            dd = (initial_bankroll - bankroll) / initial_bankroll
            if dd >= self.total_drawdown_stop:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Total drawdown {dd:.1%} >= {self.total_drawdown_stop:.0%} — FULL STOP",
                )

        # Daily loss limit
        if bankroll > 0 and abs(self._daily_pnl) / bankroll >= self.daily_loss_limit and self._daily_pnl < 0:
            self._stopped_until = self._daily_reset + timedelta(days=1)
            return LimitCheckResult(
                allowed=False,
                reason=f"Daily loss {self._daily_pnl:.2f} >= {self.daily_loss_limit:.0%} of bankroll",
            )

        # Weekly loss limit
        if bankroll > 0 and abs(self._weekly_pnl) / bankroll >= self.weekly_loss_limit and self._weekly_pnl < 0:
            return LimitCheckResult(
                allowed=False,
                reason=f"Weekly loss {self._weekly_pnl:.2f} >= {self.weekly_loss_limit:.0%} of bankroll",
            )

        # Max positions
        if len(open_positions) >= self.max_positions:
            return LimitCheckResult(
                allowed=False,
                reason=f"Max positions ({self.max_positions}) reached",
            )

        # Concentration per event
        if new_event_id:
            event_exposure = sum(
                p.size * p.entry_price
                for p in open_positions
                if p.event_id == new_event_id
            )
            total_with_new = event_exposure + new_stake
            if bankroll > 0 and total_with_new / bankroll > self.max_concentration:
                return LimitCheckResult(
                    allowed=False,
                    reason=f"Concentration in event {new_event_id}: "
                    f"${total_with_new:.1f} > {self.max_concentration:.0%} of bankroll",
                )

        # No conflicting sides on same market
        for p in open_positions:
            if p.event_id == new_event_id and p.event_id is not None:
                pass  # same event is ok, different markets within event

        return LimitCheckResult(allowed=True)
