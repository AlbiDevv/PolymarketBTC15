import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db.session as db_session
from config import Settings
from db.models import (
    LabDecisionAuditRow,
    LabEquityPointRow,
    LabFillRow,
    LabOrderRow,
    LabPortfolioRow,
    LabPositionRow,
    LabRuntimeStatusRow,
    MarketRow,
    ResearchModelArtifactRow,
)
from db.session import get_session, init_db
from lab.stats_service import LabStatsService


def _reset_db():
    db_session._engine = None
    db_session._SessionFactory = None


def test_lab_stats_service_builds_portfolio_summary(tmp_path):
    db_url = f"sqlite:///{(tmp_path / 'lab_stats.db').as_posix()}"
    _reset_db()
    init_db(db_url)
    session = get_session(db_url)
    now = datetime.now(timezone.utc)
    try:
        market_open = MarketRow(
            polymarket_id="m_open",
            question="Open market",
            category="sports",
            end_date=now + timedelta(days=5),
            active=True,
        )
        market_closed = MarketRow(
            polymarket_id="m_closed",
            question="Closed market",
            category="politics",
            end_date=now + timedelta(days=2),
            active=True,
        )
        session.add_all([market_open, market_closed])
        session.flush()

        portfolio = LabPortfolioRow(
            key="H2_base",
            mode="shadow_maker",
            initial_bankroll=500.0,
            settings_json={"portfolio": {"pack": "base", "hypotheses": ["H2"]}},
        )
        session.add(portfolio)
        session.flush()

        buy_order = LabOrderRow(
            portfolio_id=portfolio.id,
            market_id=market_closed.id,
            token_id="tok_closed",
            side="YES",
            action="BUY",
            price=0.40,
            size_total=10.0,
            size_remaining=0.0,
            filled_size=10.0,
            status="filled",
        )
        sell_order = LabOrderRow(
            portfolio_id=portfolio.id,
            market_id=market_closed.id,
            token_id="tok_closed",
            side="YES",
            action="SELL",
            price=0.55,
            size_total=10.0,
            size_remaining=0.0,
            filled_size=10.0,
            status="filled",
        )
        session.add_all([buy_order, sell_order])
        session.flush()

        session.add_all([
            LabFillRow(
                portfolio_id=portfolio.id,
                order_id=buy_order.id,
                market_id=market_closed.id,
                token_id="tok_closed",
                timestamp=now - timedelta(hours=2),
                side="YES",
                price=0.40,
                size=10.0,
                notional=4.0,
                fill_type="full",
            ),
            LabFillRow(
                portfolio_id=portfolio.id,
                order_id=sell_order.id,
                market_id=market_closed.id,
                token_id="tok_closed",
                timestamp=now - timedelta(hours=1),
                side="YES",
                price=0.55,
                size=10.0,
                notional=5.5,
                fill_type="full",
            ),
        ])

        session.add(
            LabPositionRow(
                portfolio_id=portfolio.id,
                market_id=market_closed.id,
                token_id="tok_closed",
                side="YES",
                strategy_key="H2_base",
                hypothesis="H2",
                entry_price=0.40,
                current_price=0.55,
                size=0.0,
                opened_at=now - timedelta(hours=2),
                closed_at=now - timedelta(hours=1),
                pnl=1.46,
                realized_pnl=1.46,
                status="closed",
                exit_reason="take_profit",
            )
        )
        session.add(
            LabPositionRow(
                portfolio_id=portfolio.id,
                market_id=market_open.id,
                token_id="tok_open",
                side="YES",
                strategy_key="H2_base",
                hypothesis="H2",
                entry_price=0.20,
                current_price=0.24,
                size=5.0,
                opened_at=now - timedelta(hours=3),
                status="open",
            )
        )
        session.add(
            LabEquityPointRow(
                portfolio_id=portfolio.id,
                timestamp=now,
                bankroll=501.46,
                realized_pnl=1.46,
                unrealized_pnl=0.20,
                equity=501.66,
                drawdown_pct=0.01,
            )
        )
        session.commit()
    finally:
        session.close()

    stats = LabStatsService(db_url, 500.0)
    overview = stats.overview()
    assert overview["winner_key"] == "H2_base"
    summary = overview["portfolios"][0]
    assert summary["fill_rate"] == 1.0
    assert round(summary["realized_pnl"], 2) == 1.46
    assert round(summary["unrealized_pnl"], 2) == 0.20
    assert summary["open_positions"] == 1
    assert "sports" in summary["exposure_by_category"]
    assert stats.daily_summaries("H2_base")


def test_lab_status_reads_durable_runtime_row(tmp_path):
    db_url = f"sqlite:///{(tmp_path / 'lab_runtime.db').as_posix()}"
    _reset_db()
    init_db(db_url)
    session = get_session(db_url)
    now = datetime.now(timezone.utc)
    try:
        session.add(LabRuntimeStatusRow(
            mode="shadow_maker",
            started_at=now - timedelta(hours=1),
            last_cycle_ts=now,
            last_cycle_ok=True,
            last_cycle_error=None,
            ws_connected=True,
            eligible_markets_last=88,
            subscribed_tokens_last=144,
        ))
        portfolio = LabPortfolioRow(
            key="Late_balanced",
            mode="shadow_maker",
            initial_bankroll=500.0,
            settings_json={"portfolio": {"pack": "late_balanced", "track": "late_stage", "hypotheses": ["H6"]}},
        )
        market = MarketRow(polymarket_id="m1", question="Q", category="news", active=True)
        session.add_all([portfolio, market])
        session.flush()
        session.add(LabDecisionAuditRow(
            portfolio_id=portfolio.id,
            market_id=market.id,
            timestamp=now,
            decision="rejected",
            track="late_stage",
            portfolio_key="Late_balanced",
            question_snapshot="Q",
            reasons_json=["wide_spread"],
        ))
        session.commit()
    finally:
        session.close()

    stats = LabStatsService(db_url, 500.0)
    status = stats.get_status({})
    assert status.ws_connected is True
    assert status.markets_fetched_last == 88
    assert status.subscribed_tokens_last == 144


