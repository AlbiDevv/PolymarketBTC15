from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exchange_client.base import Orderbook, OrderbookLevel
from models.hypothesis import H7_Crypto15mDirection
from research.crypto15m import (
    CRYPTO15M_FEATURE_COLUMNS,
    add_polymarket_features,
    build_crypto_features,
    classify_crypto15m_updown_market,
    classify_crypto_market,
    choose_training_side,
    crypto15m_side_gate_reason,
    label_candidate,
    normalize_ohlcv_rows,
    train_crypto15m_model,
)


def _book(bid: float, ask: float) -> Orderbook:
    return Orderbook(
        market_id="m1",
        bids=[OrderbookLevel(bid, 1000)],
        asks=[OrderbookLevel(ask, 1000)],
        timestamp=0,
    )


def test_crypto_market_classifier_finds_btc_15m_and_ignores_other_markets():
    info = classify_crypto_market("Bitcoin Up or Down - 15 minutes")
    assert info.is_crypto15m
    assert info.symbol == "BTC/USDT"
    assert info.timeframe_minutes == 15
    ranged = classify_crypto_market("Bitcoin Up or Down - April 12, 11:10AM-11:15AM ET")
    assert ranged.is_crypto15m
    assert ranged.timeframe_minutes == 15
    hourly = classify_crypto_market("Ethereum Up or Down - April 12, 10AM ET")
    assert hourly.is_crypto15m
    assert hourly.timeframe_minutes == 60
    strict = classify_crypto15m_updown_market("Ethereum Up or Down - April 12, 10AM ET")
    assert not strict.is_crypto15m
    strike = classify_crypto15m_updown_market("Bitcoin above 72,600 on April 11, 1AM ET?")
    assert not strike.is_crypto15m
    assert not classify_crypto_market("Will it rain in New York tomorrow?").is_crypto15m


def test_ohlcv_normalization_and_feature_builder_are_past_only():
    rows = [
        [1_700_000_000_000 + i * 60_000, 100 + i, 101 + i, 99 + i, 100.5 + i, 10 + i]
        for i in range(70)
    ]
    frame = normalize_ohlcv_rows(rows, exchange_id="binance", symbol="BTC/USDT", timeframe="1m")
    features = build_crypto_features(frame, at=frame.iloc[30]["timestamp"], symbol="BTC/USDT")
    assert features["ret_1m"] > 0
    assert features["ret_60m"] > 0
    assert "distance_to_15m_open" in features
    assert "trend_consistency_15m" in features
    assert "volatility_regime_60m" in features
    assert "distance_to_vwap_15m" in features


def test_candidate_label_no_trade_when_fee_adjusted_ev_negative():
    label = label_candidate(
        yes_wins=True,
        side="YES",
        entry_price=0.999,
        fee_rate=0.02,
        slippage=0.001,
        fill_probability=0.8,
    )
    assert label["net_ev"] < 0


def test_crypto15m_side_gate_rejects_late_entry_and_weak_regime():
    row = pd.Series({
        "yes_entry_price": 0.86,
        "no_entry_price": 0.14,
        "return_zscore_15m": 0.30,
        "trend_consistency_15m": 0.52,
    })
    assert crypto15m_side_gate_reason(
        row,
        "YES",
        max_entry_price=0.80,
        min_abs_return_zscore_15m=0.50,
        min_trend_consistency_15m=0.55,
    ) == "price_too_late"
    assert crypto15m_side_gate_reason(
        row,
        "NO",
        max_entry_price=0.80,
        min_abs_return_zscore_15m=0.50,
        min_trend_consistency_15m=0.55,
    ) == "btc_zscore_too_small"


def test_choose_training_side_uses_trade_gates():
    row = pd.Series({
        "yes_entry_price": 0.84,
        "no_entry_price": 0.32,
        "yes_net_ev": 0.20,
        "no_net_ev": 0.01,
        "return_zscore_15m": 0.90,
        "trend_consistency_15m": 0.70,
    })
    assert choose_training_side(
        row,
        min_net_ev=0.003,
        max_entry_price=0.80,
        min_abs_return_zscore_15m=0.50,
        min_trend_consistency_15m=0.55,
    ) == "NO"


