from __future__ import annotations

import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Literal, get_args, get_origin, get_type_hints

import yaml
from dotenv import load_dotenv

load_dotenv()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_string_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        return [part.strip() for part in stripped.split(",") if part.strip()]
    rendered = str(value).strip()
    return [rendered] if rendered else []


@dataclass
class ExchangeConfig:
    platform: str = "polymarket"
    clob_url: str = "https://clob.polymarket.com"
    gamma_url: str = "https://gamma-api.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    chain_id: int = 137


@dataclass
class ExitConfig:
    stop_loss_pct: float = 0.15        # close if unrealized loss >= 15% of entry cost
    take_profit_pct: float = 0.25      # close if unrealized gain >= 25% of entry cost
    time_exit_hours: float = 0.0       # close after N hours (0 = disabled, rely on settlement)
    enabled: bool = True


@dataclass
class StrategyConfig:
    edge_threshold: float = 0.02
    kelly_fraction: float = 0.25
    stake_min: float = 1.0
    stake_max: float = 2.0
    cycle_interval_sec: int = 60
    market_refresh_sec: int = 60
    live_decision_interval_sec: int = 5
    max_markets_per_cycle: int = 150
    fee_rate: float = 0.02
    max_spread: float = 0.15
    exit: ExitConfig = field(default_factory=ExitConfig)
    hypotheses: list[str] = field(default_factory=lambda: ["H2", "H4"])
    learned_model: "StrategyLearnedModelConfig" = field(default_factory=lambda: StrategyLearnedModelConfig())
    crypto15m_model: "StrategyCrypto15mModelConfig" = field(default_factory=lambda: StrategyCrypto15mModelConfig())


@dataclass
class LabUniverseConfig:
    max_markets: int = 150
    max_horizon_days: int = 30


@dataclass
class LabSchedulerConfig:
    market_refresh_sec: int = 60
    signal_eval_sec: int = 2
    equity_sample_sec: int = 10


@dataclass
class LabWsQualityConfig:
    enabled: bool = True
    freeze_on_stale: bool = True
    freeze_below_health_score: float = 0.45
    gap_burst_threshold: int = 3
    persist_interval_sec: int = 15
    alert_disconnect: bool = True
    alert_stale: bool = True
    alert_gap_burst: bool = True


@dataclass
class LabExecutionConfig:
    ttl_sec: int = 30
    reprice_sec: int = 5
    max_reprices: int = 3
    force_exit_after_failed_reprices: int = 3
    tick_size_default: float = 0.01
    latency_penalty_bps: float = 2.0
    max_event_age_ms: int = 2500


@dataclass
class LabMarketQualityConfig:
    enabled: bool = True
    min_score_default: float = 60.0
    require_end_date: bool = True
    require_yes_no_pair: bool = True
    hard_block_keywords_social: list[str] = field(default_factory=lambda: [
        "tweet",
        "tweets",
        "post",
        "posts",
        "followers",
        "likes",
        "mentions",
        "retweets",
        "views",
        "subscribers",
    ])
    hard_block_keywords_dispute: list[str] = field(default_factory=lambda: [
        "control",
        "ceasefire",
        "withdraw",
        "under control",
        "sovereignty",
        "occupation",
    ])
    weak_category_penalty: float = 15.0
    politics_penalty: float = 10.0
    fee_penalty: float = 10.0


@dataclass
class LabLateStageConfig:
    enabled: bool = True
    portfolios: list[str] = field(default_factory=lambda: ["Late_balanced", "Late_aggressive"])
    persistence_sec: int = 120
    imbalance_ratio_min: float = 2.5
    extreme_yes_min: float = 0.92
    extreme_yes_max: float = 0.08
    taker_entry_minutes: int = 15
    take_profit_pct: float = 0.015
    stop_loss_pct: float = 0.01
    max_hold_minutes: int = 30


