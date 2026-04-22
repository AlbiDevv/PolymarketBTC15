import sys
import asyncio
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LabPortfolioConfig, Settings
from exchange_client.base import Market, Orderbook, OrderbookLevel, Token
from lab.crypto_ohlcv_live import CryptoOHLCVLiveFeed
from lab.runtime import ShadowLabRunner
from lab.utils import portfolio_settings
from models.hypothesis import H7_Crypto15mDirection, SignalOutput
from lab.market_quality import MarketQualityAssessment
from research.crypto15m import normalize_ohlcv_rows
from db.models import (
    LabEquityPointRow,
    LabFillRow,
    LabOrderRow,
    LabPortfolioRow,
    LabPositionRow,
    MarketRow,
)
from db.session import get_session, init_db


def _crypto_portfolio() -> LabPortfolioConfig:
    return LabPortfolioConfig(
        key="Crypto15m",
        hypotheses=["H7"],
        pack="crypto15m",
        track="crypto15m",
        max_horizon_days=1,
        min_daily_volume=100,
        min_depth_usd=100,
        max_spread=0.04,
        min_quality_score=55,
        time_to_resolution_max_hours=2,
    )


def _crypto_market(
    market_id: str = "crypto1",
    *,
    minutes_left: int = 20,
    question: str = "Bitcoin Up or Down - April 13, 7:15AM-7:30AM ET",
) -> Market:
    return Market(
        id=market_id,
        question=question,
        category="Crypto",
        end_date=(datetime.now(timezone.utc) + timedelta(minutes=minutes_left)).isoformat(),
        resolution_source="",
        active=True,
        volume_24h=1000,
        tokens=[Token("yes", "Up", 0.5), Token("no", "Down", 0.5)],
    )


def _book(spread: float = 0.02, bid_size: float = 500.0, ask_size: float = 50.0) -> Orderbook:
    return Orderbook(
        market_id="tok",
        bids=[OrderbookLevel(0.49, bid_size)],
        asks=[OrderbookLevel(0.49 + spread, ask_size)],
        timestamp=0,
    )


def test_crypto15m_ab_expansion_creates_control_and_threshold_variants():
    settings = Settings()
    settings.lab.ab_testing.enabled = True
    settings.lab.crypto15m.ab_thresholds = [0.70, 0.80, 0.90, 0.95]
    settings.lab.crypto15m.shadow_stake_max = 2.0
    variants = ShadowLabRunner._expand_portfolio_variants(settings, _crypto_portfolio())

    assert [variant.key for variant in variants] == [
        "Crypto15m_control",
        "Crypto15m_t70_learned",
        "Crypto15m_t80_learned",
        "Crypto15m_t90_learned",
        "Crypto15m_t95_learned",
    ]
    assert variants[0].use_learned_gate is False
    assert variants[0].crypto15m_confidence_threshold == 0.55
    assert [variant.crypto15m_confidence_threshold for variant in variants[1:]] == [0.70, 0.80, 0.90, 0.95]
    assert all(variant.stake_max_override == 2.0 for variant in variants)


def test_crypto15m_default_live_thresholds_disable_t70():
    settings = Settings()
    settings.lab.ab_testing.enabled = True
    variants = ShadowLabRunner._expand_portfolio_variants(settings, _crypto_portfolio())

    assert [variant.key for variant in variants] == [
        "Crypto15m_control",
        "Crypto15m_t80_learned",
        "Crypto15m_t90_learned",
        "Crypto15m_t95_learned",
    ]
    assert [variant.crypto15m_confidence_threshold for variant in variants[1:]] == [0.80, 0.90, 0.95]