def test_h7_emits_yes_and_no_and_rejects_wide_spread():
    h7 = H7_Crypto15mDirection()
    yes = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.50, 0.52),
        external_data={
                "crypto15m_is_market": True,
                "crypto_ret_5m": 0.01,
                "crypto15m_momentum_threshold": 0.003,
                "crypto15m_min_net_ev": 0.003,
                "crypto15m_min_confidence": 0.54,
                "crypto15m_max_spread": 0.04,
                "time_to_resolution_sec": 480,
            },
        )
    no = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.50, 0.52),
        external_data={
                "crypto15m_is_market": True,
                "crypto_ret_5m": -0.01,
                "crypto15m_momentum_threshold": 0.003,
                "crypto15m_min_net_ev": 0.003,
                "crypto15m_min_confidence": 0.54,
                "crypto15m_max_spread": 0.04,
                "no_best_ask": 0.50,
                "time_to_resolution_sec": 480,
            },
    )
    rejected = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.50, 0.60),
        external_data={"crypto15m_is_market": True, "crypto15m_max_spread": 0.04, "time_to_resolution_sec": 480},
    )
    assert yes.side == "YES"
    assert no.side == "NO"
    assert rejected.side is None
    assert rejected.rationale == "spread_too_wide"


def test_h7_reports_explicit_reason_when_market_is_not_crypto15m():
    h7 = H7_Crypto15mDirection()
    signal = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - April 13, 6AM ET",
        _book(0.50, 0.52),
        external_data={
            "crypto15m_is_market": False,
            "crypto15m_reason": "not_15m_updown_market",
            "time_to_resolution_sec": 480,
        },
    )
    assert signal.side is None
    assert signal.rationale == "not_15m_updown_market"


def test_h7_learned_model_uses_probability_and_fee_adjusted_ev():
    h7 = H7_Crypto15mDirection()
    yes = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.50, 0.52),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_model_side": "YES",
            "crypto15m_model_confidence": 0.95,
            "crypto15m_model_yes_probability": 0.95,
            "crypto15m_min_net_ev": 0.003,
            "crypto15m_min_confidence": 0.90,
            "crypto15m_max_spread": 0.04,
            "fee_rate": 0.02,
            "estimated_slippage": 0.001,
            "time_to_resolution_sec": 480,
        },
    )
    no = h7.evaluate(
        "m1",
        "Ethereum Up or Down - 15 minutes",
        _book(0.50, 0.52),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_model_side": "NO",
            "crypto15m_model_confidence": 0.95,
            "crypto15m_model_yes_probability": 0.05,
            "no_best_ask": 0.50,
            "crypto15m_min_net_ev": 0.003,
            "crypto15m_min_confidence": 0.90,
            "crypto15m_max_spread": 0.04,
            "fee_rate": 0.02,
            "estimated_slippage": 0.001,
            "time_to_resolution_sec": 480,
        },
    )
    late = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.998, 0.999),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_model_side": "YES",
            "crypto15m_model_confidence": 0.99,
            "crypto15m_model_yes_probability": 0.99,
            "crypto15m_min_net_ev": 0.003,
            "crypto15m_min_confidence": 0.90,
            "crypto15m_max_spread": 0.04,
            "fee_rate": 0.02,
            "estimated_slippage": 0.001,
            "time_to_resolution_sec": 480,
        },
    )
    assert yes.side == "YES"
    assert yes.model_probability == 0.95
    assert yes.edge > 0.003
    assert no.side == "NO"
    assert no.model_probability == 0.05
    assert no.edge > 0.003
    assert late.side is None
    assert late.rationale == "fee_adjusted_ev_negative"


def test_h7_learned_model_rejects_stale_live_ohlcv():
    h7 = H7_Crypto15mDirection()
    signal = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.50, 0.52),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_model_side": "YES",
            "crypto15m_model_confidence": 0.95,
            "crypto15m_model_yes_probability": 0.95,
            "crypto_ohlcv_stale": True,
            "crypto15m_min_confidence": 0.90,
            "crypto15m_max_spread": 0.04,
            "time_to_resolution_sec": 480,
        },
    )
    assert signal.side is None
    assert signal.rationale == "crypto_ohlcv_stale"