@dataclass
class LabCrypto15mConfig:
    enabled: bool = True
    trade_assets: list[str] = field(default_factory=lambda: ["BTC"])
    max_horizon_hours: float = 2.0
    max_open_positions: int = 1
    entry_cooldown_sec: int = 900
    stop_min_hold_sec: int = 90
    candidate_window_minutes: int = 15
    candidate_min_time_to_resolution_sec: int = 180
    candidate_target_time_to_resolution_sec: int = 480
    candidate_target_tolerance_sec: int = 180
    min_entry_price: float = 0.30
    max_entry_price: float = 0.80
    min_abs_return_zscore_15m: float = 0.50
    min_trend_consistency_15m: float = 0.55
    signal_eval_sec: int = 1
    min_depth_usd: float = 100.0
    max_spread: float = 0.04
    min_net_ev: float = 0.003
    max_poly_return_abs: float = 1.0
    maker_wait_sec: int = 8
    allow_taker_entry: bool = True
    taker_entry_minutes: float = 20.0
    min_confidence: float = 0.55
    momentum_threshold: float = 0.003
    allow_no_trade_fallback: bool = True
    no_trade_fallback_max_probability: float = 0.86
    ab_thresholds: list[float] = field(default_factory=lambda: [0.80, 0.90, 0.95])
    control_min_confidence: float = 0.55
    live_ohlcv_enabled: bool = True
    live_ohlcv_poll_sec: int = 10
    live_ohlcv_lookback_minutes: int = 120
    live_ohlcv_stale_sec: int = 90
    shadow_stake_max: float = 2.0
    post_ab_shadow_stake_max: float = 5.0
    slug_discovery_enabled: bool = True
    slug_discovery_behind_intervals: int = 1
    slug_discovery_ahead_intervals: int = 4
    slug_discovery_concurrency: int = 8
    reward_guard_enabled: bool = True
    reward_min_score: float = 0.0
    reward_size_penalty: float = 0.001
    reward_fill_penalty: float = 0.004
    reward_drawdown_penalty: float = 0.25
    reward_stop_streak_penalty: float = 0.006
    reward_market_exit_penalty: float = 0.004
    reward_min_fill_rate: float = 0.15
    reward_stop_streak_limit: int = 2
    reward_lookback_trades: int = 12
    reward_lookback_orders: int = 30
    reward_lookback_hours: float = 24.0
    reward_strong_signal_min_score: float = 0.25
    reward_strong_signal_min_edge: float = 0.30
    side_regime_guard_enabled: bool = True
    side_regime_min_trades: int = 3
    side_regime_min_avg_pnl: float = 0.0
    ai_analyst: "LabCrypto15mAiAnalystConfig" = field(default_factory=lambda: LabCrypto15mAiAnalystConfig())


@dataclass
class LabCrypto15mAiAnalystConfig:
    enabled: bool = False
    shadow_only: bool = True
    provider: str = "qwen"
    model: str = "qwen-turbo"
    endpoint: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions"
    timeout_sec: float = 8.0
    temperature: float = 0.1
    top_p: float = 0.8
    max_tokens: int = 120
    market_cooldown_sec: int = 180
    cache_ttl_sec: int = 180
    max_calls_per_hour: int = 24
    min_signal_confidence: float = 0.65
    min_signal_edge: float = 0.003
    min_time_to_resolution_sec: int = 90
    max_time_to_resolution_sec: int = 1200
    analyst_only_below_confidence: float = 0.0


@dataclass
class LabPortfolioConfig:
    key: str
    hypotheses: list[str]
    combo_mode: bool = False
    pack: str = "base"
    track: str = "control"
    ab_group: str = "single"
    base_key: str = ""
    use_learned_gate: bool = True
    max_horizon_days: int = 30
    min_daily_volume: float = 500.0
    min_depth_usd: float = 50.0
    max_spread: float = 0.15
    min_quality_score: float = 60.0
    time_to_resolution_max_hours: float | None = None
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    max_hold_hours: float | None = None
    allow_taker_entry_minutes: float | None = None
    crypto15m_confidence_threshold: float | None = None
    stake_max_override: float | None = None
    use_ai_analyst: bool = False


def _default_lab_portfolios() -> list["LabPortfolioConfig"]:
    return [
        LabPortfolioConfig(
            key="H2_base",
            hypotheses=["H2"],
            pack="base",
            track="control",
            max_horizon_days=30,
            min_daily_volume=500.0,
            min_depth_usd=50.0,
            max_spread=0.15,
            min_quality_score=60.0,
        ),
        LabPortfolioConfig(
            key="H4_base",
            hypotheses=["H4"],
            pack="base",
            track="control",
            max_horizon_days=30,
            min_daily_volume=500.0,
            min_depth_usd=50.0,
            max_spread=0.15,
            min_quality_score=60.0,
        ),
        LabPortfolioConfig(
            key="Combo_base",
            hypotheses=["H2", "H4"],
            combo_mode=True,
            pack="base",
            track="control",
            max_horizon_days=30,
            min_daily_volume=500.0,
            min_depth_usd=50.0,
            max_spread=0.15,
            min_quality_score=60.0,
        ),
        LabPortfolioConfig(
            key="H2_strict",
            hypotheses=["H2"],
            pack="strict",
            track="control",
            max_horizon_days=7,
            min_daily_volume=2000.0,
            min_depth_usd=200.0,
            max_spread=0.05,
            min_quality_score=70.0,
        ),
        LabPortfolioConfig(
            key="H4_strict",
            hypotheses=["H4"],
            pack="strict",
            track="control",
            max_horizon_days=7,
            min_daily_volume=2000.0,
            min_depth_usd=200.0,
            max_spread=0.05,
            min_quality_score=70.0,
        ),
        LabPortfolioConfig(
            key="Combo_strict",
            hypotheses=["H2", "H4"],
            combo_mode=True,
            pack="strict",
            track="control",
            max_horizon_days=7,
            min_daily_volume=2000.0,
            min_depth_usd=200.0,
            max_spread=0.05,
            min_quality_score=70.0,
        ),
        LabPortfolioConfig(
            key="Late_balanced",
            hypotheses=["H6"],
            pack="late_balanced",
            track="late_stage",
            max_horizon_days=1,
            min_daily_volume=5000.0,
            min_depth_usd=500.0,
            max_spread=0.03,
            min_quality_score=70.0,
            time_to_resolution_max_hours=6.0,
            stop_loss_pct=0.01,
            take_profit_pct=0.015,
            max_hold_hours=0.5,
            allow_taker_entry_minutes=15.0,
        ),
        LabPortfolioConfig(
            key="Late_aggressive",
            hypotheses=["H6"],
            pack="late_aggressive",
            track="late_stage",
            max_horizon_days=1,
            min_daily_volume=2000.0,
            min_depth_usd=200.0,
            max_spread=0.05,
            min_quality_score=55.0,
            time_to_resolution_max_hours=12.0,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            max_hold_hours=0.5,
            allow_taker_entry_minutes=15.0,
        ),
        LabPortfolioConfig(
            key="Crypto15m",
            hypotheses=["H7"],
            pack="crypto15m",
            track="crypto15m",
            max_horizon_days=1,
            min_daily_volume=100.0,
            min_depth_usd=100.0,
            max_spread=0.04,
            min_quality_score=55.0,
            time_to_resolution_max_hours=2.0,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            max_hold_hours=0.25,
            allow_taker_entry_minutes=20.0,
            stake_max_override=2.0,
        ),
    ]


