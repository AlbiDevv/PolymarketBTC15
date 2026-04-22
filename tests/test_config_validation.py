"""Tests for config validation."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from config import Settings, StrategyConfig, RiskConfig, BankrollConfig, _validate, load_settings


class TestConfigValidation:
    def test_valid_config_passes(self):
        s = Settings()
        _validate(s)  # should not raise

    def test_negative_edge_threshold_fails(self):
        s = Settings()
        s.strategy.edge_threshold = -0.1
        with pytest.raises(ValueError, match="edge_threshold"):
            _validate(s)

    def test_kelly_fraction_above_one_fails(self):
        s = Settings()
        s.strategy.kelly_fraction = 1.5
        with pytest.raises(ValueError, match="kelly_fraction"):
            _validate(s)

    def test_stake_min_above_max_fails(self):
        s = Settings()
        s.strategy.stake_min = 10.0
        s.strategy.stake_max = 5.0
        with pytest.raises(ValueError, match="stake_min"):
            _validate(s)

    def test_zero_bankroll_fails(self):
        s = Settings()
        s.bankroll.initial = 0
        with pytest.raises(ValueError, match="bankroll"):
            _validate(s)

    def test_live_mode_without_key_fails(self):
        s = Settings()
        s.mode = "live"
        s.polygon_private_key = ""
        with pytest.raises(ValueError, match="POLYGON_PRIVATE_KEY"):
            _validate(s)

    def test_fee_rate_negative_fails(self):
        s = Settings()
        s.strategy.fee_rate = -0.01
        with pytest.raises(ValueError, match="fee_rate"):
            _validate(s)

    def test_multiple_admin_ids_loaded_from_yaml_and_env(self, monkeypatch):
        with tempfile.TemporaryDirectory() as d:
            yaml_path = Path(d) / "settings.yaml"
            yaml_path.write_text(
                """
alerts:
  telegram_admin_chat_id: "101"
  telegram_admin_chat_ids:
    - "202"
""",
                encoding="utf-8",
            )
            monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "303,404")
            monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_IDS", "505,606")
            settings = load_settings(yaml_path)
            assert settings.alerts.telegram_admin_chat_ids == ["202", "101", "303", "404", "505", "606"]
            assert settings.alerts.telegram_admin_chat_id == "202"

    def test_shadow_maker_allows_blank_live_credentials(self, monkeypatch):
        with tempfile.TemporaryDirectory() as d:
            yaml_path = Path(d) / "settings.yaml"
            yaml_path.write_text("mode: shadow_maker\n", encoding="utf-8")
            monkeypatch.delenv("POLYGON_PRIVATE_KEY", raising=False)
            monkeypatch.delenv("POLYMARKET_API_KEY", raising=False)
            monkeypatch.delenv("POLYMARKET_API_SECRET", raising=False)
            monkeypatch.delenv("POLYMARKET_API_PASSPHRASE", raising=False)
            settings = load_settings(yaml_path)
            assert settings.mode == "shadow_maker"
            assert settings.polygon_private_key == ""

    def test_database_url_can_be_overridden_from_env(self, monkeypatch):
        with tempfile.TemporaryDirectory() as d:
            yaml_path = Path(d) / "settings.yaml"
            yaml_path.write_text(
                """
database:
  url: sqlite:///local.db
""",
                encoding="utf-8",
            )
            monkeypatch.setenv("PREDICTION_TRADER_DATABASE_URL", "postgresql+psycopg://user:pass@localhost:5432/prediction_trader")
            settings = load_settings(yaml_path)
            assert settings.database.url == "postgresql+psycopg://user:pass@localhost:5432/prediction_trader"

    def test_historical_min_markets_cannot_exceed_max(self):
        s = Settings()
        s.historical.price_window.max_markets_per_run = 10
        s.historical.price_window.min_markets_required = 20
        with pytest.raises(ValueError, match="min_markets_required"):
            _validate(s)

    def test_historical_price_window_concurrency_must_be_positive(self):
        s = Settings()
        s.historical.price_window.concurrency = 0
        with pytest.raises(ValueError, match="concurrency"):
            _validate(s)

    def test_historical_price_window_day_limit_cannot_be_negative(self):
        s = Settings()
        s.historical.price_window.max_markets_per_settlement_day = -1
        with pytest.raises(ValueError, match="max_markets_per_settlement_day"):
            _validate(s)

    def test_historical_sync_backfill_values_are_validated(self):
        s = Settings()
        s.historical.sync.date_backfill_stride_days = 0
        with pytest.raises(ValueError, match="date_backfill_stride_days"):
            _validate(s)

    def test_learned_model_ev_gate_values_are_validated(self):
        s = Settings()
        s.strategy.learned_model.max_candidate_entry_price = 0
        with pytest.raises(ValueError, match="max_candidate_entry_price"):
            _validate(s)