def test_h7_learned_model_honors_no_trade_label():
    h7 = H7_Crypto15mDirection()
    signal = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.50, 0.52),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_use_learned_gate": True,
            "crypto15m_allow_no_trade_fallback": False,
            "crypto15m_model_label": "NO_TRADE",
            "crypto15m_model_confidence": 0.91,
            "crypto15m_model_no_trade_probability": 0.91,
            "crypto_ret_5m": 0.02,
            "crypto15m_momentum_threshold": 0.003,
            "crypto15m_min_confidence": 0.80,
            "crypto15m_max_spread": 0.04,
            "time_to_resolution_sec": 480,
        },
    )
    assert signal.side is None
    assert signal.rationale == "model_no_trade"
    assert signal.metadata["model_label"] == "NO_TRADE"


def test_h7_learned_no_trade_can_fallback_to_momentum_signal():
    h7 = H7_Crypto15mDirection()
    signal = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.50, 0.52),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_use_learned_gate": True,
            "crypto15m_allow_no_trade_fallback": True,
            "crypto15m_no_trade_fallback_max_probability": 0.82,
            "crypto15m_model_label": "NO_TRADE",
            "crypto15m_model_confidence": 0.77,
            "crypto15m_model_no_trade_probability": 0.77,
            "crypto_ret_5m": 0.01,
            "return_zscore_15m": 1.2,
            "trend_consistency_15m": 0.80,
            "crypto15m_max_entry_price": 0.80,
            "crypto15m_min_abs_return_zscore_15m": 0.50,
            "crypto15m_min_trend_consistency_15m": 0.55,
            "crypto15m_momentum_threshold": 0.003,
            "crypto15m_min_confidence": 0.75,
            "crypto15m_min_net_ev": 0.003,
            "crypto15m_max_spread": 0.04,
            "fee_rate": 0.02,
            "estimated_slippage": 0.001,
            "time_to_resolution_sec": 480,
        },
    )
    assert signal.side == "YES"
    assert signal.rationale.startswith("crypto15m fallback momentum YES")
    assert signal.metadata["fallback_from_model_no_trade"] is True
    assert signal.metadata["model_label"] == "NO_TRADE"


def test_h7_fallback_uses_composite_momentum_not_only_ret_5m():
    h7 = H7_Crypto15mDirection()
    signal = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.49, 0.51),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_use_learned_gate": True,
            "crypto15m_allow_no_trade_fallback": True,
            "crypto15m_no_trade_fallback_max_probability": 0.97,
            "crypto15m_model_label": "NO_TRADE",
            "crypto15m_model_confidence": 0.72,
            "crypto15m_model_no_trade_probability": 0.72,
            "crypto_ret_1m": 0.0012,
            "crypto_ret_3m": 0.0014,
            "crypto_ret_5m": 0.0002,
            "crypto_ret_15m": 0.0010,
            "crypto15m_momentum_threshold": 0.0008,
            "crypto15m_min_confidence": 0.65,
            "crypto15m_max_spread": 0.04,
            "return_zscore_15m": 1.2,
            "trend_consistency_15m": 0.80,
            "crypto15m_max_entry_price": 0.80,
            "crypto15m_min_abs_return_zscore_15m": 0.50,
            "crypto15m_min_trend_consistency_15m": 0.55,
            "fee_rate": 0.02,
            "estimated_slippage": 0.001,
            "time_to_resolution_sec": 480,
        },
    )
    assert signal.side == "YES"
    assert signal.rationale.startswith("crypto15m fallback momentum YES")
    assert signal.metadata["composite_momentum"] > 0.0008


def test_h7_can_synthesize_no_entry_price_from_yes_book():
    h7 = H7_Crypto15mDirection()
    signal = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.49, 0.51),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_use_learned_gate": True,
            "crypto15m_model_side": "NO",
            "crypto15m_model_label": "NO",
            "crypto15m_model_confidence": 0.82,
            "crypto15m_model_yes_probability": 0.35,
            "crypto15m_min_confidence": 0.80,
            "crypto15m_min_net_ev": 0.003,
            "crypto15m_max_spread": 0.04,
            "crypto15m_max_entry_price": 0.85,
            "return_zscore_15m": 1.1,
            "trend_consistency_15m": 0.74,
            "fee_rate": 0.02,
            "estimated_slippage": 0.001,
            "time_to_resolution_sec": 480,
        },
    )
    assert signal.side == "NO"
    assert signal.entry_price is not None
    assert 0.0 < signal.entry_price < 1.0


