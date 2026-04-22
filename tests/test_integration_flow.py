"""
Integration-style tests: full signal→risk→execution→position flow.

Uses PaperBroker with a real Orderbook to verify end-to-end consistency.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncio

from exchange_client.base import Orderbook, OrderbookLevel
from execution.broker import TradeIntent, FillResult
from execution.paper_broker import PaperBroker, PaperBrokerConfig
from execution.live_broker import DryRunBroker
from models.ev import calculate_ev
from models.kelly import kelly_stake
from risk.limits import RiskLimits, PositionInfo


def _run(coro):
    return asyncio.run(coro)


def _realistic_ob() -> Orderbook:
    """Realistic Polymarket orderbook with multiple levels."""
    return Orderbook(
        market_id="test_market",
        bids=[
            OrderbookLevel(0.52, 500),
            OrderbookLevel(0.51, 800),
            OrderbookLevel(0.50, 1200),
            OrderbookLevel(0.49, 2000),
        ],
        asks=[
            OrderbookLevel(0.54, 500),
            OrderbookLevel(0.55, 800),
            OrderbookLevel(0.56, 1200),
            OrderbookLevel(0.57, 2000),
        ],
        timestamp=1000.0,
    )


class TestFullTradingFlow:
    """Signal → EV → Kelly → Risk → Broker → FillResult."""

    def test_yes_trade_flow(self):
        ob = _realistic_ob()

        # 1. Signal: model thinks YES is 65% likely
        p_model = 0.65
        ev = calculate_ev(
            p_model=p_model,
            price_ask=ob.best_ask,
            price_bid=ob.best_bid,
            fee=0.02,
            edge_threshold=0.05,
        )
        assert ev.recommended
        assert ev.best_side == "YES"

        # 2. Kelly sizing
        stake = kelly_stake(
            p=p_model, price=ob.best_ask,
            bankroll=500, k=0.25,
            stake_min=1.0, stake_max=10.0,
        )
        assert stake > 0

        # 3. Risk check
        limits = RiskLimits()
        check = limits.check_all(
            bankroll=500, initial_bankroll=500,
            open_positions=[], new_event_id="evt_1", new_stake=stake,
        )
        assert check.allowed

        # 4. Build intent with correct units
        contracts = stake / ob.best_ask
        intent = TradeIntent(
            market_id="1", condition_id="cond_1", token_id="tok_YES",
            side="YES", price=ob.best_ask, stake_usd=stake, contracts=contracts,
        )

        # 5. Execute via paper broker
        broker = PaperBroker(PaperBrokerConfig(latency_levels=0, fee_rate=0.02))
        fill = _run(broker.execute(intent, ob))

        assert fill.status in ("PAPER_FILL", "PARTIAL")
        assert fill.filled_contracts > 0
        assert fill.avg_fill_price >= ob.best_ask  # no negative slippage
        assert fill.fees_usd >= 0

    def test_no_trade_flow(self):
        ob = _realistic_ob()

        # Model thinks NO is more likely
        p_model = 0.30
        ev = calculate_ev(
            p_model=p_model,
            price_ask=ob.best_ask,
            price_bid=ob.best_bid,
            fee=0.02,
            edge_threshold=0.05,
        )
        assert ev.best_side == "NO"

        # NO token cost = 1 - best_bid
        no_price = 1 - ob.best_bid
        stake = kelly_stake(
            p=1 - p_model,  # prob of NO winning
            price=no_price,
            bankroll=500, k=0.25,
            stake_min=1.0, stake_max=10.0,
        )

        if stake > 0:
            contracts = stake / no_price
            intent = TradeIntent(
                market_id="1", condition_id="cond_1", token_id="tok_NO",
                side="NO", price=no_price, stake_usd=stake, contracts=contracts,
            )

            broker = PaperBroker(PaperBrokerConfig(latency_levels=0))
            fill = _run(broker.execute(intent, ob))

            # NO side walks bids; should work
            assert fill.order_id.startswith("paper_")

    def test_risk_blocks_when_max_positions(self):
        """Risk limits prevent trade even with good signal."""
        limits = RiskLimits(max_positions=2)
        positions = [
            PositionInfo(market_id=i, event_id=f"e{i}", side="YES", size=10, entry_price=0.50)
            for i in range(2)
        ]
        check = limits.check_all(
            bankroll=500, initial_bankroll=500,
            open_positions=positions, new_event_id="e3", new_stake=5.0,
        )
        assert not check.allowed

    def test_dry_run_simulates_fill_for_pipeline(self):
        broker = DryRunBroker()
        ob = _realistic_ob()
        intent = TradeIntent(
            market_id="1", condition_id="c1", token_id="t1",
            side="YES", price=0.54, stake_usd=10.0, contracts=18.5,
        )
        fill = _run(broker.execute(intent, ob))

        assert fill.status == "PAPER_FILL"
        assert fill.filled_contracts > 0
        assert fill.avg_fill_price > 0

    def test_mode_switch_same_strategy(self):
        """Same intent, different brokers — verifies strategy/execution separation."""
        ob = _realistic_ob()
        intent = TradeIntent(
            market_id="1", condition_id="c1", token_id="t1",
            side="YES", price=0.55, stake_usd=10.0, contracts=18.2,
        )

        dry = DryRunBroker()
        paper = PaperBroker(PaperBrokerConfig(latency_levels=0))

        r_dry = _run(dry.execute(intent, ob))
        r_paper = _run(paper.execute(intent, ob))

        assert r_dry.status == "PAPER_FILL"
        assert r_dry.filled_contracts > 0

        assert r_paper.status in ("PAPER_FILL", "PARTIAL")
        assert r_paper.filled_contracts > 0
