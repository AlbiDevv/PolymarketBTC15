import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db.session as db_session
from config import Settings
from exchange_client.base import Market, Orderbook, OrderbookLevel, Token
from db.models import LabOrderRow, LabPortfolioRow, MarketRow
from db.session import get_session, init_db
from lab.runtime import ShadowLabRunner


def _reset_db():
    db_session._engine = None
    db_session._SessionFactory = None


def _book(bid: float = 0.40, ask: float = 0.42) -> Orderbook:
    return Orderbook(
        market_id="tok",
        bids=[OrderbookLevel(bid, 100.0)],
        asks=[OrderbookLevel(ask, 100.0)],
        timestamp=0.0,
    )


def test_ws_guardrail_freezes_on_low_health(tmp_path):
    settings = Settings()
    settings.database.url = f"sqlite:///{(tmp_path / 'guardrail.db').as_posix()}"
    _reset_db()
    runner = ShadowLabRunner(settings)
    try:
        assert runner._should_freeze_entries({"is_stale": False, "health_score": 0.2}) is True
        assert runner._should_freeze_entries({"is_stale": False, "health_score": 0.9}) is False
    finally:
        asyncio.run(runner._client.close())


def test_non_hard_exit_does_not_degrade_to_forced_taker(tmp_path):
    db_url = f"sqlite:///{(tmp_path / 'orders.db').as_posix()}"
    _reset_db()
    init_db(db_url)

    settings = Settings()
    settings.database.url = db_url
    settings.lab.ab_testing.enabled = False
    runner = ShadowLabRunner(settings)
    session = get_session(db_url)
    now = datetime.now(timezone.utc)
    try:
        portfolio_row = LabPortfolioRow(
            key="H2_base",
            mode="shadow_maker",
            initial_bankroll=500.0,
            settings_json={"portfolio": {"pack": "base", "track": "control", "hypotheses": ["H2"]}},
        )
        market_row = MarketRow(
            polymarket_id="m1",
            question="Q",
            category="politics",
            active=True,
        )
        session.add_all([portfolio_row, market_row])
        session.flush()

        runtime = runner._portfolio_by_key("H2_base")
        runtime.row_id = portfolio_row.id
        runner._portfolios_by_id[portfolio_row.id] = runtime

        runner._ob_manager.apply_snapshot("tok_yes", _book())
        order = runner._engine.create_order(
            portfolio_key=runtime.key,
            market_id="m1",
            market_db_id=market_row.id,
            token_id="tok_yes",
            event_id="e1",
            side="YES",
            action="SELL",
            price=0.41,
            size=4.0,
            queue_ahead=1.0,
            hypothesis="H2",
            edge=0.03,
            now=now,
            force_taker_allowed=False,
        )
        order.reason = "take_profit"
        order.reprices = settings.lab.execution.max_reprices
        order.expires_at = now
        runner._persist_new_order(session, runtime, order)
        session.commit()

        runner._manage_working_orders(session, now + timedelta(seconds=10))
        assert order.status == "expired"
        order_row = session.query(LabOrderRow).first()
        assert order_row is not None
        assert order_row.status == "expired"
        assert order_row.order_kind == "maker"
        assert order_row.close_reason == "expired"
    finally:
        session.close()
        asyncio.run(runner._client.close())


def test_ab_portfolios_are_built_as_control_and_learned(tmp_path):
    settings = Settings()
    settings.database.url = f"sqlite:///{(tmp_path / 'ab.db').as_posix()}"
    _reset_db()
    runner = ShadowLabRunner(settings)
    try:
        keys = {runtime.key for runtime in runner._portfolio_runtimes}
        assert "H2_base_control" in keys
        assert "H2_base_learned" in keys
        crypto_count = sum(1 for portfolio in settings.lab.portfolios if portfolio.track == "crypto15m")
        expected_count = (len(settings.lab.portfolios) - crypto_count) * 2 + crypto_count * (
            1 + len(settings.lab.crypto15m.ab_thresholds)
        )
        assert len(runner._portfolio_runtimes) == expected_count
        assert "Crypto15m_control" in keys
        assert "Crypto15m_t80_learned" in keys
        assert "Crypto15m_t70_learned" not in keys
        control = runner._portfolio_by_key("H2_base_control")
        learned = runner._portfolio_by_key("H2_base_learned")
        assert control is not None and control.config.use_learned_gate is False
        assert learned is not None and learned.config.use_learned_gate is True
    finally:
        asyncio.run(runner._client.close())


def test_control_group_bypasses_learned_gate(tmp_path):
    settings = Settings()
    settings.database.url = f"sqlite:///{(tmp_path / 'ab_gate.db').as_posix()}"
    _reset_db()
    runner = ShadowLabRunner(settings)
    try:
        runtime = runner._portfolio_by_key("H2_base_control")
        assert runtime is not None
        market = Market(
            id="m1",
            question="Will X happen?",
            category="crypto",
            end_date="2026-04-10T12:00:00+00:00",
            resolution_source="",
            active=True,
            volume_24h=1000.0,
            tokens=[
                Token(token_id="yes", outcome="Yes", price=0.95),
                Token(token_id="no", outcome="No", price=0.05),
            ],
            event_id="e1",
        )
        yes_book = Orderbook(
            market_id="yes",
            bids=[OrderbookLevel(0.94, 100.0)],
            asks=[OrderbookLevel(0.96, 100.0)],
            timestamp=0.0,
        )
        no_book = Orderbook(
            market_id="no",
            bids=[OrderbookLevel(0.04, 100.0)],
            asks=[OrderbookLevel(0.06, 100.0)],
            timestamp=0.0,
        )
        decision = runner._score_with_learned_gate(
            runtime,
            market,
            yes_book,
            no_book,
            side="YES",
            market_probability=0.95,
            external_data={"yes_mid": 0.95},
        )
        assert decision.enabled is False
        assert decision.should_veto is False
        assert decision.reason == "ab_control_bypass"
    finally:
        asyncio.run(runner._client.close())
