import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db.session as db_session
from config import Settings
from dashboard.app import create_app
from db.models import LabDecisionAuditRow, LabEquityPointRow, LabPortfolioRow, LabRuntimeStatusRow, LabWsMetricRow, MarketRow, ResearchModelArtifactRow
from db.session import get_session, init_db
from fastapi.testclient import TestClient


def _reset_db():
    db_session._engine = None
    db_session._SessionFactory = None


def test_dashboard_api_returns_lab_overview(tmp_path):
    db_url = f"sqlite:///{(tmp_path / 'dashboard.db').as_posix()}"
    _reset_db()
    init_db(db_url)
    session = get_session(db_url)
    now = datetime.now(timezone.utc)
    try:
        portfolio = LabPortfolioRow(
            key="H2_base",
            mode="shadow_maker",
            initial_bankroll=500.0,
            settings_json={"portfolio": {"pack": "base", "hypotheses": ["H2"]}},
        )
        session.add(portfolio)
        session.flush()
        session.add(
            LabEquityPointRow(
                portfolio_id=portfolio.id,
                timestamp=now,
                bankroll=500.0,
                realized_pnl=0.0,
                unrealized_pnl=0.0,
                equity=500.0,
                drawdown_pct=0.0,
            )
        )
        session.commit()
    finally:
        session.close()

    settings = Settings()
    settings.database.url = db_url
    settings.telegram.dev_initdata_bypass = True
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/api/overview")
    assert response.status_code == 200
    data = response.json()
    assert data["portfolios"][0]["key"] == "H2_base"
    assert "runtime" in data

    html = client.get("/")
    assert html.status_code == 200
    assert "Русский мониторинг shadow lab" in html.text


def test_dashboard_rejections_and_candidates_endpoints(tmp_path):
    db_url = f"sqlite:///{(tmp_path / 'dashboard_events.db').as_posix()}"
    _reset_db()
    init_db(db_url)
    session = get_session(db_url)
    now = datetime.now(timezone.utc)
    try:
        market = MarketRow(
            polymarket_id="m1",
            question="Will something happen?",
            category="politics",
            active=True,
        )
        portfolio = LabPortfolioRow(
            key="Late_balanced",
            mode="shadow_maker",
            initial_bankroll=500.0,
            settings_json={"portfolio": {"pack": "late_balanced", "track": "late_stage", "hypotheses": ["H6"]}},
        )
        session.add_all([market, portfolio])
        session.flush()
        session.add_all([
            LabDecisionAuditRow(
                portfolio_id=portfolio.id,
                market_id=market.id,
                timestamp=now,
                decision="rejected",
                track="late_stage",
                portfolio_key=portfolio.key,
                hypothesis="H6",
                side="YES",
                quality_score=42.0,
                expected_net_edge=-0.01,
                question_snapshot=market.question,
                category=market.category,
                reasons_json=["keyword_dispute_resolution"],
            ),
            LabDecisionAuditRow(
                portfolio_id=portfolio.id,
                market_id=market.id,
                timestamp=now,
                decision="candidate",
                track="late_stage",
                portfolio_key=portfolio.key,
                hypothesis="H6",
                side="YES",
                edge=0.03,
                quality_score=78.0,
                question_snapshot=market.question,
                category=market.category,
                reasons_json=[],
            ),
        ])
        session.commit()
    finally:
        session.close()

    settings = Settings()
    settings.database.url = db_url
    settings.telegram.dev_initdata_bypass = True
    app = create_app(settings)
    client = TestClient(app)

    rejections = client.get("/api/rejections")
    assert rejections.status_code == 200
    assert rejections.json()["items"][0]["reasons"] == ["keyword_dispute_resolution"]

    candidates = client.get("/api/candidates")
    assert candidates.status_code == 200
    assert candidates.json()["items"][0]["portfolio_key"] == "Late_balanced"


def test_dashboard_metrics_endpoint(tmp_path):
    db_url = f"sqlite:///{(tmp_path / 'dashboard_metrics.db').as_posix()}"
    _reset_db()
    init_db(db_url)
    session = get_session(db_url)
    now = datetime.now(timezone.utc)
    try:
        portfolio = LabPortfolioRow(
            key="H2_base",
            mode="shadow_maker",
            initial_bankroll=500.0,
            settings_json={"portfolio": {"pack": "base", "track": "control", "hypotheses": ["H2"]}},
        )
        session.add(portfolio)
        session.flush()
        session.add(LabEquityPointRow(
            portfolio_id=portfolio.id,
            timestamp=now,
            bankroll=500.0,
            realized_pnl=1.0,
            unrealized_pnl=0.0,
            equity=501.0,
            drawdown_pct=0.0,
        ))
        session.add(LabRuntimeStatusRow(
            mode="shadow_maker",
            started_at=now,
            last_cycle_ts=now,
            ws_connected=True,
            eligible_markets_last=10,
            subscribed_tokens_last=20,
        ))
        session.add(LabWsMetricRow(
            timestamp=now,
            connected=True,
            health_score=0.9,
            gap_count=1,
            last_message_age_sec=1.0,
            entries_frozen=False,
            forced_taker_exit_ratio=0.02,
            maker_fill_ratio=0.80,
        ))
        session.add(ResearchModelArtifactRow(
            artifact_key="artifact_1",
            artifact_path="model.pkl",
            manifest_path="manifest.json",
            accepted=True,
            enabled=True,
            high_conf_accuracy=0.97,
            high_conf_net_ev=0.01,
            calibration_error=0.03,
        ))
        session.commit()
    finally:
        session.close()

    settings = Settings()
    settings.database.url = db_url
    settings.telegram.dev_initdata_bypass = True
    client = TestClient(create_app(settings))
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "shadow_lab_ws_health_score" in response.text
    assert "shadow_lab_learning_high_conf_accuracy" in response.text
    assert "shadow_lab_training_verdict" in response.text