def test_crypto15m_ab_expansion_adds_analyst_variants_when_enabled():
    settings = Settings()
    settings.lab.ab_testing.enabled = True
    settings.lab.crypto15m.ai_analyst.enabled = True
    settings.lab.crypto15m.ab_thresholds = [0.65, 0.80]
    variants = ShadowLabRunner._expand_portfolio_variants(settings, _crypto_portfolio())

    assert [variant.key for variant in variants] == [
        "Crypto15m_control",
        "Crypto15m_t65_learned",
        "Crypto15m_t65_analyst",
        "Crypto15m_t80_learned",
        "Crypto15m_t80_analyst",
    ]
    assert [variant.ab_group for variant in variants] == [
        "control",
        "learned_t65",
        "analyst_t65",
        "learned_t80",
        "analyst_t80",
    ]
    assert variants[2].use_ai_analyst is True
    assert variants[1].use_ai_analyst is False


def test_crypto15m_ab_expansion_can_run_low_thresholds_under_analyst_only():
    settings = Settings()
    settings.lab.ab_testing.enabled = True
    settings.lab.crypto15m.ai_analyst.enabled = True
    settings.lab.crypto15m.ai_analyst.analyst_only_below_confidence = 0.80
    settings.lab.crypto15m.ab_thresholds = [0.65, 0.70, 0.80]
    variants = ShadowLabRunner._expand_portfolio_variants(settings, _crypto_portfolio())

    assert [variant.key for variant in variants] == [
        "Crypto15m_control",
        "Crypto15m_t65_analyst",
        "Crypto15m_t70_analyst",
        "Crypto15m_t80_learned",
        "Crypto15m_t80_analyst",
    ]
    assert all(variant.use_ai_analyst for variant in variants[1:3])
    assert variants[3].use_ai_analyst is False


def test_portfolio_settings_apply_crypto_threshold_and_stake_override():
    settings = Settings()
    portfolio = _crypto_portfolio()
    portfolio.crypto15m_confidence_threshold = 0.80
    portfolio.stake_max_override = 5.0
    scoped = portfolio_settings(settings, portfolio)

    assert scoped.strategy.crypto15m_model.min_confidence == 0.80
    assert scoped.lab.crypto15m.min_confidence == 0.80
    assert scoped.strategy.stake_max == 5.0


def test_prepare_signal_external_data_keeps_model_confidence_floor_with_ai_veto():
    settings = Settings()
    settings.lab.ab_testing.enabled = True
    settings.lab.crypto15m.ai_analyst.enabled = True
    settings.lab.crypto15m.ai_analyst.min_signal_confidence = 0.60
    runner = ShadowLabRunner(settings)
    runtime = ShadowLabRunner._build_portfolios(settings)[0]
    runtime.config.track = "crypto15m"
    runtime.config.use_ai_analyst = True
    runtime.settings.lab.crypto15m.min_confidence = 0.80
    try:
        external = runner._prepare_signal_external_data(
            runtime,
            _crypto_market(),
            _book(),
            _book(),
            datetime.now(timezone.utc),
            base_external_data={"crypto15m_is_market": True},
        )
        assert external["crypto15m_min_confidence"] == 0.80
        assert external["crypto15m_relax_momentum_gate"] is False
        assert external["crypto15m_relax_regime_gates"] is False
    finally:
        asyncio.run(runner._client.close())


def test_crypto15m_pre_signal_filter_skips_orderbook_depth_until_side_is_known():
    portfolio = _crypto_portfolio()
    market = _crypto_market()
    shallow_ask_book = _book(bid_size=500.0, ask_size=10.0)

    assert ShadowLabRunner._market_passes_portfolio_filters(
        portfolio,
        market,
        shallow_ask_book,
        check_orderbook=False,
    ) is True
    assert ShadowLabRunner._market_passes_portfolio_filters(
        portfolio,
        market,
        shallow_ask_book,
        check_orderbook=True,
    ) is False