def test_lab_stats_filters_inactive_ab_portfolios_from_settings(tmp_path):
    db_url = f"sqlite:///{(tmp_path / 'lab_active_filter.db').as_posix()}"
    _reset_db()
    init_db(db_url)
    session = get_session(db_url)
    now = datetime.now(timezone.utc)
    try:
        rows = [
            LabPortfolioRow(
                key="Crypto15m_control",
                mode="shadow_maker",
                initial_bankroll=1500.0,
                settings_json={"portfolio": {"pack": "crypto15m", "track": "crypto15m", "ab_group": "control"}},
            ),
            LabPortfolioRow(
                key="Crypto15m_t80_learned",
                mode="shadow_maker",
                initial_bankroll=1500.0,
                settings_json={"portfolio": {"pack": "crypto15m", "track": "crypto15m", "ab_group": "learned_t80"}},
            ),
            LabPortfolioRow(
                key="Crypto15m_t90_learned",
                mode="shadow_maker",
                initial_bankroll=1500.0,
                settings_json={"portfolio": {"pack": "crypto15m", "track": "crypto15m", "ab_group": "learned_t90"}},
            ),
            LabPortfolioRow(
                key="Crypto15m_t65_analyst",
                mode="shadow_maker",
                initial_bankroll=1500.0,
                settings_json={"portfolio": {"pack": "crypto15m", "track": "crypto15m", "ab_group": "analyst_t65"}},
            ),
        ]
        session.add_all(rows)
        session.flush()
        for row, realized in zip(rows, (1.0, 2.0, 3.0, -50.0)):
            session.add(LabEquityPointRow(
                portfolio_id=row.id,
                timestamp=now,
                bankroll=1500.0 + realized,
                realized_pnl=realized,
                unrealized_pnl=0.0,
                equity=1500.0 + realized,
                drawdown_pct=0.0,
            ))
        session.commit()
    finally:
        session.close()

    settings = Settings()
    settings.lab.ab_testing.enabled = True
    settings.lab.crypto15m.ab_thresholds = [0.80, 0.90]
    settings.lab.crypto15m.ai_analyst.enabled = False
    stats = LabStatsService(db_url, 1500.0, settings=settings)

    overview = stats.overview()
    keys = [item["key"] for item in overview["portfolios"]]
    assert keys == ["Crypto15m_control", "Crypto15m_t80_learned", "Crypto15m_t90_learned"]
    assert overview["aggregate"]["equity"] == 4506.0


def test_lab_stats_service_builds_ab_group_verdict(tmp_path):
    db_url = f"sqlite:///{(tmp_path / 'lab_ab_stats.db').as_posix()}"
    _reset_db()
    init_db(db_url)
    session = get_session(db_url)
    now = datetime.now(timezone.utc)
    try:
        control = LabPortfolioRow(
            key="H2_base_control",
            mode="shadow_maker",
            initial_bankroll=500.0,
            settings_json={"portfolio": {"pack": "base", "track": "control", "ab_group": "control", "base_key": "H2_base", "hypotheses": ["H2"]}},
        )
        learned = LabPortfolioRow(
            key="H2_base_learned",
            mode="shadow_maker",
            initial_bankroll=500.0,
            settings_json={"portfolio": {"pack": "base", "track": "control", "ab_group": "learned", "base_key": "H2_base", "hypotheses": ["H2"]}},
        )
        session.add_all([control, learned])
        session.flush()
        session.add_all([
            LabEquityPointRow(
                portfolio_id=control.id,
                timestamp=now,
                bankroll=501.0,
                realized_pnl=1.0,
                unrealized_pnl=0.0,
                equity=501.0,
                drawdown_pct=0.01,
            ),
            LabEquityPointRow(
                portfolio_id=learned.id,
                timestamp=now,
                bankroll=505.0,
                realized_pnl=5.0,
                unrealized_pnl=0.0,
                equity=505.0,
                drawdown_pct=0.01,
            ),
            LabRuntimeStatusRow(
                mode="shadow_maker",
                started_at=now,
                last_cycle_ts=now,
                ws_connected=True,
            ),
            ResearchModelArtifactRow(
                artifact_key="pmotif_test",
                model_type="logistic_regression",
                artifact_path="model.pkl",
                manifest_path="latest_manifest.json",
                metrics_json={"verdict": {"accepted": True, "reason": "accepted"}},
                holdout_summary_json=[],
                accepted=True,
                enabled=True,
                high_conf_accuracy=0.97,
                high_conf_net_ev=0.02,
                calibration_error=0.03,
            ),
        ])
        session.commit()
    finally:
        session.close()

    stats = LabStatsService(db_url, 500.0)
    overview = stats.overview()
    assert overview["ab_groups"]["control"]["equity"] == 501.0
    assert overview["ab_groups"]["learned"]["equity"] == 505.0
    assert overview["verdict"]["status"] == "positive"
    status = stats.get_status({})
    assert status.ab_groups["learned"]["equity"] == 505.0