def test_h7_analyst_relaxes_regime_gates_for_learned_signal():
    h7 = H7_Crypto15mDirection()
    signal = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.49, 0.51),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_use_learned_gate": True,
            "crypto15m_relax_regime_gates": True,
            "crypto15m_model_side": "YES",
            "crypto15m_model_label": "YES",
            "crypto15m_model_confidence": 0.82,
            "crypto15m_model_yes_probability": 0.68,
            "crypto15m_min_confidence": 0.80,
            "crypto15m_min_net_ev": 0.003,
            "crypto15m_max_spread": 0.04,
            "crypto15m_max_entry_price": 0.85,
            "return_zscore_15m": 0.10,
            "trend_consistency_15m": 0.40,
            "fee_rate": 0.02,
            "estimated_slippage": 0.001,
            "time_to_resolution_sec": 480,
        },
    )
    assert signal.side == "YES"
    assert signal.rationale.startswith("crypto15m learned YES")


def test_h7_learned_model_rejects_price_too_late_and_weak_regime():
    h7 = H7_Crypto15mDirection()
    late = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.84, 0.86),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_model_side": "YES",
            "crypto15m_model_confidence": 0.97,
            "crypto15m_model_yes_probability": 0.97,
            "crypto15m_min_net_ev": 0.003,
            "crypto15m_min_confidence": 0.90,
            "crypto15m_max_spread": 0.04,
            "crypto15m_max_entry_price": 0.80,
            "crypto15m_min_abs_return_zscore_15m": 0.50,
            "crypto15m_min_trend_consistency_15m": 0.55,
            "return_zscore_15m": 1.20,
            "trend_consistency_15m": 0.80,
            "fee_rate": 0.02,
            "estimated_slippage": 0.001,
            "time_to_resolution_sec": 480,
        },
    )
    weak = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.50, 0.52),
        external_data={
            "crypto15m_is_market": True,
            "crypto15m_model_side": "YES",
            "crypto15m_model_confidence": 0.97,
            "crypto15m_model_yes_probability": 0.80,
            "crypto15m_min_net_ev": 0.003,
            "crypto15m_min_confidence": 0.90,
            "crypto15m_max_spread": 0.04,
            "crypto15m_max_entry_price": 0.80,
            "crypto15m_min_abs_return_zscore_15m": 0.50,
            "crypto15m_min_trend_consistency_15m": 0.55,
            "return_zscore_15m": 0.10,
            "trend_consistency_15m": 0.80,
            "fee_rate": 0.02,
            "estimated_slippage": 0.001,
            "time_to_resolution_sec": 480,
        },
    )
    assert late.side is None
    assert late.rationale == "price_too_late"
    assert weak.side is None
    assert weak.rationale == "btc_zscore_too_small"


def test_h7_rejects_market_outside_target_entry_window():
    h7 = H7_Crypto15mDirection()
    signal = h7.evaluate(
        "m1",
        "Bitcoin Up or Down - 15 minutes",
        _book(0.50, 0.52),
        external_data={
            "crypto15m_is_market": True,
            "crypto_ret_5m": 0.01,
            "crypto15m_momentum_threshold": 0.003,
            "crypto15m_min_net_ev": 0.003,
            "crypto15m_min_confidence": 0.55,
            "crypto15m_max_spread": 0.04,
            "time_to_resolution_sec": 840,
            "crypto15m_candidate_window_minutes": 15,
            "crypto15m_candidate_min_time_to_resolution_sec": 180,
            "crypto15m_candidate_target_time_to_resolution_sec": 480,
            "crypto15m_candidate_target_tolerance_sec": 180,
        },
    )
    assert signal.side is None
    assert signal.rationale == "outside_entry_window"


def test_crypto15m_model_rejects_small_dataset(tmp_path):
    frame = pd.DataFrame([{column: 0.0 for column in CRYPTO15M_FEATURE_COLUMNS} for _ in range(10)])
    frame["label_side"] = "NO_TRADE"
    verdict = train_crypto15m_model(frame, artifact_dir=tmp_path, min_rows=3000)
    assert verdict["accepted"] is False
    assert verdict["reason"] == "not_enough_rows"