def test_crypto15m_slug_discovery_fetches_current_and_future_updown_markets(tmp_path):
    async def run():
        settings = Settings()
        settings.database.url = f"sqlite:///{(tmp_path / 'lab.db').as_posix()}"
        settings.lab.crypto15m.trade_assets = ["BTC", "ETH"]
        settings.lab.crypto15m.slug_discovery_behind_intervals = 0
        settings.lab.crypto15m.slug_discovery_ahead_intervals = 1
        settings.lab.crypto15m.slug_discovery_concurrency = 2
        runner = ShadowLabRunner(settings)
        calls = []

        async def fake_get_market_by_slug(slug: str):
            calls.append(slug)
            if slug.startswith("btc-"):
                return _crypto_market(slug)
            return None

        runner._client.get_market_by_slug = fake_get_market_by_slug
        try:
            now = datetime(2026, 4, 13, 12, 5, tzinfo=timezone.utc)
            markets = await runner._fetch_crypto15m_slug_markets(now)
        finally:
            await runner._client.close()
        return calls, markets

    calls, markets = asyncio.run(run())

    assert calls == [
        "btc-updown-15m-1776081600",
        "eth-updown-15m-1776081600",
        "btc-updown-15m-1776082500",
        "eth-updown-15m-1776082500",
    ]
    assert [market.id for market in markets] == [
        "btc-updown-15m-1776081600",
        "btc-updown-15m-1776082500",
    ]


def test_crypto15m_default_slug_discovery_is_btc_only(tmp_path):
    async def run():
        settings = Settings()
        settings.database.url = f"sqlite:///{(tmp_path / 'lab.db').as_posix()}"
        settings.lab.crypto15m.slug_discovery_behind_intervals = 0
        settings.lab.crypto15m.slug_discovery_ahead_intervals = 1
        runner = ShadowLabRunner(settings)
        calls = []

        async def fake_get_market_by_slug(slug: str):
            calls.append(slug)
            return _crypto_market(slug)

        runner._client.get_market_by_slug = fake_get_market_by_slug
        try:
            await runner._fetch_crypto15m_slug_markets(datetime(2026, 4, 13, 12, 5, tzinfo=timezone.utc))
        finally:
            await runner._client.close()
        return calls

    calls = asyncio.run(run())

    assert calls == [
        "btc-updown-15m-1776081600",
        "btc-updown-15m-1776082500",
    ]
    assert all("eth-updown" not in slug for slug in calls)


def test_crypto15m_eth_market_is_disabled_by_default(tmp_path):
    settings = Settings()
    settings.database.url = f"sqlite:///{(tmp_path / 'lab.db').as_posix()}"
    runner = ShadowLabRunner(settings)
    try:
        eth_market = _crypto_market(
            "eth1",
            question="Ethereum Up or Down - April 13, 7:15AM-7:30AM ET",
        )

        assert runner._market_is_crypto15m_eligible(eth_market, datetime.now(timezone.utc)) is False
    finally:
        asyncio.run(runner._client.close())


def test_live_ohlcv_snapshot_features_and_stale_detection():
    settings = Settings()
    settings.lab.crypto15m.live_ohlcv_stale_sec = 90
    feed = CryptoOHLCVLiveFeed(settings)
    rows = [
        [1_700_000_000_000 + i * 60_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i]
        for i in range(70)
    ]
    frame = normalize_ohlcv_rows(rows, exchange_id="binance", symbol="BTC/USDT", timeframe="1m")
    feed._frames["BTC/USDT"] = frame
    feed._exchange_by_symbol["BTC/USDT"] = "binance"

    latest = frame.iloc[-1]["timestamp"].to_pydatetime()
    fresh = feed.feature_snapshot("BTC/USDT", at=latest, now=latest + timedelta(seconds=30))
    stale = feed.feature_snapshot("BTC/USDT", at=latest, now=latest + timedelta(seconds=180))

    assert fresh.fresh is True
    assert fresh.exchange_id == "binance"
    assert fresh.features["ret_5m"] > 0
    assert stale.fresh is False
    assert stale.features == {}


