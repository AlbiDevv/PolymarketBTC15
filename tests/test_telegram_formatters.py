import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.stats_service import StatusSnapshot
from monitor.telegram_formatters import fmt_crypto15m, fmt_dashboard_hint, fmt_positions, fmt_status


def test_dashboard_hint_uses_ssh_instructions():
    text = fmt_dashboard_hint(
        base_url="http://127.0.0.1:8090",
        ssh_hint="ssh -L 8090:127.0.0.1:8090 root@1.2.3.4",
        local_url="http://127.0.0.1:8090/dashboard",
        mode="ssh_hint",
    )
    assert "ssh -L 8090:127.0.0.1:8090 root@1.2.3.4" in text
    assert "http://127.0.0.1:8090/dashboard" in text


def test_positions_formatter_deduplicates_same_market():
    text = fmt_positions([
        {
            "portfolio_key": "H4_base",
            "question": "Will US withdraw from NATO by April 30?",
            "side": "YES",
            "entry": 0.015,
            "mark": 0.014,
            "size": 133.33,
            "unrealized_pnl": -0.13,
            "hold_hours": 0.3,
        },
        {
            "portfolio_key": "Combo_base",
            "question": "Will US withdraw from NATO by April 30?",
            "side": "YES",
            "entry": 0.015,
            "mark": 0.014,
            "size": 133.33,
            "unrealized_pnl": -0.13,
            "hold_hours": 0.3,
        },
    ])
    assert text.count("Will US withdraw from NATO by April 30?") == 1
    assert "266.66" in text


def test_crypto15m_formatter_splits_btc_eth_other():
    text = fmt_crypto15m({
        "manifest": {"accepted": True, "rows": 121340, "markets_used": 17246, "coverage_days": 90},
        "ws_connected": True,
        "ws_health_score": 1.0,
        "ws_subscribed_tokens": 24,
        "latest_ohlcv_age_sec": 12,
        "latest_ohlcv_exchange": "binance",
        "candidate_count_24h": 10,
        "accepted_count_24h": 2,
        "orders_count": 4,
        "fills_count": 3,
        "reject_count_24h": 8,
        "active_thresholds": ["t80", "t90", "t95"],
        "trade_assets": ["BTC"],
        "assets": {
            "BTC": {"realized_pnl": 1.2, "unrealized_pnl": 0.1, "orders": 2, "fills": 1, "fill_rate": 0.5, "open_positions": 1, "best_threshold": "t80"},
            "ETH": {"realized_pnl": -0.2, "unrealized_pnl": 0.0, "orders": 2, "fills": 2, "fill_rate": 1.0, "open_positions": 0, "best_threshold": "t80"},
            "Other": {"realized_pnl": 0.0, "unrealized_pnl": 0.0, "orders": 0, "fills": 0, "fill_rate": 0.0, "open_positions": 0, "best_threshold": "n/a"},
        },
        "thresholds": {
            "t70": {"equity": 1501.2, "realized_pnl": 1.2, "fills": 1},
            "t80": {"equity": 1499.8, "realized_pnl": -0.2, "fills": 2},
        },
    })
    assert "<b>BTC</b>" in text
    assert "<b>ETH disabled / historical only</b>" in text
    assert "<b>Other disabled / historical only</b>" not in text
    assert "Active: BTC | t80/t90/t95" in text
    assert "t70: eq" not in text
    assert "t80: eq" in text
    assert "Crypto15m BTC-only" in text


def test_status_formatter_shows_live_crypto15m_heartbeat():
    text = fmt_status(StatusSnapshot(
        mode="shadow_maker",
        bankroll=10469.23,
        realized_pnl_today=0.0,
        unrealized_pnl=0.0,
        open_positions_count=0,
        trades_today=0,
        drawdown_pct=0.0,
        last_cycle_ts="2026-04-18T10:50:00",
        ws_connected=True,
        gate_status="shadow_maker",
        paper_days=0,
        subscribed_tokens_last=8,
        ws_health_score=0.81,
        candidate_count_24h=26,
        accepted_count_24h=0,
        reject_count_24h=80736,
        top_reject_reason="low_volume",
        last_decision_ts="2026-04-18T10:49:48",
        latest_ohlcv_age_sec=48.0,
        ab_groups={
            "control": {"equity": 1499.54},
            "learned_t90": {"equity": 1500.21},
            "learned_t70": {"equity": 1485.61},
        },
    ))
    assert "Crypto15m 24h c/a/r 26/0/80736" in text
    assert "Top reject <code>low_volume</code>" in text
    assert "Last decision" in text
    assert "control $1499.54" in text
    assert "best learned_t90 $1500.21" in text
    assert "combined $10469.23" in text


def test_status_formatter_single_live_portfolio_uses_bankroll_line():
    text = fmt_status(StatusSnapshot(
        mode="shadow_maker",
        bankroll=1500.0,
        realized_pnl_today=0.0,
        unrealized_pnl=-0.91,
        open_positions_count=1,
        trades_today=0,
        drawdown_pct=0.0,
        last_cycle_ts="2026-04-20T11:32:51",
        ws_connected=True,
        gate_status="shadow_maker",
        paper_days=0,
        subscribed_tokens_last=8,
        ws_health_score=1.0,
        ab_groups={"learned": {"equity": 1499.09}},
    ))
    assert "Bankroll $1500.00 | today $+0.00 | u $-0.91" in text
    assert "best learned" not in text