@dataclass
class LabConfig:
    enabled: bool = True
    mode: str = "shadow_maker"
    universe: LabUniverseConfig = field(default_factory=LabUniverseConfig)
    scheduler: LabSchedulerConfig = field(default_factory=LabSchedulerConfig)
    ab_testing: "LabAbTestingConfig" = field(default_factory=lambda: LabAbTestingConfig())
    ws_quality: LabWsQualityConfig = field(default_factory=LabWsQualityConfig)
    execution: LabExecutionConfig = field(default_factory=LabExecutionConfig)
    market_quality: LabMarketQualityConfig = field(default_factory=LabMarketQualityConfig)
    late_stage: LabLateStageConfig = field(default_factory=LabLateStageConfig)
    crypto15m: LabCrypto15mConfig = field(default_factory=LabCrypto15mConfig)
    portfolios: list[LabPortfolioConfig] = field(default_factory=_default_lab_portfolios)
    exit_time_hours: float = 72.0


@dataclass
class LabAbTestingConfig:
    enabled: bool = True
    control_suffix: str = "control"
    learned_suffix: str = "learned"


@dataclass
class RiskConfig:
    max_positions: int = 30
    max_concentration: float = 0.10
    daily_loss_limit: float = 0.05
    weekly_loss_limit: float = 0.10
    total_drawdown_stop: float = 0.20
    max_correlated_exposure: float = 0.15


@dataclass
class LiquidityConfig:
    min_daily_volume: float = 500
    min_depth_usd: float = 50
    max_price_impact: float = 0.005
    min_spread: float = 0.001


@dataclass
class BankrollConfig:
    initial: float = 500
    currency: str = "USDC"


@dataclass
class DatabaseConfig:
    url: str = f"sqlite:///{_PROJECT_ROOT / 'prediction_trader.db'}"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = str(_PROJECT_ROOT / "logs" / "trader.log")
    rotation: str = "10 MB"
    retention: str = "30 days"


@dataclass
class AlertsConfig:
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Empty = single-user mode (only telegram_chat_id). Non-empty = admins-only mode.
    telegram_admin_chat_id: str = ""
    telegram_admin_chat_ids: list[str] = field(default_factory=list)


@dataclass
class TelegramUiConfig:
    webapp_url: str = ""
    dev_initdata_bypass: bool = True
    dashboard_mode: str = "ssh_hint"


@dataclass
class DashboardConfig:
    base_url: str = "http://127.0.0.1:8090"
    host: str = "127.0.0.1"
    port: int = 8090
    locale: str = "ru"
    auto_refresh_sec: int = 5
    ssh_tunnel_hint: str = "ssh -L 8090:127.0.0.1:8090 user@server"
    local_url_hint: str = "http://127.0.0.1:8090/dashboard"


@dataclass
class HistoricalBootstrapConfig:
    source_paths: list[str] = field(default_factory=list)
    out_dir: str = str(_PROJECT_ROOT / "data" / "polymarket_historical")


@dataclass
class HistoricalSyncConfig:
    out_dir: str = str(_PROJECT_ROOT / "data" / "polymarket_historical")
    page_limit: int = 20
    batch_size: int = 100
    date_backfill_days: int = 0
    date_backfill_stride_days: int = 30
    date_backfill_page_limit: int = 3


@dataclass
class HistoricalPriceWindowConfig:
    out_dir: str = str(_PROJECT_ROOT / "data" / "polymarket_historical")
    pre_event_minutes: int = 60
    post_event_minutes: int = 30
    fidelity_sec: int = 60
    concurrency: int = 8
    max_markets_per_settlement_day: int = 12
    max_markets_per_run: int = 250
    min_markets_required: int = 100
    min_coverage_days: int = 30
    fail_on_insufficient_history: bool = True