def test_crypto15m_taker_entry_uses_fee_adjusted_signal_edge_without_double_subtract(tmp_path):
    settings = Settings()
    settings.database.url = f"sqlite:///{(tmp_path / 'lab.db').as_posix()}"
    settings.lab.crypto15m.min_net_ev = 0.003
    runner = ShadowLabRunner(settings)
    runtime = ShadowLabRunner._build_portfolios(settings)[0]
    runtime.config.track = "crypto15m"
    runtime.config.min_depth_usd = 100
    market = Market(
        id="m1",
        question="Bitcoin Up or Down - 15 minutes",
        category="crypto",
        end_date=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        resolution_source="",
        active=True,
        volume_24h=1000,
        tokens=[Token("yes", "Yes", 0.5), Token("no", "No", 0.5)],
    )
    book = Orderbook(
        market_id="yes",
        bids=[OrderbookLevel(0.50, 1000)],
        asks=[OrderbookLevel(0.54, 1000)],
        timestamp=0,
    )
    signal = SignalOutput(
        hypothesis_id="H7",
        market_id="m1",
        side="YES",
        model_probability=0.60,
        market_probability=0.52,
        edge=0.004,
        confidence=0.90,
        metadata={"expected_net_ev": 0.004},
    )
    try:
        assert runner._should_use_taker_entry(
            runtime,
            market,
            book,
            signal,
            MarketQualityAssessment(score=90, bid_depth=1000, ask_depth=1000),
            datetime.now(timezone.utc),
        ) is True
    finally:
        asyncio.run(runner._client.close())


def test_crypto15m_reward_guard_blocks_stop_loss_streak(tmp_path):
    db_path = tmp_path / "lab.db"
    settings = Settings()
    settings.database.url = f"sqlite:///{db_path.as_posix()}"
    settings.lab.crypto15m.reward_guard_enabled = True
    settings.lab.crypto15m.reward_stop_streak_limit = 2
    init_db(settings.database.url)
    runner = ShadowLabRunner(settings)
    runtime = ShadowLabRunner._build_portfolios(settings)[0]
    runtime.row_id = 1
    runtime.config.track = "crypto15m"
    runtime.bankroll = 1500
    now = datetime.now(timezone.utc)
    session = get_session(settings.database.url)
    try:
        session.add(LabEquityPointRow(
            portfolio_id=1,
            timestamp=now,
            bankroll=1500,
            realized_pnl=0,
            unrealized_pnl=0,
            equity=1500,
            drawdown_pct=0,
        ))
        session.add_all([
            LabPositionRow(
                portfolio_id=1,
                market_id=idx,
                token_id=f"tok{idx}",
                event_id=f"event{idx}",
                side="YES",
                strategy_key="Crypto15m_t80_learned",
                hypothesis="H7",
                entry_price=0.5,
                current_price=0.49,
                size=10.0,
                opened_at=now - timedelta(minutes=10 + idx),
                closed_at=now - timedelta(minutes=idx),
                status="closed",
                exit_reason="stop_loss",
                pnl=-0.1,
                realized_pnl=-0.1,
            )
            for idx in (1, 2)
        ])
        session.commit()
        signal = SignalOutput(
            hypothesis_id="H7",
            market_id="m1",
            side="YES",
            model_probability=0.60,
            market_probability=0.52,
            edge=0.01,
            confidence=0.90,
            metadata={"expected_net_ev": 0.01},
        )

        result = runner._crypto15m_reward_guard(session, runtime, signal=signal, stake=10.0, now=now)

        assert result["accepted"] is False
        assert result["reason"] == "stop_streak"
    finally:
        session.close()
        asyncio.run(runner._client.close())


