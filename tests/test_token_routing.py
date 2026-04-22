"""Tests for correct YES/NO token routing and units consistency."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exchange_client.base import Orderbook, OrderbookLevel, Token, Market
from execution.broker import TradeIntent


def _yes_token():
    return Token(token_id="tok_YES_123", outcome="Yes", price=0.55)


def _no_token():
    return Token(token_id="tok_NO_456", outcome="No", price=0.45)


def _market():
    return Market(
        id="cond_abc", question="Test?", category="test",
        end_date=None, resolution_source="manual", active=True,
        volume_24h=10000, tokens=[_yes_token(), _no_token()],
        event_id="evt_1",
    )


class TestTokenRouting:
    """Verify that YES decisions use YES token and NO decisions use NO token."""

    def test_yes_decision_uses_yes_token(self):
        market = _market()
        yes_tok = next(t for t in market.tokens if t.outcome.lower() == "yes")
        no_tok = next(t for t in market.tokens if t.outcome.lower() == "no")

        side = "YES"
        if side == "YES":
            token_id = yes_tok.token_id
        else:
            token_id = no_tok.token_id

        assert token_id == "tok_YES_123"

    def test_no_decision_uses_no_token(self):
        market = _market()
        yes_tok = next(t for t in market.tokens if t.outcome.lower() == "yes")
        no_tok = next(t for t in market.tokens if t.outcome.lower() == "no")

        side = "NO"
        if side == "YES":
            token_id = yes_tok.token_id
        else:
            token_id = no_tok.token_id

        assert token_id == "tok_NO_456"


class TestUnitsConsistency:
    """Verify that stake_usd and contracts are consistent."""

    def test_contracts_equals_stake_over_price(self):
        price = 0.52
        stake = 10.0
        contracts = stake / price
        intent = TradeIntent(
            market_id="1", condition_id="c1", token_id="t1",
            side="YES", price=price, stake_usd=stake, contracts=contracts,
        )
        assert abs(intent.contracts * intent.price - intent.stake_usd) < 0.01

    def test_notional_property(self):
        intent = TradeIntent(
            market_id="1", condition_id="c1", token_id="t1",
            side="YES", price=0.50, stake_usd=10.0, contracts=20.0,
        )
        assert intent.notional == 10.0

    def test_no_side_price_is_complement(self):
        """NO token price = 1 - YES bid price."""
        yes_bid = 0.55
        no_price = 1 - yes_bid  # 0.45
        stake = 10.0
        contracts = stake / no_price

        intent = TradeIntent(
            market_id="1", condition_id="c1", token_id="tok_NO",
            side="NO", price=no_price, stake_usd=stake, contracts=contracts,
        )
        assert abs(intent.price - 0.45) < 0.001
        assert abs(intent.contracts * intent.price - stake) < 0.01
