from __future__ import annotations

from dataclasses import dataclass

from loguru import logger

from config import LiquidityConfig
from .base import Market, Orderbook


@dataclass
class LiquidityCheck:
    passed: bool
    reason: str = ""
    volume_24h: float = 0
    bid_depth: float = 0
    ask_depth: float = 0
    spread: float = 0
    price_impact: float = 0


class LiquidityFilter:
    def __init__(self, cfg: LiquidityConfig):
        self._cfg = cfg

    def check_market(self, market: Market) -> LiquidityCheck:
        if market.volume_24h < self._cfg.min_daily_volume:
            return LiquidityCheck(
                passed=False,
                reason=f"Volume ${market.volume_24h:.0f} < ${self._cfg.min_daily_volume:.0f}",
                volume_24h=market.volume_24h,
            )
        return LiquidityCheck(passed=True, volume_24h=market.volume_24h)

    def check_orderbook(
        self, ob: Orderbook, intended_size: float
    ) -> LiquidityCheck:
        bid_depth = ob.depth("bid", pct_from_mid=0.02)
        ask_depth = ob.depth("ask", pct_from_mid=0.02)
        spread = ob.spread

        if bid_depth < self._cfg.min_depth_usd:
            return LiquidityCheck(
                passed=False,
                reason=f"Bid depth ${bid_depth:.1f} < ${self._cfg.min_depth_usd}",
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                spread=spread,
            )

        if ask_depth < self._cfg.min_depth_usd:
            return LiquidityCheck(
                passed=False,
                reason=f"Ask depth ${ask_depth:.1f} < ${self._cfg.min_depth_usd}",
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                spread=spread,
            )

        impact = self._estimate_price_impact(ob, intended_size)
        if impact > self._cfg.max_price_impact:
            return LiquidityCheck(
                passed=False,
                reason=f"Price impact {impact:.3f} > {self._cfg.max_price_impact}",
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                spread=spread,
                price_impact=impact,
            )

        return LiquidityCheck(
            passed=True,
            bid_depth=bid_depth,
            ask_depth=ask_depth,
            spread=spread,
            price_impact=impact,
        )

    @staticmethod
    def _estimate_price_impact(ob: Orderbook, size: float) -> float:
        """Estimate how much mid-price would move after a buy of *size* contracts."""
        if not ob.asks or ob.mid_price == 0:
            return 1.0

        remaining = size
        vwap_num = 0.0
        for lvl in ob.asks:
            fill = min(remaining, lvl.size)
            vwap_num += fill * lvl.price
            remaining -= fill
            if remaining <= 0:
                break

        if remaining > 0:
            return 1.0  # not enough liquidity at all

        vwap = vwap_num / size
        return abs(vwap - ob.mid_price) / ob.mid_price