def test_crypto15m_reward_guard_ignores_stale_stop_loss_streak(tmp_path):
    db_path = tmp_path / "lab.db"
    settings = Settings()
    settings.database.url = f"sqlite:///{db_path.as_posix()}"
    settings.lab.crypto15m.reward_guard_enabled = True
    settings.lab.crypto15m.reward_stop_streak_limit = 2
    settings.lab.crypto15m.reward_lookback_hours = 24
    init_db(settings.database.url)
    runner = ShadowLabRunner(settings)
    runtime = ShadowLabRunner._build_portfolios(settings)[0]
    runtime.row_id = 1
    runtime.config.track = "crypto15m"
    runtime.bankroll = 1500
    now = datetime.now(timezone.utc)
    session = get_session(settings.database.url)
    try:
        session.add(LabEquityPointRow(
            portfolio_id=1,
            timestamp=now,
            bankroll=1500,
            realized_pnl=0,
            unrealized_pnl=0,
            equity=1500,
            drawdown_pct=0,
        ))
        session.add_all([
            LabPositionRow(
                portfolio_id=1,
                market_id=idx,
                token_id=f"tok{idx}",
                event_id=f"event{idx}",
                side="YES",
                strategy_key="Crypto15m_t80_learned",
                hypothesis="H7",
                entry_price=0.5,
                current_price=0.49,
                size=10.0,
                opened_at=now - timedelta(days=2, minutes=10 + idx),
                closed_at=now - timedelta(days=2, minutes=idx),
                status="closed",
                exit_reason="stop_loss",
                pnl=-0.1,
                realized_pnl=-0.1,
            )
            for idx in (1, 2)
        ])
        session.commit()
        signal = SignalOutput(
            hypothesis_id="H7",
            market_id="m1",
            side="YES",
            model_probability=0.60,
            market_probability=0.52,
            edge=0.01,
            confidence=0.90,
            metadata={"expected_net_ev": 0.01},
        )

        result = runner._crypto15m_reward_guard(session, runtime, signal=signal, stake=10.0, now=now)

        assert result["accepted"] is True
        assert result["stop_streak"] == 0
    finally:
        session.close()
        asyncio.run(runner._client.close())


def test_crypto15m_reward_guard_does_not_relax_stop_streak_for_analyst(tmp_path):
    db_path = tmp_path / "lab.db"
    settings = Settings()
    settings.database.url = f"sqlite:///{db_path.as_posix()}"
    settings.lab.crypto15m.reward_guard_enabled = True
    settings.lab.crypto15m.reward_stop_streak_limit = 2
    init_db(settings.database.url)
    runner = ShadowLabRunner(settings)
    runtime = ShadowLabRunner._build_portfolios(settings)[0]
    runtime.row_id = 1
    runtime.config.track = "crypto15m"
    runtime.config.use_ai_analyst = True
    runtime.bankroll = 1500
    now = datetime.now(timezone.utc)
    session = get_session(settings.database.url)
    try:
        session.add(LabEquityPointRow(
            portfolio_id=1,
            timestamp=now,
            bankroll=1500,
            realized_pnl=0,
            unrealized_pnl=0,
            equity=1500,
            drawdown_pct=0,
        ))
        session.add_all([
            LabPositionRow(
                portfolio_id=1,
                market_id=idx,
                token_id=f"tok{idx}",
                event_id=f"event{idx}",
                side="YES",
                strategy_key="Crypto15m_t65_analyst",
                hypothesis="H7",
                entry_price=0.5,
                current_price=0.49,
                size=10.0,
                opened_at=now - timedelta(minutes=10 + idx),
                closed_at=now - timedelta(minutes=idx),
                status="closed",
                exit_reason="stop_loss",
                pnl=-0.1,
                realized_pnl=-0.1,
            )
            for idx in (1, 2)
        ])
        session.commit()
        signal = SignalOutput(
            hypothesis_id="H7",
            market_id="m1",
            side="YES",
            model_probability=0.60,
            market_probability=0.52,
            edge=0.03,
            confidence=0.90,
            metadata={"expected_net_ev": 0.03},
        )

        result = runner._crypto15m_reward_guard(session, runtime, signal=signal, stake=10.0, now=now)

        assert result["accepted"] is False
        assert result["reason"] == "stop_streak"
        assert result["analyst_relaxed"] is False
        assert result["enforce_stop_streak"] is True
    finally:
        session.close()
        asyncio.run(runner._client.close())


