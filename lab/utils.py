from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone

from config import Settings, LabPortfolioConfig


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_market_end_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def time_to_resolution_days(value: str | None, now: datetime | None = None) -> float | None:
    dt = parse_market_end_date(value)
    if dt is None:
        return None
    base = now or utcnow()
    return (dt - base).total_seconds() / 86400.0


def horizon_bucket(days: float | None) -> str:
    if days is None:
        return "unknown"
    if days <= 1:
        return "0-1d"
    if days <= 7:
        return "2-7d"
    if days <= 30:
        return "8-30d"
    return "30d+"


def portfolio_settings(settings: Settings, portfolio: LabPortfolioConfig) -> Settings:
    clone = deepcopy(settings)
    clone.strategy.hypotheses = list(portfolio.hypotheses)
    clone.strategy.max_spread = portfolio.max_spread
    clone.strategy.learned_model.enabled = bool(portfolio.use_learned_gate)
    if portfolio.crypto15m_confidence_threshold is not None:
        threshold = float(portfolio.crypto15m_confidence_threshold)
        clone.strategy.crypto15m_model.min_confidence = threshold
        clone.lab.crypto15m.min_confidence = threshold
    if portfolio.stake_max_override is not None:
        clone.strategy.stake_max = float(portfolio.stake_max_override)
    clone.strategy.exit.time_exit_hours = settings.lab.exit_time_hours
    clone.liquidity.min_daily_volume = portfolio.min_daily_volume
    clone.liquidity.min_depth_usd = portfolio.min_depth_usd
    return clone


def settings_snapshot_dict(settings: Settings, portfolio: LabPortfolioConfig) -> dict:
    snapshot = {
        "portfolio": asdict(portfolio),
        "strategy": asdict(settings.strategy),
        "risk": asdict(settings.risk),
        "liquidity": asdict(settings.liquidity),
        "lab_execution": asdict(settings.lab.execution),
        "lab_scheduler": asdict(settings.lab.scheduler),
    }
    return snapshot