@dataclass
class HistoricalConfig:
    bootstrap: HistoricalBootstrapConfig = field(default_factory=HistoricalBootstrapConfig)
    sync: HistoricalSyncConfig = field(default_factory=HistoricalSyncConfig)
    price_window: HistoricalPriceWindowConfig = field(default_factory=HistoricalPriceWindowConfig)


@dataclass
class CryptoDataConfig:
    enabled: bool = True
    exchange_primary: str = "binance"
    exchange_fallbacks: list[str] = field(default_factory=lambda: ["okx", "bybit"])
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT"])
    timeframes: list[str] = field(default_factory=lambda: ["1m", "5m", "15m"])
    history_days: int = 90
    out_dir: str = str(_PROJECT_ROOT / "data" / "crypto_ohlcv")


@dataclass
class StrategyLearnedModelConfig:
    enabled: bool = True
    artifact_path: str = str(_PROJECT_ROOT / "research" / "artifacts" / "learned_model" / "latest_manifest.json")
    require_accepted_artifact: bool = True
    veto_margin: float = 0.005
    min_candidate_confidence: float = 0.65
    min_candidate_net_ev: float = 0.001
    max_candidate_entry_price: float = 0.995
    estimated_slippage: float = 0.0
    candidate_stride_sec: int = 300
    reload_interval_sec: int = 300


@dataclass
class StrategyCrypto15mModelConfig:
    enabled: bool = True
    artifact_path: str = str(_PROJECT_ROOT / "research" / "artifacts" / "crypto15m" / "latest_manifest.json")
    require_accepted_artifact: bool = False
    min_confidence: float = 0.55
    min_net_ev: float = 0.003
    reload_interval_sec: int = 300


@dataclass
class ResearchHoldoutsConfig:
    window_days: int = 90
    windows_count: int = 3
    min_high_conf_accuracy: float = 0.95
    max_calibration_error: float = 0.08
    min_rows_per_holdout: int = 100
    min_high_conf_count_per_holdout: int = 25
    monitoring_export_dir: str = str(_PROJECT_ROOT / "research" / "artifacts" / "monitoring")
    grafana_dir: str = str(_PROJECT_ROOT / "research" / "artifacts" / "grafana")


@dataclass
class ResearchConfig:
    holdouts: ResearchHoldoutsConfig = field(default_factory=ResearchHoldoutsConfig)


@dataclass
class CollectorConfig:
    interval_sec: int = 300
    settlement_check_sec: int = 900