def test_crypto15m_select_signal_preserves_explicit_no_trade_rationale(tmp_path):
    settings = Settings()
    settings.database.url = f"sqlite:///{(tmp_path / 'lab.db').as_posix()}"
    settings.lab.ab_testing.enabled = False
    runner = ShadowLabRunner(settings)
    runtime = ShadowLabRunner._build_portfolios(settings)[0]
    runtime.hypotheses = [H7_Crypto15mDirection()]
    market = _crypto_market()
    yes_book = _book()
    no_book = Orderbook(
        market_id="tok-no",
        bids=[OrderbookLevel(0.48, 500.0)],
        asks=[OrderbookLevel(0.50, 500.0)],
        timestamp=0,
    )
    try:
        signal, rejected = runner._select_signal(
            runtime,
            market,
            yes_book,
            no_book,
            datetime.now(timezone.utc),
            external_data={
                "crypto15m_is_market": True,
                "crypto15m_symbol": "BTC/USDT",
                "crypto15m_reason": "",
                "crypto15m_use_learned_gate": True,
                "crypto15m_allow_no_trade_fallback": False,
                "crypto15m_model_label": "NO_TRADE",
                "crypto15m_model_confidence": 0.92,
                "crypto15m_model_no_trade_probability": 0.92,
                "crypto15m_min_confidence": 0.80,
                "crypto15m_min_net_ev": 0.003,
                "crypto15m_max_spread": 0.04,
                "crypto15m_momentum_threshold": 0.003,
                "time_to_resolution_sec": 480,
                "crypto15m_candidate_window_minutes": 15,
                "crypto15m_candidate_min_time_to_resolution_sec": 180,
                "crypto15m_candidate_target_time_to_resolution_sec": 480,
                "crypto15m_candidate_target_tolerance_sec": 180,
            },
        )
    finally:
        asyncio.run(runner._client.close())

    assert signal is None
    assert rejected is not None
    assert rejected.rationale == "model_no_trade"
    assert rejected.metadata["model_label"] == "NO_TRADE"


def test_crypto15m_select_signal_can_fallback_from_model_no_trade(tmp_path):
    settings = Settings()
    settings.database.url = f"sqlite:///{(tmp_path / 'lab.db').as_posix()}"
    settings.lab.ab_testing.enabled = False
    runner = ShadowLabRunner(settings)
    runtime = ShadowLabRunner._build_portfolios(settings)[0]
    runtime.hypotheses = [H7_Crypto15mDirection()]
    market = _crypto_market()
    yes_book = _book()
    no_book = Orderbook(
        market_id="tok-no",
        bids=[OrderbookLevel(0.48, 500.0)],
        asks=[OrderbookLevel(0.50, 500.0)],
        timestamp=0,
    )
    try:
        signal, rejected = runner._select_signal(
            runtime,
            market,
            yes_book,
            no_book,
            datetime.now(timezone.utc),
            external_data={
                "crypto15m_is_market": True,
                "crypto15m_symbol": "BTC/USDT",
                "crypto15m_reason": "",
                "crypto15m_use_learned_gate": True,
                "crypto15m_allow_no_trade_fallback": True,
                "crypto15m_no_trade_fallback_max_probability": 0.82,
                "crypto15m_model_label": "NO_TRADE",
                "crypto15m_model_confidence": 0.77,
                "crypto15m_model_no_trade_probability": 0.77,
                "crypto15m_min_confidence": 0.75,
                "crypto15m_min_net_ev": 0.003,
                "crypto15m_max_spread": 0.04,
                "crypto15m_momentum_threshold": 0.003,
                "crypto15m_max_entry_price": 0.80,
                "crypto15m_min_abs_return_zscore_15m": 0.50,
                "crypto15m_min_trend_consistency_15m": 0.55,
                "return_zscore_15m": 1.2,
                "trend_consistency_15m": 0.80,
                "crypto_ret_5m": 0.01,
                "fee_rate": 0.02,
                "estimated_slippage": 0.001,
                "time_to_resolution_sec": 480,
                "crypto15m_candidate_window_minutes": 15,
                "crypto15m_candidate_min_time_to_resolution_sec": 180,
                "crypto15m_candidate_target_time_to_resolution_sec": 480,
                "crypto15m_candidate_target_tolerance_sec": 0,
            },
        )
    finally:
        asyncio.run(runner._client.close())

    assert rejected is None
    assert signal is not None
    assert signal.side == "YES"
    assert signal.metadata["fallback_from_model_no_trade"] is True


def test_ws_guardrail_does_not_freeze_shadow_entries_before_first_message(tmp_path):
    settings = Settings()
    settings.database.url = f"sqlite:///{(tmp_path / 'lab.db').as_posix()}"
    runner = ShadowLabRunner(settings)
    try:
        assert runner._should_freeze_entries({
            "message_count": 0,
            "last_message_age_sec": -1.0,
            "health_score": 0.0,
            "is_stale": False,
        }) is False
        assert runner._should_freeze_entries({
            "message_count": 10,
            "last_message_age_sec": 120.0,
            "health_score": 0.2,
            "is_stale": True,
        }) is True
    finally:
        asyncio.run(runner._client.close())


def test_crypto15m_runner_ignores_open_positions_from_inactive_old_portfolios(tmp_path):
    db_path = tmp_path / "lab.db"
    settings = Settings()
    settings.database.url = f"sqlite:///{db_path.as_posix()}"
    init_db(settings.database.url)

    session = get_session(settings.database.url)
    try:
        session.add(LabPositionRow(
            portfolio_id=999,
            market_id=1,
            token_id="old-token",
            event_id="old-event",
            side="YES",
            strategy_key="old",
            hypothesis="H4",
            entry_price=0.50,
            current_price=0.40,
            size=10.0,
            opened_at=datetime.now(timezone.utc) - timedelta(hours=1),
            status="open",
        ))
        session.commit()

        runner = ShadowLabRunner(settings)
        runner._portfolios_by_id = {}
        runner._submit_exit_orders(session, datetime.now(timezone.utc))
        runner._update_position_marks(session)
        assert runner._open_position_tokens() == set()
    finally:
        session.close()
        asyncio.run(runner._client.close())


def test_crypto15m_expired_position_closes_without_orderbook(tmp_path):
    db_path = tmp_path / "lab.db"
    settings = Settings()
    settings.database.url = f"sqlite:///{db_path.as_posix()}"
    settings.lab.ab_testing.enabled = False
    settings.lab.portfolios = [_crypto_portfolio()]
    init_db(settings.database.url)

    now = datetime.now(timezone.utc)
    session = get_session(settings.database.url)
    runner = ShadowLabRunner(settings)
    runtime = runner._portfolio_runtimes[0]
    try:
        portfolio = LabPortfolioRow(
            id=1,
            key=runtime.key,
            mode="shadow_maker",
            settings_json={},
            initial_bankroll=1500.0,
        )
        session.add(portfolio)
        session.flush()
        runtime.row_id = portfolio.id
        runtime.initial_bankroll = 1500.0
        runtime.bankroll = 1500.0
        runner._portfolios_by_id = {portfolio.id: runtime}

        market = MarketRow(
            polymarket_id="btc-old",
            event_id="btc-old-event",
            question="Bitcoin Up or Down - expired",
            category="Crypto",
            end_date=now - timedelta(minutes=1),
            active=False,
            volume_24h=1000.0,
            yes_token_id="yes-token",
            no_token_id="no-token",
        )
        session.add(market)
        session.flush()
        position = LabPositionRow(
            portfolio_id=portfolio.id,
            market_id=market.id,
            token_id="yes-token",
            event_id="btc-old-event",
            side="YES",
            strategy_key=runtime.key,
            hypothesis="H7",
            entry_price=0.77,
            current_price=0.99,
            size=10.0,
            opened_at=now - timedelta(minutes=20),
            status="open",
        )
        session.add(position)
        session.commit()

        runner._submit_exit_orders(session, now)
        session.commit()
        session.refresh(position)

        assert position.status == "closed"
        assert position.exit_reason == "market_resolved"
        assert position.forced_exit is True
        assert position.pnl == pytest.approx(2.2, abs=0.02)
        fill = session.query(LabFillRow).filter(LabFillRow.token_id == "yes-token").one()
        assert fill.fill_type == "forced_taker_exit"
    finally:
        session.close()
        asyncio.run(runner._client.close())