@dataclass
class Settings:
    mode: Literal["dry_run", "paper", "live", "shadow_maker"] = "dry_run"
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    lab: LabConfig = field(default_factory=LabConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    liquidity: LiquidityConfig = field(default_factory=LiquidityConfig)
    bankroll: BankrollConfig = field(default_factory=BankrollConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    telegram: TelegramUiConfig = field(default_factory=TelegramUiConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    historical: HistoricalConfig = field(default_factory=HistoricalConfig)
    crypto_data: CryptoDataConfig = field(default_factory=CryptoDataConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)

    # Secrets from environment (never stored in yaml)
    polygon_private_key: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    qwen_api_key: str = ""


def _dict_to_dataclass(cls, data: dict):
    """Recursively map nested dicts to dataclass fields."""
    if not data:
        return cls()
    import dataclasses as dc

    fieldtypes = get_type_hints(cls)
    kwargs = {}
    for k, v in data.items():
        if k not in fieldtypes:
            continue
        field_type = fieldtypes[k]
        origin = get_origin(field_type)
        args = get_args(field_type)

        if isinstance(v, dict):
            nested_cls = None
            if dc.is_dataclass(field_type):
                nested_cls = field_type
            else:
                factory = next((f.default_factory for f in dc.fields(cls) if f.name == k), None)
                if factory:
                    try:
                        sample = factory()
                        if dc.is_dataclass(type(sample)):
                            nested_cls = type(sample)
                    except TypeError:
                        nested_cls = None
            kwargs[k] = _dict_to_dataclass(nested_cls, v) if nested_cls else v
            continue

        if isinstance(v, list) and origin is list and args:
            item_type = args[0]
            if dc.is_dataclass(item_type):
                kwargs[k] = [
                    _dict_to_dataclass(item_type, item) if isinstance(item, dict) else item
                    for item in v
                ]
            else:
                kwargs[k] = v
            continue

        kwargs[k] = v
    return cls(**kwargs)


def load_settings(path: str | Path | None = None) -> Settings:
    if path is None:
        path = _PROJECT_ROOT / "config" / "settings.yaml"
    path = Path(path)

    raw: dict = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    settings = _dict_to_dataclass(Settings, raw)

    settings.polygon_private_key = os.getenv("POLYGON_PRIVATE_KEY", "")
    settings.polymarket_api_key = os.getenv("POLYMARKET_API_KEY", "")
    settings.polymarket_api_secret = os.getenv("POLYMARKET_API_SECRET", "")
    settings.polymarket_api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")
    settings.qwen_api_key = os.getenv("QWEN_API_KEY", "")
    qwen_model = os.getenv("QWEN_MODEL", "").strip()
    if qwen_model:
        settings.lab.crypto15m.ai_analyst.model = qwen_model
    qwen_endpoint = os.getenv("QWEN_ENDPOINT", "").strip()
    if qwen_endpoint:
        settings.lab.crypto15m.ai_analyst.endpoint = qwen_endpoint

    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if tg_token:
        settings.alerts.telegram_bot_token = tg_token
    if tg_chat:
        settings.alerts.telegram_chat_id = tg_chat
    tg_admin = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
    tg_admins = os.getenv("TELEGRAM_ADMIN_CHAT_IDS", "")
    admin_ids = []
    admin_ids.extend(_parse_string_list(settings.alerts.telegram_admin_chat_ids))
    admin_ids.extend(_parse_string_list(settings.alerts.telegram_admin_chat_id))
    admin_ids.extend(_parse_string_list(tg_admin))
    admin_ids.extend(_parse_string_list(tg_admins))
    if admin_ids:
        deduped = list(dict.fromkeys(admin_ids))
        settings.alerts.telegram_admin_chat_ids = deduped
        settings.alerts.telegram_admin_chat_id = deduped[0]
    tg_webapp = os.getenv("TELEGRAM_WEBAPP_URL", "")
    if tg_webapp:
        settings.telegram.webapp_url = tg_webapp.strip()
    dashboard_base = os.getenv("DASHBOARD_BASE_URL", "")
    if dashboard_base:
        settings.dashboard.base_url = dashboard_base.strip()
    database_url = os.getenv("PREDICTION_TRADER_DATABASE_URL", "").strip()
    if database_url:
        settings.database.url = database_url

    _validate(settings)
    return settings


def _validate(s: Settings):
    """Fail fast on obviously wrong config values."""
    errors = []
    if s.strategy.edge_threshold < 0:
        errors.append("strategy.edge_threshold must be >= 0")
    if not (0 < s.strategy.kelly_fraction <= 1):
        errors.append("strategy.kelly_fraction must be in (0, 1]")
    if s.strategy.stake_min > s.strategy.stake_max:
        errors.append("strategy.stake_min must be <= stake_max")
    if s.strategy.stake_min < 0:
        errors.append("strategy.stake_min must be >= 0")
    if s.strategy.cycle_interval_sec < 1:
        errors.append("strategy.cycle_interval_sec must be >= 1")
    if s.strategy.market_refresh_sec < 1:
        errors.append("strategy.market_refresh_sec must be >= 1")
    if s.strategy.live_decision_interval_sec < 1:
        errors.append("strategy.live_decision_interval_sec must be >= 1")
    if s.strategy.max_markets_per_cycle < 1:
        errors.append("strategy.max_markets_per_cycle must be >= 1")
    if s.lab.universe.max_markets < 1:
        errors.append("lab.universe.max_markets must be >= 1")
    if s.lab.universe.max_horizon_days < 1:
        errors.append("lab.universe.max_horizon_days must be >= 1")
    if s.lab.scheduler.market_refresh_sec < 1:
        errors.append("lab.scheduler.market_refresh_sec must be >= 1")
    if s.lab.scheduler.signal_eval_sec < 1:
        errors.append("lab.scheduler.signal_eval_sec must be >= 1")
    if s.lab.scheduler.equity_sample_sec < 1:
        errors.append("lab.scheduler.equity_sample_sec must be >= 1")
    if not s.lab.ab_testing.control_suffix.strip():
        errors.append("lab.ab_testing.control_suffix must not be empty")
    if not s.lab.ab_testing.learned_suffix.strip():
        errors.append("lab.ab_testing.learned_suffix must not be empty")
    if not (0.0 <= s.lab.ws_quality.freeze_below_health_score <= 1.0):
        errors.append("lab.ws_quality.freeze_below_health_score must be in [0, 1]")
    if s.lab.ws_quality.gap_burst_threshold < 1:
        errors.append("lab.ws_quality.gap_burst_threshold must be >= 1")
    if s.lab.ws_quality.persist_interval_sec < 1:
        errors.append("lab.ws_quality.persist_interval_sec must be >= 1")
    if s.lab.execution.ttl_sec < 1:
        errors.append("lab.execution.ttl_sec must be >= 1")
    if s.lab.execution.reprice_sec < 1:
        errors.append("lab.execution.reprice_sec must be >= 1")
    if s.lab.execution.max_reprices < 0:
        errors.append("lab.execution.max_reprices must be >= 0")
    if s.lab.execution.force_exit_after_failed_reprices < 0:
        errors.append("lab.execution.force_exit_after_failed_reprices must be >= 0")
    if s.lab.exit_time_hours <= 0:
        errors.append("lab.exit_time_hours must be > 0")
    if not s.lab.portfolios:
        errors.append("lab.portfolios must not be empty")
    if s.lab.market_quality.min_score_default < 0 or s.lab.market_quality.min_score_default > 100:
        errors.append("lab.market_quality.min_score_default must be in [0, 100]")
    if s.lab.late_stage.persistence_sec < 1:
        errors.append("lab.late_stage.persistence_sec must be >= 1")
    if s.lab.late_stage.imbalance_ratio_min <= 0:
        errors.append("lab.late_stage.imbalance_ratio_min must be > 0")
    if not (0 < s.lab.late_stage.extreme_yes_min < 1):
        errors.append("lab.late_stage.extreme_yes_min must be in (0, 1)")
    if not (0 < s.lab.late_stage.extreme_yes_max < 1):
        errors.append("lab.late_stage.extreme_yes_max must be in (0, 1)")
    if s.lab.crypto15m.max_horizon_hours <= 0:
        errors.append("lab.crypto15m.max_horizon_hours must be > 0")
    if s.lab.crypto15m.max_open_positions < 1:
        errors.append("lab.crypto15m.max_open_positions must be >= 1")
    if s.lab.crypto15m.entry_cooldown_sec < 0:
        errors.append("lab.crypto15m.entry_cooldown_sec must be >= 0")
    if s.lab.crypto15m.stop_min_hold_sec < 0:
        errors.append("lab.crypto15m.stop_min_hold_sec must be >= 0")
    if not s.lab.crypto15m.trade_assets:
        errors.append("lab.crypto15m.trade_assets must not be empty")
    for asset in s.lab.crypto15m.trade_assets:
        if str(asset).upper() not in {"BTC", "ETH"}:
            errors.append("lab.crypto15m.trade_assets values must be BTC or ETH")
            break
    if s.lab.crypto15m.candidate_window_minutes < 1:
        errors.append("lab.crypto15m.candidate_window_minutes must be >= 1")
    if s.lab.crypto15m.candidate_min_time_to_resolution_sec < 0:
        errors.append("lab.crypto15m.candidate_min_time_to_resolution_sec must be >= 0")
    if s.lab.crypto15m.candidate_target_time_to_resolution_sec <= 0:
        errors.append("lab.crypto15m.candidate_target_time_to_resolution_sec must be > 0")
    if s.lab.crypto15m.candidate_target_tolerance_sec < 0:
        errors.append("lab.crypto15m.candidate_target_tolerance_sec must be >= 0")
    if not (0.0 < s.lab.crypto15m.max_entry_price < 1.0):
        errors.append("lab.crypto15m.max_entry_price must be in (0, 1)")
    if not (0.0 <= s.lab.crypto15m.min_entry_price < s.lab.crypto15m.max_entry_price):
        errors.append("lab.crypto15m.min_entry_price must be >= 0 and < max_entry_price")
    if s.lab.crypto15m.min_abs_return_zscore_15m < 0:
        errors.append("lab.crypto15m.min_abs_return_zscore_15m must be >= 0")
    if not (0.0 <= s.lab.crypto15m.min_trend_consistency_15m <= 1.0):
        errors.append("lab.crypto15m.min_trend_consistency_15m must be in [0, 1]")
    if s.lab.crypto15m.signal_eval_sec < 1:
        errors.append("lab.crypto15m.signal_eval_sec must be >= 1")
    if s.lab.crypto15m.min_depth_usd < 0:
        errors.append("lab.crypto15m.min_depth_usd must be >= 0")
    if not (0.0 < s.lab.crypto15m.max_spread <= 1.0):
        errors.append("lab.crypto15m.max_spread must be in (0, 1]")
    if s.lab.crypto15m.min_net_ev < 0:
        errors.append("lab.crypto15m.min_net_ev must be >= 0")
    if s.lab.crypto15m.max_poly_return_abs <= 0:
        errors.append("lab.crypto15m.max_poly_return_abs must be > 0")
    if s.lab.crypto15m.maker_wait_sec < 1:
        errors.append("lab.crypto15m.maker_wait_sec must be >= 1")
    if not (0.0 <= s.lab.crypto15m.min_confidence <= 1.0):
        errors.append("lab.crypto15m.min_confidence must be in [0, 1]")
    if not (0.0 <= s.lab.crypto15m.no_trade_fallback_max_probability <= 1.0):
        errors.append("lab.crypto15m.no_trade_fallback_max_probability must be in [0, 1]")
    if not s.lab.crypto15m.ab_thresholds:
        errors.append("lab.crypto15m.ab_thresholds must not be empty")
    for threshold in s.lab.crypto15m.ab_thresholds:
        if not (0.0 <= float(threshold) <= 1.0):
            errors.append("lab.crypto15m.ab_thresholds values must be in [0, 1]")
            break
    if not (0.0 <= s.lab.crypto15m.control_min_confidence <= 1.0):
        errors.append("lab.crypto15m.control_min_confidence must be in [0, 1]")
    if s.lab.crypto15m.live_ohlcv_poll_sec < 1:
        errors.append("lab.crypto15m.live_ohlcv_poll_sec must be >= 1")
    if s.lab.crypto15m.live_ohlcv_lookback_minutes < 60:
        errors.append("lab.crypto15m.live_ohlcv_lookback_minutes must be >= 60")
    if s.lab.crypto15m.live_ohlcv_stale_sec < 1:
        errors.append("lab.crypto15m.live_ohlcv_stale_sec must be >= 1")
    if s.lab.crypto15m.shadow_stake_max < s.strategy.stake_min:
        errors.append("lab.crypto15m.shadow_stake_max must be >= strategy.stake_min")
    if s.lab.crypto15m.post_ab_shadow_stake_max < s.lab.crypto15m.shadow_stake_max:
        errors.append("lab.crypto15m.post_ab_shadow_stake_max must be >= shadow_stake_max")
    if s.lab.crypto15m.slug_discovery_behind_intervals < 0:
        errors.append("lab.crypto15m.slug_discovery_behind_intervals must be >= 0")
    if s.lab.crypto15m.slug_discovery_ahead_intervals < 0:
        errors.append("lab.crypto15m.slug_discovery_ahead_intervals must be >= 0")
    if s.lab.crypto15m.slug_discovery_concurrency < 1:
        errors.append("lab.crypto15m.slug_discovery_concurrency must be >= 1")
    if s.lab.crypto15m.reward_min_score < -1.0:
        errors.append("lab.crypto15m.reward_min_score must be >= -1")
    for name in (
        "reward_size_penalty",
        "reward_fill_penalty",
        "reward_drawdown_penalty",
        "reward_stop_streak_penalty",
        "reward_market_exit_penalty",
    ):
        if getattr(s.lab.crypto15m, name) < 0:
            errors.append(f"lab.crypto15m.{name} must be >= 0")
    if not (0.0 <= s.lab.crypto15m.reward_min_fill_rate <= 1.0):
        errors.append("lab.crypto15m.reward_min_fill_rate must be in [0, 1]")
    if s.lab.crypto15m.reward_stop_streak_limit < 1:
        errors.append("lab.crypto15m.reward_stop_streak_limit must be >= 1")
    if s.lab.crypto15m.reward_lookback_trades < 1:
        errors.append("lab.crypto15m.reward_lookback_trades must be >= 1")
    if s.lab.crypto15m.reward_lookback_orders < 1:
        errors.append("lab.crypto15m.reward_lookback_orders must be >= 1")
    if s.lab.crypto15m.reward_lookback_hours <= 0:
        errors.append("lab.crypto15m.reward_lookback_hours must be > 0")
    if s.lab.crypto15m.reward_strong_signal_min_score < 0:
        errors.append("lab.crypto15m.reward_strong_signal_min_score must be >= 0")
    if s.lab.crypto15m.reward_strong_signal_min_edge < 0:
        errors.append("lab.crypto15m.reward_strong_signal_min_edge must be >= 0")
    if s.lab.crypto15m.side_regime_min_trades < 1:
        errors.append("lab.crypto15m.side_regime_min_trades must be >= 1")
    if s.dashboard.auto_refresh_sec < 1:
        errors.append("dashboard.auto_refresh_sec must be >= 1")
    if s.historical.sync.page_limit < 1:
        errors.append("historical.sync.page_limit must be >= 1")
    if s.historical.sync.batch_size < 1:
        errors.append("historical.sync.batch_size must be >= 1")
    if s.historical.sync.date_backfill_days < 0:
        errors.append("historical.sync.date_backfill_days must be >= 0")
    if s.historical.sync.date_backfill_stride_days < 1:
        errors.append("historical.sync.date_backfill_stride_days must be >= 1")
    if s.historical.sync.date_backfill_page_limit < 1:
        errors.append("historical.sync.date_backfill_page_limit must be >= 1")
    if s.historical.price_window.pre_event_minutes < 1:
        errors.append("historical.price_window.pre_event_minutes must be >= 1")
    if s.historical.price_window.post_event_minutes < 0:
        errors.append("historical.price_window.post_event_minutes must be >= 0")
    if s.historical.price_window.fidelity_sec < 1:
        errors.append("historical.price_window.fidelity_sec must be >= 1")
    if s.historical.price_window.concurrency < 1:
        errors.append("historical.price_window.concurrency must be >= 1")
    if s.historical.price_window.max_markets_per_settlement_day < 0:
        errors.append("historical.price_window.max_markets_per_settlement_day must be >= 0")
    if s.historical.price_window.max_markets_per_run < 1:
        errors.append("historical.price_window.max_markets_per_run must be >= 1")
    if s.historical.price_window.min_markets_required < 1:
        errors.append("historical.price_window.min_markets_required must be >= 1")
    if s.historical.price_window.min_coverage_days < 1:
        errors.append("historical.price_window.min_coverage_days must be >= 1")
    if s.historical.price_window.min_markets_required > s.historical.price_window.max_markets_per_run:
        errors.append("historical.price_window.min_markets_required must be <= max_markets_per_run")
    if s.crypto_data.history_days < 1:
        errors.append("crypto_data.history_days must be >= 1")
    if not s.crypto_data.symbols:
        errors.append("crypto_data.symbols must not be empty")
    if not s.crypto_data.timeframes:
        errors.append("crypto_data.timeframes must not be empty")
    if s.research.holdouts.window_days < 7:
        errors.append("research.holdouts.window_days must be >= 7")
    if s.research.holdouts.windows_count < 1:
        errors.append("research.holdouts.windows_count must be >= 1")
    if not (0.0 <= s.research.holdouts.min_high_conf_accuracy <= 1.0):
        errors.append("research.holdouts.min_high_conf_accuracy must be in [0, 1]")
    if not (0.0 <= s.research.holdouts.max_calibration_error <= 1.0):
        errors.append("research.holdouts.max_calibration_error must be in [0, 1]")
    if s.research.holdouts.min_rows_per_holdout < 1:
        errors.append("research.holdouts.min_rows_per_holdout must be >= 1")
    if s.research.holdouts.min_high_conf_count_per_holdout < 0:
        errors.append("research.holdouts.min_high_conf_count_per_holdout must be >= 0")
    if not (0.0 <= s.strategy.learned_model.min_candidate_confidence <= 1.0):
        errors.append("strategy.learned_model.min_candidate_confidence must be in [0, 1]")
    if s.strategy.learned_model.min_candidate_net_ev < 0:
        errors.append("strategy.learned_model.min_candidate_net_ev must be >= 0")
    if not (0.0 < s.strategy.learned_model.max_candidate_entry_price <= 1.0):
        errors.append("strategy.learned_model.max_candidate_entry_price must be in (0, 1]")
    if s.strategy.learned_model.estimated_slippage < 0:
        errors.append("strategy.learned_model.estimated_slippage must be >= 0")
    if s.strategy.learned_model.candidate_stride_sec < 1:
        errors.append("strategy.learned_model.candidate_stride_sec must be >= 1")
    if s.strategy.learned_model.reload_interval_sec < 1:
        errors.append("strategy.learned_model.reload_interval_sec must be >= 1")
    if not (0.0 <= s.strategy.crypto15m_model.min_confidence <= 1.0):
        errors.append("strategy.crypto15m_model.min_confidence must be in [0, 1]")
    if s.strategy.crypto15m_model.min_net_ev < 0:
        errors.append("strategy.crypto15m_model.min_net_ev must be >= 0")
    if s.strategy.crypto15m_model.reload_interval_sec < 1:
        errors.append("strategy.crypto15m_model.reload_interval_sec must be >= 1")
    if not (0 <= s.strategy.fee_rate < 1):
        errors.append("strategy.fee_rate must be in [0, 1)")
    if s.risk.max_positions < 1:
        errors.append("risk.max_positions must be >= 1")
    if not (0 < s.risk.max_concentration <= 1):
        errors.append("risk.max_concentration must be in (0, 1]")
    if s.bankroll.initial <= 0:
        errors.append("bankroll.initial must be > 0")
    if s.mode == "live" and not s.polygon_private_key:
        errors.append("POLYGON_PRIVATE_KEY required for live mode")
    if s.lab.crypto15m.ai_analyst.timeout_sec <= 0:
        errors.append("lab.crypto15m.ai_analyst.timeout_sec must be > 0")
    if s.lab.crypto15m.ai_analyst.max_tokens < 1:
        errors.append("lab.crypto15m.ai_analyst.max_tokens must be >= 1")
    if s.lab.crypto15m.ai_analyst.market_cooldown_sec < 0:
        errors.append("lab.crypto15m.ai_analyst.market_cooldown_sec must be >= 0")
    if s.lab.crypto15m.ai_analyst.cache_ttl_sec < 0:
        errors.append("lab.crypto15m.ai_analyst.cache_ttl_sec must be >= 0")
    if s.lab.crypto15m.ai_analyst.max_calls_per_hour < 1:
        errors.append("lab.crypto15m.ai_analyst.max_calls_per_hour must be >= 1")
    if not (0.0 <= s.lab.crypto15m.ai_analyst.min_signal_confidence <= 1.0):
        errors.append("lab.crypto15m.ai_analyst.min_signal_confidence must be in [0, 1]")
    if s.lab.crypto15m.ai_analyst.min_signal_edge < 0:
        errors.append("lab.crypto15m.ai_analyst.min_signal_edge must be >= 0")
    if not (0.0 <= s.lab.crypto15m.ai_analyst.analyst_only_below_confidence <= 1.0):
        errors.append("lab.crypto15m.ai_analyst.analyst_only_below_confidence must be in [0, 1]")
    if s.lab.crypto15m.ai_analyst.min_time_to_resolution_sec < 0:
        errors.append("lab.crypto15m.ai_analyst.min_time_to_resolution_sec must be >= 0")
    if s.lab.crypto15m.ai_analyst.max_time_to_resolution_sec <= 0:
        errors.append("lab.crypto15m.ai_analyst.max_time_to_resolution_sec must be > 0")
    if errors:
        msg = "Config validation failed:\n  " + "\n  ".join(errors)
        raise ValueError(msg)