def test_execution_telemetry_counts_only_active_portfolios(tmp_path):
    db_path = tmp_path / "lab.db"
    settings = Settings()
    settings.database.url = f"sqlite:///{db_path.as_posix()}"
    init_db(settings.database.url)

    session = get_session(settings.database.url)
    runner = ShadowLabRunner(settings)
    runtime = ShadowLabRunner._build_portfolios(settings)[0]
    runtime.row_id = 1
    runner._portfolios_by_id = {1: runtime}
    try:
        active_sell = LabOrderRow(
            id=1,
            portfolio_id=1,
            market_id=1,
            token_id="active",
            event_id="active-event",
            side="YES",
            action="SELL",
            price=0.6,
            size_total=1.0,
            size_remaining=0.0,
            filled_size=1.0,
            status="filled",
        )
        old_sell = LabOrderRow(
            id=2,
            portfolio_id=999,
            market_id=1,
            token_id="old",
            event_id="old-event",
            side="YES",
            action="SELL",
            price=0.6,
            size_total=1.0,
            size_remaining=0.0,
            filled_size=1.0,
            status="filled",
        )
        session.add_all([active_sell, old_sell])
        session.flush()
        session.add_all([
            LabFillRow(
                portfolio_id=1,
                order_id=active_sell.id,
                market_id=1,
                token_id="active",
                timestamp=datetime.now(timezone.utc),
                side="YES",
                price=0.6,
                size=1.0,
                notional=0.6,
                fill_type="forced_taker_exit",
            ),
            LabFillRow(
                portfolio_id=999,
                order_id=old_sell.id,
                market_id=1,
                token_id="old",
                timestamp=datetime.now(timezone.utc),
                side="YES",
                price=0.6,
                size=1.0,
                notional=0.6,
                fill_type="forced_taker_exit",
            ),
        ])
        session.commit()

        telemetry = runner._execution_telemetry(session)

        assert telemetry["exit_fill_count"] == 1
        assert telemetry["forced_taker_exit_count"] == 1
        assert telemetry["forced_taker_exit_ratio"] == 1.0
    finally:
        session.close()
        asyncio.run(runner._client.close())


def test_position_price_delta_is_side_aware():
    assert ShadowLabRunner._position_price_delta("YES", 0.52, 0.54) == pytest.approx(0.02)
    assert ShadowLabRunner._position_price_delta("NO", 0.56, 0.54) == pytest.approx(-0.02)
    assert ShadowLabRunner._position_price_delta("NO", 0.56, 0.57) == pytest.approx(0.01)


def test_crypto15m_artifact_path_accepts_windows_separators(tmp_path, monkeypatch):
    artifact = tmp_path / "research" / "artifacts" / "crypto15m" / "model.pkl"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"model")
    monkeypatch.chdir(tmp_path)

    resolved = ShadowLabRunner._resolve_artifact_path("research\\artifacts\\crypto15m\\model.pkl")

    assert resolved == Path("research/artifacts/crypto15m/model.pkl")
    assert resolved.exists()


def test_ws_event_debounce_skips_entry_reevaluation_before_interval(tmp_path):
    async def run():
        db_path = tmp_path / "lab.db"
        settings = Settings()
        settings.database.url = f"sqlite:///{db_path.as_posix()}"
        init_db(settings.database.url)
        runner = ShadowLabRunner(settings)
        market = _crypto_market("m1")
        token = market.tokens[0]
        now = datetime.now(timezone.utc)
        runner._token_to_market[token.token_id] = market
        runner._event_eval_last_at[market.id] = now

        def fail_submit(*args, **kwargs):
            raise AssertionError("entry reevaluation should be debounced")

        runner._submit_entry_orders = fail_submit
        try:
            await runner._on_ws_event({
                "asset_id": token.token_id,
                "event_type": "last_trade_price",
                "timestamp": int((now + timedelta(milliseconds=200)).timestamp() * 1000),
                "price": 0.51,
                "size": 10.0,
                "side": "BUY",
            })
        finally:
            await runner._client.close()

    asyncio.run(run())
