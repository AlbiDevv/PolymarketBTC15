from __future__ import annotations

import asyncio
import json
import pickle
import signal
import sys
import warnings
from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger
import numpy as np
import pandas as pd
from sqlalchemy import func

from config import LabPortfolioConfig, Settings
from db.models import (
    AuditRow,
    LabDecisionAuditRow,
    LabEquityPointRow,
    LabFillRow,
    LabOrderRow,
    LabPortfolioRow,
    LabPositionRow,
    LabRuntimeStatusRow,
    LabWsMetricRow,
    MarketRow,
    OrderbookRawRow,
    PriceHistoryRow,
    SignalRow,
)
from db.session import get_session, init_db
from exchange_client.base import Market, Orderbook, Token
from exchange_client.liquidity import LiquidityFilter
from exchange_client.polymarket import PolymarketClient
from market_data.orderbook_manager import OrderbookManager
from market_data.ws_client import PolymarketWebSocket
from models.hypothesis import (
    H2_RoundNumberBias,
    H4_UnderpricedTails,
    H6_LateStagePressure,
    H7_Crypto15mDirection,
    HypothesisBase,
    SignalOutput,
)
from monitor import runtime_state
from monitor.alerts import TelegramAlerter
from risk.limits import PositionInfo
from risk.manager import RiskManager
from research.crypto15m import CRYPTO15M_FEATURE_COLUMNS, classify_crypto15m_updown_market
from research.trade_costs import fee_rate_bps_to_fraction, polymarket_taker_fee_usdc

from .ai_analyst import AnalystReview, Crypto15mAiAnalyst
from .crypto_ohlcv_live import CryptoOHLCVLiveFeed
from .live_learning import LearnedGateDecision, LearnedModelGate
from .market_quality import MarketQualityAssessment, assess_market_quality
from .shadow_engine import ShadowFill, ShadowMakerEngine, ShadowOrderState, TakerFillPreview
from .utils import parse_market_end_date, portfolio_settings, settings_snapshot_dict, time_to_resolution_days, utcnow


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _prepare_runtime_model(model: Any) -> Any:
    n_jobs = getattr(model, "n_jobs", None)
    if isinstance(n_jobs, int) and n_jobs != 1:
        try:
            model.n_jobs = 1
        except Exception:
            pass
    return model


def _predict_safely(model: Any, features: pd.DataFrame) -> Any:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"sklearn\.utils\.parallel\.delayed should be used with sklearn\.utils\.parallel\.Parallel.*",
            category=UserWarning,
        )
        try:
            return model.predict(features)
        except (AttributeError, TypeError, ValueError):
            return model.predict(features.to_numpy())


def _predict_proba_safely(model: Any, features: pd.DataFrame) -> Any:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"sklearn\.utils\.parallel\.delayed should be used with sklearn\.utils\.parallel\.Parallel.*",
            category=UserWarning,
        )
        try:
            return model.predict_proba(features)
        except (AttributeError, TypeError, ValueError):
            return model.predict_proba(features.to_numpy())


@dataclass
class PortfolioRuntime:
    key: str
    config: LabPortfolioConfig
    settings: Settings
    liquidity: LiquidityFilter
    risk: RiskManager
    hypotheses: list[HypothesisBase]
    row_id: int = 0
    initial_bankroll: float = 0.0
    bankroll: float = 0.0
    peak_equity: float = 0.0


class ShadowLabRunner:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._db_url = settings.database.url
        self._client = PolymarketClient(settings)
        self._ob_manager = OrderbookManager()
        self._engine = ShadowMakerEngine(
            ttl_sec=settings.lab.execution.ttl_sec,
            reprice_sec=settings.lab.execution.reprice_sec,
            max_reprices=settings.lab.execution.max_reprices,
            tick_default=settings.lab.execution.tick_size_default,
            latency_penalty_bps=settings.lab.execution.latency_penalty_bps,
            max_event_age_ms=settings.lab.execution.max_event_age_ms,
        )
        self._ws: PolymarketWebSocket | None = None
        self._running = False
        self._state_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None
        self._signal_task: asyncio.Task | None = None
        self._next_equity_sample_at: datetime | None = None

        self._market_cache: list[Market] = []
        self._shortlist: list[Market] = []
        self._market_rows: dict[str, int] = {}
        self._token_to_market: dict[str, Market] = {}
        self._token_tick_sizes: dict[str, float] = {}
        self._desired_subscriptions: set[str] = set()
        self._market_mid_history: dict[str, deque[tuple[datetime, float]]] = defaultdict(deque)
        self._market_trade_history: dict[str, deque[tuple[datetime, str, float]]] = defaultdict(deque)
        self._market_microstate_last_at: dict[str, datetime] = {}
        self._event_eval_last_at: dict[str, datetime] = {}
        self._decision_fingerprints: dict[tuple[Any, ...], str] = {}
        self._runtime_status_id: int | None = None
        self._last_ws_metrics_persist_at: datetime | None = None
        self._last_ws_quality_alert: dict[str, datetime] = {}
        self._learned_gate = LearnedModelGate(settings)
        self._crypto15m_manifest: dict[str, Any] | None = None
        self._crypto15m_bundle: dict[str, Any] | None = None
        self._crypto15m_loaded_at: datetime | None = None
        self._crypto_ohlcv_feed = CryptoOHLCVLiveFeed(settings)
        self._fee_rate_bps_by_token: dict[str, float] = {}
        self._entries_frozen = False
        self._alerts = TelegramAlerter(
            settings.alerts.telegram_bot_token,
            settings.alerts.telegram_chat_id,
            enabled=settings.alerts.telegram_enabled,
        )
        self._ai_analyst = Crypto15mAiAnalyst(settings)

        self._portfolio_runtimes = self._build_portfolios(settings)
        self._portfolios_by_id: dict[int, PortfolioRuntime] = {}

        self._working_orders: dict[str, ShadowOrderState] = {}
        self._order_row_ids: dict[str, int] = {}
        self._orders_by_token: dict[str, set[str]] = defaultdict(set)

        init_db(self._db_url)

    @staticmethod
    def _build_portfolios(settings: Settings) -> list[PortfolioRuntime]:
        runtimes: list[PortfolioRuntime] = []
        for base_portfolio in settings.lab.portfolios:
            for portfolio in ShadowLabRunner._expand_portfolio_variants(settings, base_portfolio):
                scoped = portfolio_settings(settings, portfolio)
                runtimes.append(PortfolioRuntime(
                    key=portfolio.key,
                    config=portfolio,
                    settings=scoped,
                    liquidity=LiquidityFilter(scoped.liquidity),
                    risk=RiskManager(scoped, initial_bankroll=settings.bankroll.initial),
                    hypotheses=ShadowLabRunner._init_hypotheses(portfolio.hypotheses),
                    initial_bankroll=settings.bankroll.initial,
                    bankroll=settings.bankroll.initial,
                    peak_equity=settings.bankroll.initial,
                ))
        return runtimes

    @staticmethod
    def _expand_portfolio_variants(settings: Settings, portfolio: LabPortfolioConfig) -> list[LabPortfolioConfig]:
        base = deepcopy(portfolio)
        base.base_key = base.base_key or portfolio.key
        if not settings.lab.ab_testing.enabled:
            if base.ab_group == "single":
                base.ab_group = "learned"
            base.use_learned_gate = True
            base.use_ai_analyst = bool(settings.lab.crypto15m.ai_analyst.enabled)
            if base.track == "crypto15m":
                base.stake_max_override = base.stake_max_override or settings.lab.crypto15m.shadow_stake_max
            return [base]

        if base.track == "crypto15m":
            control = deepcopy(base)
            control.key = f"{portfolio.key}_{settings.lab.ab_testing.control_suffix}"
            control.ab_group = "control"
            control.base_key = portfolio.key
            control.use_learned_gate = False
            control.use_ai_analyst = False
            control.crypto15m_confidence_threshold = settings.lab.crypto15m.control_min_confidence
            control.stake_max_override = settings.lab.crypto15m.shadow_stake_max

            variants = [control]
            for threshold in settings.lab.crypto15m.ab_thresholds:
                learned = deepcopy(base)
                threshold_value = float(threshold)
                analyst_only_below = float(settings.lab.crypto15m.ai_analyst.analyst_only_below_confidence or 0.0)
                create_raw_learned = not (
                    settings.lab.crypto15m.ai_analyst.enabled
                    and analyst_only_below > 0
                    and threshold_value < analyst_only_below
                )
                if create_raw_learned:
                    learned.key = f"{portfolio.key}_t{int(round(threshold_value * 100)):02d}_{settings.lab.ab_testing.learned_suffix}"
                    learned.ab_group = f"learned_t{int(round(threshold_value * 100)):02d}"
                    learned.base_key = portfolio.key
                    learned.use_learned_gate = True
                    learned.use_ai_analyst = False
                    learned.crypto15m_confidence_threshold = threshold_value
                    learned.stake_max_override = settings.lab.crypto15m.shadow_stake_max
                    variants.append(learned)
                if settings.lab.crypto15m.ai_analyst.enabled:
                    analyst = deepcopy(base)
                    analyst.key = f"{portfolio.key}_t{int(round(threshold_value * 100)):02d}_analyst"
                    analyst.ab_group = f"analyst_t{int(round(threshold_value * 100)):02d}"
                    analyst.base_key = portfolio.key
                    analyst.use_learned_gate = True
                    analyst.use_ai_analyst = True
                    analyst.crypto15m_confidence_threshold = threshold_value
                    analyst.stake_max_override = settings.lab.crypto15m.shadow_stake_max
                    variants.append(analyst)
            return variants

        control = deepcopy(base)
        control.key = f"{portfolio.key}_{settings.lab.ab_testing.control_suffix}"
        control.ab_group = "control"
        control.base_key = portfolio.key
        control.use_learned_gate = False

        learned = deepcopy(base)
        learned.key = f"{portfolio.key}_{settings.lab.ab_testing.learned_suffix}"
        learned.ab_group = "learned"
        learned.base_key = portfolio.key
        learned.use_learned_gate = True

        return [control, learned]

    @staticmethod
    def _init_hypotheses(hypothesis_ids: list[str]) -> list[HypothesisBase]:
        registry: dict[str, type[HypothesisBase]] = {
            "H2": H2_RoundNumberBias,
            "H4": H4_UnderpricedTails,
            "H6": H6_LateStagePressure,
            "H7": H7_Crypto15mDirection,
        }
        active: list[HypothesisBase] = []
        for hypothesis_id in hypothesis_ids:
            cls = registry.get(hypothesis_id)
            if cls is None:
                logger.warning(f"Unknown lab hypothesis '{hypothesis_id}' skipped")
                continue
            active.append(cls())
        return active

    async def start(self):
        self._running = True
        runtime_state.mark_started()
        self._setup_signal_handlers()
        await self._crypto_ohlcv_feed.start()
        await self._bootstrap_state()
        await self._start_websocket()
        await self._refresh_market_universe()

        self._refresh_task = asyncio.create_task(self._refresh_loop())
        self._signal_task = asyncio.create_task(self._signal_loop())

        logger.info(
            "Shadow lab starting | portfolios={} | refresh={}s | signal_eval={}s".format(
                len(self._portfolio_runtimes),
                self._settings.lab.scheduler.market_refresh_sec,
                self._settings.lab.scheduler.signal_eval_sec,
            )
        )

        try:
            await asyncio.gather(self._refresh_task, self._signal_task)
        except asyncio.CancelledError:
            logger.info("Shadow lab cancelled")
        finally:
            await self._shutdown()

    async def stop(self):
        self._running = False
        for task in (self._refresh_task, self._signal_task):
            if task and not task.done():
                task.cancel()

    async def _bootstrap_state(self):
        session = get_session(self._db_url)
        try:
            runtime_row = session.query(LabRuntimeStatusRow).order_by(LabRuntimeStatusRow.id.asc()).first()
            if runtime_row is None:
                runtime_row = LabRuntimeStatusRow(
                    mode="shadow_maker",
                    started_at=utcnow(),
                    ws_connected=False,
                )
                session.add(runtime_row)
                session.flush()
            else:
                runtime_row.mode = "shadow_maker"
                runtime_row.started_at = runtime_row.started_at or utcnow()
                runtime_row.ws_connected = False
            self._runtime_status_id = runtime_row.id

            for runtime in self._portfolio_runtimes:
                row = (
                    session.query(LabPortfolioRow)
                    .filter(LabPortfolioRow.key == runtime.key)
                    .first()
                )
                if row is None:
                    row = LabPortfolioRow(
                        key=runtime.key,
                        mode="shadow_maker",
                        settings_json=settings_snapshot_dict(runtime.settings, runtime.config),
                        initial_bankroll=runtime.initial_bankroll,
                    )
                    session.add(row)
                    session.flush()
                else:
                    row.mode = "shadow_maker"
                    row.settings_json = settings_snapshot_dict(runtime.settings, runtime.config)
                    if row.initial_bankroll <= 0:
                        row.initial_bankroll = runtime.initial_bankroll

                runtime.row_id = row.id
                runtime.initial_bankroll = row.initial_bankroll or runtime.initial_bankroll

                latest_equity = (
                    session.query(LabEquityPointRow)
                    .filter(LabEquityPointRow.portfolio_id == row.id)
                    .order_by(LabEquityPointRow.timestamp.desc())
                    .first()
                )
                if latest_equity is not None:
                    runtime.bankroll = float(latest_equity.bankroll)
                    peak_equity = (
                        session.query(func.max(LabEquityPointRow.equity))
                        .filter(LabEquityPointRow.portfolio_id == row.id)
                        .scalar()
                    )
                    runtime.peak_equity = max(float(peak_equity or runtime.bankroll), runtime.bankroll)
                else:
                    realized = (
                        session.query(func.coalesce(func.sum(LabPositionRow.realized_pnl), 0.0))
                        .filter(LabPositionRow.portfolio_id == row.id)
                        .filter(LabPositionRow.status == "closed")
                        .scalar()
                    ) or 0.0
                    runtime.bankroll = runtime.initial_bankroll + float(realized)
                    runtime.peak_equity = runtime.bankroll

                stale_orders = (
                    session.query(LabOrderRow)
                    .filter(LabOrderRow.portfolio_id == row.id)
                    .filter(LabOrderRow.status.in_(("working", "partial")))
                    .all()
                )
                for stale in stale_orders:
                    stale.status = "cancelled"
                    stale.closed_at = utcnow()

                self._portfolios_by_id[row.id] = runtime

            session.add(AuditRow(
                timestamp=utcnow(),
                event_type="shadow_lab_bootstrap",
                details=json.dumps({
                    "portfolio_keys": [runtime.key for runtime in self._portfolio_runtimes],
                }),
            ))
            self._update_runtime_status_row(
                session,
                started_at=runtime_row.started_at,
                ws_connected=False,
                last_cycle_ok=True,
                last_cycle_error=None,
                cycle_failures_in_row=0,
                markets_fetched_last=0,
                eligible_markets_last=0,
                subscribed_tokens_last=0,
            )
            session.commit()
        finally:
            session.close()

    async def _start_websocket(self):
        if not self._settings.exchange.ws_url:
            return

        def _on_disconnect():
            runtime_state.set_ws_connected(False)
            self._ob_manager.invalidate_all()
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._persist_runtime_status_async(
                    ws_connected=False,
                    last_cycle_ok=False,
                    last_cycle_error="websocket_disconnected",
                ))
            except RuntimeError:
                pass

        self._ws = PolymarketWebSocket(
            ws_url=self._settings.exchange.ws_url,
            ob_manager=self._ob_manager,
            on_error=lambda exc: logger.warning(f"lab ws error: {exc}"),
            on_disconnect=_on_disconnect,
            on_event=self._on_ws_event,
            on_quality_alert=self._on_ws_quality_alert,
        )

    def _update_runtime_status_row(self, session, **updates):
        row = None
        if self._runtime_status_id is not None:
            row = session.get(LabRuntimeStatusRow, self._runtime_status_id)
        if row is None:
            row = session.query(LabRuntimeStatusRow).order_by(LabRuntimeStatusRow.id.asc()).first()
        if row is None:
            row = LabRuntimeStatusRow(mode="shadow_maker", started_at=utcnow())
            session.add(row)
            session.flush()
        self._runtime_status_id = row.id
        for key, value in updates.items():
            if hasattr(row, key):
                setattr(row, key, value)
        row.updated_at = utcnow()
        return row

    async def _persist_runtime_status_async(self, **updates):
        session = get_session(self._db_url)
        try:
            self._update_runtime_status_row(session, **updates)
            session.commit()
        finally:
            session.close()

    def _ws_snapshot(self) -> dict[str, Any]:
        if self._ws is None:
            return {
                "connected": False,
                "message_count": 0,
                "messages_per_minute": 0.0,
                "last_message_age_sec": -1.0,
                "gap_count": 0,
                "max_gap_sec": 0.0,
                "total_gaps_sec": 0.0,
                "connect_count": 0,
                "disconnect_count": 0,
                "error_count": 0,
                "health_score": 0.0,
                "is_stale": False,
                "subscribed_tokens": len(self._desired_subscriptions),
                "heartbeat_interval": 0.0,
            }
        return self._ws.stats

    def _execution_telemetry(self, session) -> dict[str, float | int]:
        active_portfolio_ids = set(self._portfolios_by_id)
        if active_portfolio_ids:
            fill_query = (
                session.query(LabFillRow)
                .join(LabOrderRow, LabFillRow.order_id == LabOrderRow.id)
                .filter(LabOrderRow.portfolio_id.in_(active_portfolio_ids))
            )
            fills_count = fill_query.count()
            exit_fill_count = fill_query.filter(LabOrderRow.action == "SELL").count()
            forced_count = (
                fill_query
                .filter(LabOrderRow.action == "SELL")
                .filter(LabFillRow.fill_type == "forced_taker_exit")
                .count()
            )
            maker_like_count = (
                fill_query
                .filter(LabFillRow.fill_type.notin_(["forced_taker_exit", "taker_entry"]))
                .count()
            )
        else:
            fills_count = 0
            exit_fill_count = 0
            forced_count = 0
            maker_like_count = 0
        working_orders = list(self._working_orders.values())
        quote_ages = [order.quote_age_sec for order in working_orders] if working_orders else [0.0]
        reprices = [float(order.reprices) for order in working_orders] if working_orders else [0.0]
        return {
            "forced_taker_exit_count": forced_count,
            "exit_fill_count": exit_fill_count,
            "forced_taker_exit_ratio": (forced_count / exit_fill_count) if exit_fill_count else 0.0,
            "maker_fill_ratio": (maker_like_count / fills_count) if fills_count else 0.0,
            "avg_quote_age_sec": sum(quote_ages) / len(quote_ages),
            "avg_reprice_count": sum(reprices) / len(reprices),
            "open_working_orders": len(working_orders),
        }

    def _should_freeze_entries(self, snapshot: dict[str, Any]) -> bool:
        if not self._settings.lab.ws_quality.enabled:
            return False
        message_count = int(snapshot.get("message_count") or 0)
        last_age = float(snapshot.get("last_message_age_sec") or -1.0)
        if "message_count" in snapshot and "last_message_age_sec" in snapshot and message_count == 0 and last_age < 0:
            # REST-seeded books are enough for shadow startup; freeze only after
            # WS proves stale/bad, not before the first market message arrives.
            return False
        if self._settings.lab.ws_quality.freeze_on_stale and bool(snapshot.get("is_stale")):
            return True
        if float(snapshot.get("health_score") or 0.0) < self._settings.lab.ws_quality.freeze_below_health_score:
            return True
        return False

    def _persist_ws_metrics_if_due(self, session, now: datetime, *, force: bool = False):
        if not force and self._last_ws_metrics_persist_at is not None:
            elapsed = (now - self._last_ws_metrics_persist_at).total_seconds()
            if elapsed < self._settings.lab.ws_quality.persist_interval_sec:
                return
        snapshot = self._ws_snapshot()
        telemetry = self._execution_telemetry(session)
        self._entries_frozen = self._should_freeze_entries(snapshot)
        session.add(LabWsMetricRow(
            timestamp=now,
            connected=bool(snapshot.get("connected")),
            reconnect_count=int(snapshot.get("connect_count") or 0),
            disconnect_count=int(snapshot.get("disconnect_count") or 0),
            error_count=int(snapshot.get("error_count") or 0),
            message_count=int(snapshot.get("message_count") or 0),
            messages_per_minute=float(snapshot.get("messages_per_minute") or 0.0),
            last_message_age_sec=float(snapshot.get("last_message_age_sec") or 0.0),
            gap_count=int(snapshot.get("gap_count") or 0),
            max_gap_sec=float(snapshot.get("max_gap_sec") or 0.0),
            total_gaps_sec=float(snapshot.get("total_gaps_sec") or 0.0),
            health_score=float(snapshot.get("health_score") or 0.0),
            is_stale=bool(snapshot.get("is_stale")),
            subscribed_tokens=int(snapshot.get("subscribed_tokens") or 0),
            heartbeat_interval=float(snapshot.get("heartbeat_interval") or 0.0),
            entries_frozen=self._entries_frozen,
            forced_taker_exit_count=int(telemetry["forced_taker_exit_count"]),
            exit_fill_count=int(telemetry["exit_fill_count"]),
            forced_taker_exit_ratio=float(telemetry["forced_taker_exit_ratio"]),
            maker_fill_ratio=float(telemetry["maker_fill_ratio"]),
            avg_quote_age_sec=float(telemetry["avg_quote_age_sec"]),
            avg_reprice_count=float(telemetry["avg_reprice_count"]),
            open_working_orders=int(telemetry["open_working_orders"]),
            extra_json={
                "ws": snapshot,
                "execution": telemetry,
            },
        ))
        self._last_ws_metrics_persist_at = now
        if int(snapshot.get("gap_count") or 0) >= self._settings.lab.ws_quality.gap_burst_threshold:
            asyncio.create_task(self._on_ws_quality_alert(
                "gap_burst",
                {
                    "gap_count": int(snapshot.get("gap_count") or 0),
                    "max_gap_sec": float(snapshot.get("max_gap_sec") or 0.0),
                    "health_score": float(snapshot.get("health_score") or 0.0),
                },
            ))

    async def _on_ws_quality_alert(self, alert_type: str, details: dict[str, Any]):
        now = utcnow()
        session = get_session(self._db_url)
        try:
            session.add(AuditRow(
                timestamp=now,
                event_type=f"ws_quality_{alert_type}",
                details=json.dumps(details),
            ))
            session.commit()
        finally:
            session.close()

        allow = {
            "disconnect": self._settings.lab.ws_quality.alert_disconnect,
            "stale": self._settings.lab.ws_quality.alert_stale,
            "gap_burst": self._settings.lab.ws_quality.alert_gap_burst,
        }.get(alert_type, False)
        if not allow:
            return
        last_sent = self._last_ws_quality_alert.get(alert_type)
        if last_sent and (now - last_sent).total_seconds() < 300:
            return
        self._last_ws_quality_alert[alert_type] = now
        await self._alerts.send_plain(
            f"WS {alert_type}: {json.dumps(details, ensure_ascii=False, sort_keys=True)}"
        )

    def _event_relevant(self, event_type: str) -> bool:
        return event_type in {"book", "best_bid_ask", "price_change", "last_trade_price"}

    def _remember_mid(self, market_id: str, timestamp: datetime, yes_mid: float):
        history = self._market_mid_history[market_id]
        history.append((timestamp, yes_mid))
        cutoff = timestamp - timedelta(minutes=20)
        while history and history[0][0] < cutoff:
            history.popleft()
        while len(history) > 1_500:
            history.popleft()

    def _remember_trade(self, market_id: str, timestamp: datetime, side: str, price: float):
        history = self._market_trade_history[market_id]
        history.append((timestamp, side.upper(), price))
        cutoff = timestamp - timedelta(minutes=10)
        while history and history[0][0] < cutoff:
            history.popleft()
        while len(history) > 500:
            history.popleft()

    def _record_market_microstate(self, market: Market, timestamp: datetime):
        yes_token, no_token = self._extract_tokens(market)
        if yes_token is None or no_token is None:
            return
        yes_ob = self._ob_manager.get_orderbook(yes_token.token_id)
        no_ob = self._ob_manager.get_orderbook(no_token.token_id)
        if yes_ob is None or no_ob is None:
            return
        self._remember_mid(market.id, timestamp, yes_ob.mid_price)

    def _persistence_sec(self, market_id: str, predicate) -> float:
        history = self._market_mid_history.get(market_id) or []
        if not history:
            return 0.0
        latest_ts, latest_mid = history[-1]
        if not predicate(latest_mid):
            return 0.0
        anchor_ts = latest_ts
        for ts, mid in reversed(history):
            if predicate(mid):
                anchor_ts = ts
            else:
                break
        return max(0.0, (latest_ts - anchor_ts).total_seconds())

    def _top_imbalance_ratio(self, orderbook: Orderbook | None) -> float:
        if orderbook is None:
            return 0.0
        bid_notional = sum(level.price * level.size for level in orderbook.bids[:1])
        ask_notional = sum(level.price * level.size for level in orderbook.asks[:1])
        if ask_notional <= 0 or bid_notional <= 0:
            return 0.0
        return bid_notional / ask_notional

    def _direction_agrees(self, market_id: str, *, yes_side: bool, current_mid: float) -> bool:
        trades = list(self._market_trade_history.get(market_id) or [])
        recent_trades = trades[-10:]
        buy_count = sum(1 for _, side, _ in recent_trades if side in {"BUY", "BID"})
        sell_count = sum(1 for _, side, _ in recent_trades if side in {"SELL", "ASK"})
        mids = list(self._market_mid_history.get(market_id) or [])
        recent_mids = [mid for ts, mid in mids if ts >= utcnow() - timedelta(minutes=3)]
        mid_delta = 0.0
        if recent_mids:
            mid_delta = current_mid - recent_mids[0]
        if yes_side:
            if recent_trades:
                return buy_count >= sell_count and mid_delta >= -0.002
            return mid_delta >= 0.0
        if recent_trades:
            return sell_count >= buy_count and mid_delta <= 0.002
        return mid_delta <= 0.0

    def _build_hypothesis_context(
        self,
        market: Market,
        yes_orderbook: Orderbook,
        no_orderbook: Orderbook,
        now: datetime,
    ) -> dict[str, Any]:
        late_cfg = self._settings.lab.late_stage
        yes_mid = yes_orderbook.mid_price
        mids = list(self._market_mid_history.get(market.id) or [])
        recent_prices = [mid for ts, mid in mids if ts >= now - timedelta(minutes=60)]
        price_return_60m = (recent_prices[-1] - recent_prices[0]) if len(recent_prices) >= 2 else 0.0
        price_range_60m = (max(recent_prices) - min(recent_prices)) if recent_prices else 0.0
        price_changes = np.diff(recent_prices) if len(recent_prices) >= 2 else np.array([])
        fee_rate = max(
            self._fee_rate_fraction_for(yes_orderbook.market_id),
            self._fee_rate_fraction_for(no_orderbook.market_id),
        )
        latency_penalty, latency_ms = self._latency_penalty(now)
        estimated_slippage = max(
            yes_orderbook.spread / 2.0,
            no_orderbook.spread / 2.0,
        ) + latency_penalty
        fee_plus_slippage = fee_rate + estimated_slippage
        crypto_info = classify_crypto15m_updown_market(market.question, category=market.category, tags=market.tags)
        crypto_cfg = self._settings.lab.crypto15m
        proxy_features = {
            "ret_1m": price_return_60m,
            "ret_3m": price_return_60m,
            "ret_5m": price_return_60m,
            "ret_15m": price_return_60m,
            "ret_60m": price_return_60m,
            "volatility_15m": float(np.std(price_changes[-15:])) if price_changes.size else 0.0,
            "volatility_regime_60m": 0.0,
            "volume_spike_15m": 1.0,
            "volume_spike_5m": 1.0,
            "candle_body_15m": price_return_60m,
            "upper_wick_15m": 0.0,
            "lower_wick_15m": 0.0,
            "distance_to_15m_open": price_return_60m,
            "distance_to_vwap_15m": 0.0,
            "return_zscore_15m": 0.0,
            "trend_consistency_15m": 0.5,
        }
        crypto_features = dict(proxy_features)
        ohlcv_meta = {
            "crypto_ohlcv_fresh": False,
            "crypto_ohlcv_stale": bool(crypto_info.is_crypto15m and crypto_cfg.live_ohlcv_enabled),
            "crypto_ohlcv_age_sec": None,
            "crypto_ohlcv_exchange": "",
        }
        if crypto_info.is_crypto15m and crypto_cfg.live_ohlcv_enabled:
            snapshot = self._crypto_ohlcv_feed.feature_snapshot(crypto_info.symbol, at=now, now=now)
            ohlcv_meta.update({
                "crypto_ohlcv_fresh": snapshot.fresh,
                "crypto_ohlcv_stale": not snapshot.fresh,
                "crypto_ohlcv_age_sec": snapshot.age_sec,
                "crypto_ohlcv_exchange": snapshot.exchange_id,
            })
            if snapshot.fresh:
                crypto_features.update(snapshot.features)
            else:
                crypto_features = {key: 0.0 for key in proxy_features}
        context = {
            "yes_mid": yes_mid,
            "no_mid": no_orderbook.mid_price,
            "no_best_ask": no_orderbook.best_ask,
            "time_to_resolution_hours": (
                (time_to_resolution_days(market.end_date, now) or 0.0) * 24.0
                if time_to_resolution_days(market.end_date, now) is not None
                else None
            ),
            "extreme_yes_min": late_cfg.extreme_yes_min,
            "extreme_yes_max": late_cfg.extreme_yes_max,
            "persistence_required_sec": late_cfg.persistence_sec,
            "imbalance_ratio_min": late_cfg.imbalance_ratio_min,
            "yes_extreme_persistence_sec": self._persistence_sec(
                market.id,
                lambda mid: mid >= late_cfg.extreme_yes_min,
            ),
            "no_extreme_persistence_sec": self._persistence_sec(
                market.id,
                lambda mid: mid <= late_cfg.extreme_yes_max,
            ),
            "yes_imbalance_ratio": self._top_imbalance_ratio(yes_orderbook),
            "no_imbalance_ratio": self._top_imbalance_ratio(no_orderbook),
            "yes_direction_agrees": self._direction_agrees(market.id, yes_side=True, current_mid=yes_mid),
            "no_direction_agrees": self._direction_agrees(market.id, yes_side=False, current_mid=yes_mid),
            "fee_plus_slippage": fee_plus_slippage,
            "fee_rate": fee_rate,
            "estimated_slippage": estimated_slippage,
            "latency_penalty": latency_penalty,
            "latency_ms": latency_ms,
            "poly_mid": yes_mid,
            "poly_spread": yes_orderbook.spread,
            "poly_depth_bid": yes_orderbook.depth("bid"),
            "poly_depth_ask": yes_orderbook.depth("ask"),
            "price_return_60m": price_return_60m,
            "price_range_60m": price_range_60m,
            "volatility_60m": float(np.std(price_changes)) if price_changes.size else 0.0,
            "samples_pre": float(len(recent_prices)),
            "extreme_yes_share": float(sum(1 for mid in recent_prices if mid >= late_cfg.extreme_yes_min) / len(recent_prices)) if recent_prices else 0.0,
            "extreme_no_share": float(sum(1 for mid in recent_prices if mid <= late_cfg.extreme_yes_max) / len(recent_prices)) if recent_prices else 0.0,
            "pre_event_window_minutes": 60.0,
            "liquidity": float(market.volume_24h or 0.0),
            "crypto15m_is_market": crypto_info.is_crypto15m,
            "crypto15m_symbol": crypto_info.symbol,
            "crypto15m_reason": crypto_info.reason,
            "crypto15m_timeframe_minutes": crypto_info.timeframe_minutes,
            "crypto15m_min_confidence": crypto_cfg.min_confidence,
            "crypto15m_min_net_ev": crypto_cfg.min_net_ev,
            "crypto15m_max_spread": crypto_cfg.max_spread,
            "crypto15m_candidate_window_minutes": crypto_cfg.candidate_window_minutes,
            "crypto15m_candidate_min_time_to_resolution_sec": crypto_cfg.candidate_min_time_to_resolution_sec,
            "crypto15m_candidate_target_time_to_resolution_sec": crypto_cfg.candidate_target_time_to_resolution_sec,
            "crypto15m_candidate_target_tolerance_sec": crypto_cfg.candidate_target_tolerance_sec,
            "crypto15m_min_entry_price": crypto_cfg.min_entry_price,
            "crypto15m_max_entry_price": crypto_cfg.max_entry_price,
            "crypto15m_min_abs_return_zscore_15m": crypto_cfg.min_abs_return_zscore_15m,
            "crypto15m_min_trend_consistency_15m": crypto_cfg.min_trend_consistency_15m,
            "crypto15m_momentum_threshold": crypto_cfg.momentum_threshold,
            "time_to_resolution_sec": (time_to_resolution_days(market.end_date, now) or 0.0) * 86400.0,
            "poly_imbalance": yes_orderbook.depth("bid") / yes_orderbook.depth("ask") if yes_orderbook.depth("ask") > 0 else 0.0,
            "poly_return_5m": price_return_60m,
        }
        context.update(crypto_features)
        context.update(ohlcv_meta)
        context["crypto_ret_1m"] = float(crypto_features.get("ret_1m", 0.0))
        context["crypto_ret_3m"] = float(crypto_features.get("ret_3m", 0.0))
        context["crypto_ret_5m"] = float(crypto_features.get("ret_5m", 0.0))
        context["crypto_ret_15m"] = float(crypto_features.get("ret_15m", 0.0))
        return context

    async def _refresh_loop(self):
        while self._running:
            try:
                await self._refresh_market_universe()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime_state.update_cycle_error(f"market_refresh: {exc}")
                await self._persist_runtime_status_async(
                    last_cycle_ts=utcnow(),
                    last_market_refresh_ts=utcnow(),
                    last_cycle_ok=False,
                    last_cycle_error=f"market_refresh: {exc}"[:2000],
                    cycle_failures_in_row=(runtime_state.snapshot().get("cycle_failures_in_row") or 0),
                    ws_connected=bool(self._ws and self._ws.is_connected),
                )
                logger.exception(f"Shadow lab market refresh failed: {exc}")
            await asyncio.sleep(self._settings.lab.scheduler.market_refresh_sec)

    async def _signal_loop(self):
        while self._running:
            try:
                await self._run_signal_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime_state.update_cycle_error(f"signal_tick: {exc}")
                await self._persist_runtime_status_async(
                    last_cycle_ts=utcnow(),
                    last_signal_tick_ts=utcnow(),
                    last_cycle_ok=False,
                    last_cycle_error=f"signal_tick: {exc}"[:2000],
                    cycle_failures_in_row=(runtime_state.snapshot().get("cycle_failures_in_row") or 0),
                    ws_connected=bool(self._ws and self._ws.is_connected),
                )
                logger.exception(f"Shadow lab signal tick failed: {exc}")
            await asyncio.sleep(self._settings.lab.scheduler.signal_eval_sec)

    async def _refresh_market_universe(self):
        now = utcnow()
        markets = await self._client.get_markets(
            active_only=True,
            max_pages=10,
            order_by="volume24hr",
            ascending=False,
        )
        standard_shortlist = [
            market
            for market in markets
            if self._market_is_eligible_for_universe(market, now)
        ]
        standard_shortlist.sort(key=lambda market: market.volume_24h, reverse=True)
        shortlist = standard_shortlist[: self._settings.lab.universe.max_markets]
        if self._settings.lab.crypto15m.enabled:
            seen_ids = {market.id for market in shortlist}
            crypto_markets = [
                market
                for market in markets
                if market.id not in seen_ids and self._market_is_crypto15m_eligible(market, now)
            ]
            crypto_markets.sort(key=lambda market: time_to_resolution_days(market.end_date, now) or 99.0)
            shortlist.extend(crypto_markets)
            seen_ids.update(market.id for market in crypto_markets)
            slug_markets = await self._fetch_crypto15m_slug_markets(now)
            for market in slug_markets:
                if market.id in seen_ids:
                    continue
                if not self._market_is_crypto15m_eligible(market, now):
                    continue
                shortlist.append(market)
                seen_ids.add(market.id)

        session = get_session(self._db_url)
        try:
            self._market_rows.clear()
            for market in shortlist:
                row = self._ensure_market_row(session, market)
                self._market_rows[market.id] = row.id
            session.commit()
        finally:
            session.close()

        self._market_cache = markets
        self._shortlist = shortlist
        self._rebuild_token_maps(shortlist)

        await self._seed_orderbooks(shortlist)
        await self._refresh_fee_rates(shortlist)
        await self._sync_subscriptions()

        runtime_state.update_cycle_ok(
            markets_count=len(shortlist),
            ws_connected=bool(self._ws and self._ws.is_connected),
        )
        session = get_session(self._db_url)
        try:
            self._persist_ws_metrics_if_due(session, now)
            self._update_runtime_status_row(
                session,
                last_market_refresh_ts=now,
                last_cycle_ts=now,
                last_cycle_ok=True,
                last_cycle_error=None,
                cycle_failures_in_row=0,
                ws_connected=bool(self._ws and self._ws.is_connected),
                markets_fetched_last=len(markets),
                eligible_markets_last=len(shortlist),
                subscribed_tokens_last=len(self._desired_subscriptions),
            )
            session.commit()
        finally:
            session.close()
        logger.info(
            f"Shadow universe refreshed: {len(shortlist)} eligible markets / {len(markets)} active"
        )

    async def _refresh_fee_rates(self, markets: list[Market]):
        getter = getattr(self._client, "get_fee_rate_bps", None)
        if getter is None:
            return
        token_ids = {
            token.token_id
            for market in markets
            for token in market.tokens
            if token.token_id and token.token_id not in self._fee_rate_bps_by_token
        }
        if not token_ids:
            return

        async def _fetch(token_id: str):
            try:
                bps = await getter(token_id)
            except Exception as exc:
                logger.debug(f"fee-rate fetch failed for {token_id[:12]}...: {exc}")
                return
            if bps is not None:
                self._fee_rate_bps_by_token[token_id] = float(bps)

        await asyncio.gather(*(_fetch(token_id) for token_id in token_ids))

    def _fee_rate_bps_for(self, token_id: str) -> float:
        fallback = max(0.0, float(self._settings.strategy.fee_rate)) * 10000.0
        return float(self._fee_rate_bps_by_token.get(token_id, fallback))

    def _fee_rate_fraction_for(self, token_id: str) -> float:
        return fee_rate_bps_to_fraction(self._fee_rate_bps_for(token_id))

    def _latency_penalty(self, event_time: datetime, now: datetime | None = None) -> tuple[float, float]:
        current = now or utcnow()
        event_time = _as_utc(event_time) or current
        age_ms = max(0.0, (current - event_time).total_seconds() * 1000.0)
        max_age = max(0.0, float(self._settings.lab.execution.max_event_age_ms))
        if age_ms <= max_age:
            return 0.0, age_ms
        penalty = max(0.0, float(self._settings.lab.execution.latency_penalty_bps)) / 10000.0
        return penalty, age_ms

    def _taker_fee_usdc_for(self, token_id: str, contracts: float, price: float) -> float:
        return polymarket_taker_fee_usdc(
            contracts,
            price,
            fee_rate_bps=self._fee_rate_bps_for(token_id),
            fees_enabled=True,
        )

    def _orderbook_event_age_ms(self, orderbook: Orderbook, now: datetime | None = None) -> float:
        event_time = self._coerce_ws_timestamp(orderbook.timestamp)
        return self._latency_penalty(event_time, now=now)[1]

    def _crypto15m_trade_assets(self) -> set[str]:
        return {
            str(asset).upper()
            for asset in (self._settings.lab.crypto15m.trade_assets or [])
            if str(asset).strip()
        }

    def _crypto15m_asset_allowed(self, asset: str) -> bool:
        return str(asset).upper() in self._crypto15m_trade_assets()

    def _crypto15m_reward_guard(
        self,
        session,
        runtime: PortfolioRuntime,
        *,
        signal: SignalOutput,
        stake: float,
        now: datetime,
    ) -> dict[str, Any]:
        cfg = self._settings.lab.crypto15m
        if runtime.config.track != "crypto15m" or not cfg.reward_guard_enabled:
            return {"accepted": True, "reason": "disabled", "reward_score": signal.edge}
        lookback_started_at = now - timedelta(hours=float(cfg.reward_lookback_hours))

        recent_orders = (
            session.query(LabOrderRow)
            .filter(LabOrderRow.portfolio_id == runtime.row_id)
            .filter(LabOrderRow.created_at >= lookback_started_at)
            .order_by(LabOrderRow.created_at.desc())
            .limit(int(cfg.reward_lookback_orders))
            .all()
        )
        order_size = sum(float(order.size_total or 0.0) for order in recent_orders)
        filled_size = sum(float(order.filled_size or 0.0) for order in recent_orders)
        fill_rate = (filled_size / order_size) if order_size > 0 else 1.0

        closed_positions = (
            session.query(LabPositionRow)
            .filter(LabPositionRow.portfolio_id == runtime.row_id)
            .filter(LabPositionRow.status == "closed")
            .filter(LabPositionRow.side == signal.side)
            .filter(LabPositionRow.closed_at >= lookback_started_at)
            .order_by(LabPositionRow.closed_at.desc())
            .limit(int(cfg.reward_lookback_trades))
            .all()
        )
        stop_streak = 0
        for position in closed_positions:
            if position.exit_reason == "stop_loss":
                stop_streak += 1
                continue
            break
        side_trade_count = len(closed_positions)
        side_avg_pnl = (
            sum(float(position.realized_pnl or position.pnl or 0.0) for position in closed_positions) / side_trade_count
            if side_trade_count
            else 0.0
        )

        latest_equity = (
            session.query(LabEquityPointRow)
            .filter(LabEquityPointRow.portfolio_id == runtime.row_id)
            .order_by(LabEquityPointRow.timestamp.desc())
            .first()
        )
        drawdown = float(latest_equity.drawdown_pct or 0.0) if latest_equity is not None else 0.0
        bankroll = max(float(runtime.bankroll or runtime.initial_bankroll or self._settings.bankroll.initial), 1.0)
        size_fraction = max(0.0, float(stake or 0.0) / bankroll)

        size_penalty = float(cfg.reward_size_penalty) * min(1.0, size_fraction * 10.0)
        fill_penalty = float(cfg.reward_fill_penalty) * max(0.0, 1.0 - fill_rate)
        drawdown_penalty = float(cfg.reward_drawdown_penalty) * max(0.0, drawdown)
        stop_penalty = float(cfg.reward_stop_streak_penalty) * float(stop_streak)
        reward_score = float(signal.edge or 0.0) - size_penalty - fill_penalty - drawdown_penalty - stop_penalty

        reason = "ok"
        accepted = reward_score >= float(cfg.reward_min_score)
        analyst_relaxed = False
        enforce_stop_streak = True
        enforce_fill_rate = len(recent_orders) >= max(5, int(cfg.reward_lookback_orders) // 3)
        strong_signal_override = (
            reward_score >= float(cfg.reward_strong_signal_min_score)
            and float(signal.edge or 0.0) >= float(cfg.reward_strong_signal_min_edge)
        )
        if enforce_stop_streak and stop_streak >= int(cfg.reward_stop_streak_limit) and not strong_signal_override:
            accepted = False
            reason = "stop_streak"
        elif (
            bool(cfg.side_regime_guard_enabled)
            and side_trade_count >= int(cfg.side_regime_min_trades)
            and side_avg_pnl < float(cfg.side_regime_min_avg_pnl)
        ):
            accepted = False
            reason = "side_regime_negative"
        elif enforce_fill_rate and fill_rate < float(cfg.reward_min_fill_rate):
            accepted = False
            reason = "fill_rate_low"
        elif not accepted:
            reason = "reward_guard_negative"
        elif strong_signal_override and stop_streak >= int(cfg.reward_stop_streak_limit):
            reason = "ok_strong_signal_override"

        return {
            "accepted": accepted,
            "reason": reason,
            "action_label": "BUY_LIMIT",
            "reward_score": reward_score,
            "reward_min_score": float(cfg.reward_min_score),
            "rolling_fill_rate": fill_rate,
            "rolling_orders": len(recent_orders),
            "lookback_started_at": lookback_started_at.isoformat(),
            "stop_streak": stop_streak,
            "side_trade_count": side_trade_count,
            "side_avg_pnl": side_avg_pnl,
            "drawdown_pct": drawdown,
            "size_fraction": size_fraction,
            "size_penalty": size_penalty,
            "fill_penalty": fill_penalty,
            "drawdown_penalty": drawdown_penalty,
            "stop_streak_penalty": stop_penalty,
            "strong_signal_override": strong_signal_override,
            "reward_strong_signal_min_score": float(cfg.reward_strong_signal_min_score),
            "reward_strong_signal_min_edge": float(cfg.reward_strong_signal_min_edge),
            "analyst_relaxed": analyst_relaxed,
            "enforce_stop_streak": enforce_stop_streak,
            "enforce_fill_rate": enforce_fill_rate,
            "evaluated_at": now.isoformat(),
        }

    async def _fetch_crypto15m_slug_markets(self, now: datetime) -> list[Market]:
        cfg = self._settings.lab.crypto15m
        if not cfg.enabled or not cfg.slug_discovery_enabled:
            return []
        current_start = int(now.timestamp() // 900 * 900)
        start_idx = -int(cfg.slug_discovery_behind_intervals)
        end_idx = int(cfg.slug_discovery_ahead_intervals)
        assets = [
            asset.lower()
            for asset in sorted(self._crypto15m_trade_assets())
            if asset in {"BTC", "ETH"}
        ]
        if not assets:
            return []
        slugs = [
            f"{asset}-updown-15m-{current_start + offset * 900}"
            for offset in range(start_idx, end_idx + 1)
            for asset in assets
        ]
        semaphore = asyncio.Semaphore(max(1, int(cfg.slug_discovery_concurrency)))

        async def _fetch(slug: str) -> Market | None:
            async with semaphore:
                return await self._client.get_market_by_slug(slug)

        results = await asyncio.gather(*(_fetch(slug) for slug in slugs), return_exceptions=True)
        markets: list[Market] = []
        seen: set[str] = set()
        for result in results:
            if isinstance(result, Exception) or result is None:
                continue
            if result.id in seen:
                continue
            seen.add(result.id)
            markets.append(result)
        if markets:
            logger.info(f"Crypto15m slug discovery added {len(markets)} direct markets")
        return markets

    def _market_is_eligible_for_universe(self, market: Market, now: datetime | None = None) -> bool:
        if not market.active:
            return False
        yes_token, no_token = self._extract_tokens(market)
        if yes_token is None or no_token is None:
            return False
        horizon_days = time_to_resolution_days(market.end_date, now)
        if horizon_days is None:
            return False
        if horizon_days < 0:
            return False
        return horizon_days <= self._settings.lab.universe.max_horizon_days

    def _market_is_crypto15m_eligible(self, market: Market, now: datetime | None = None) -> bool:
        if not self._settings.lab.crypto15m.enabled or not market.active:
            return False
        yes_token, no_token = self._extract_tokens(market)
        if yes_token is None or no_token is None:
            return False
        info = classify_crypto15m_updown_market(market.question, category=market.category, tags=market.tags)
        if not info.is_crypto15m:
            return False
        if not self._crypto15m_asset_allowed(info.asset):
            return False
        horizon_days = time_to_resolution_days(market.end_date, now)
        if horizon_days is None or horizon_days < 0:
            return False
        return (horizon_days * 24.0) <= self._settings.lab.crypto15m.max_horizon_hours

    @staticmethod
    def _market_passes_portfolio_filters(
        portfolio: LabPortfolioConfig,
        market: Market,
        orderbook: Orderbook,
        now: datetime | None = None,
        *,
        check_orderbook: bool = True,
    ) -> bool:
        horizon_days = time_to_resolution_days(market.end_date, now)
        if horizon_days is None or horizon_days < 0:
            return False
        if horizon_days > portfolio.max_horizon_days:
            return False
        if portfolio.time_to_resolution_max_hours is not None and (horizon_days * 24.0) > portfolio.time_to_resolution_max_hours:
            return False
        if market.volume_24h < portfolio.min_daily_volume:
            return False
        if not check_orderbook:
            return True
        if orderbook.spread > portfolio.max_spread:
            return False
        bid_depth = sum(level.price * level.size for level in orderbook.bids[:5])
        ask_depth = sum(level.price * level.size for level in orderbook.asks[:5])
        if bid_depth < portfolio.min_depth_usd:
            return False
        if ask_depth < portfolio.min_depth_usd:
            return False
        return True

    def _rebuild_token_maps(self, markets: list[Market]):
        self._token_to_market.clear()
        for market in markets:
            yes_token, no_token = self._extract_tokens(market)
            if yes_token:
                self._token_to_market[yes_token.token_id] = market
            if no_token:
                self._token_to_market[no_token.token_id] = market

    async def _seed_orderbooks(self, markets: list[Market]):
        tokens_to_fetch: list[tuple[Market, str]] = []
        for market in markets:
            yes_token, no_token = self._extract_tokens(market)
            for token in (yes_token, no_token):
                if token is None:
                    continue
                current = self._ob_manager.get_orderbook(token.token_id)
                if current is None or (utcnow() - datetime.fromtimestamp(current.timestamp, timezone.utc)).total_seconds() > 30:
                    tokens_to_fetch.append((market, token.token_id))

        for token_id in self._open_position_tokens():
            current = self._ob_manager.get_orderbook(token_id)
            if current is None:
                market = self._token_to_market.get(token_id)
                if market is not None:
                    tokens_to_fetch.append((market, token_id))

        if not tokens_to_fetch:
            return

        semaphore = asyncio.Semaphore(24)
        results: list[tuple[Market, str, Orderbook | None]] = []

        async def _fetch(market: Market, token_id: str):
            async with semaphore:
                try:
                    orderbook = await self._client.get_orderbook(token_id)
                    self._ob_manager.apply_snapshot(token_id, orderbook)
                    results.append((market, token_id, orderbook))
                except Exception as exc:
                    logger.debug(f"orderbook seed failed for {token_id[:12]}...: {exc}")
                    results.append((market, token_id, None))

        await asyncio.gather(*[_fetch(market, token_id) for market, token_id in tokens_to_fetch])

        session = get_session(self._db_url)
        try:
            by_market: dict[str, dict[str, Orderbook]] = defaultdict(dict)
            for market, token_id, orderbook in results:
                if orderbook is None:
                    continue
                by_market[market.id][token_id] = orderbook

            for market in markets:
                row_id = self._market_rows.get(market.id)
                if row_id is None:
                    continue
                yes_token, no_token = self._extract_tokens(market)
                yes_ob = by_market.get(market.id, {}).get(yes_token.token_id) if yes_token else None
                no_ob = by_market.get(market.id, {}).get(no_token.token_id) if no_token else None
                if yes_ob is None:
                    continue
                self._store_market_snapshot(session, row_id, market, yes_ob, no_ob)
                if no_ob is not None:
                    self._record_market_microstate(market, utcnow())
            session.commit()
        finally:
            session.close()

    async def _sync_subscriptions(self):
        desired: set[str] = set()
        for market in self._shortlist:
            yes_token, no_token = self._extract_tokens(market)
            if yes_token:
                desired.add(yes_token.token_id)
            if no_token:
                desired.add(no_token.token_id)
        desired.update(self._open_position_tokens())

        if self._ws is None:
            self._desired_subscriptions = desired
            return

        add_tokens = desired - self._desired_subscriptions
        remove_tokens = self._desired_subscriptions - desired

        if add_tokens:
            await self._ws.subscribe(sorted(add_tokens))
        if remove_tokens:
            await self._ws.unsubscribe(sorted(remove_tokens))

        self._desired_subscriptions = desired
        runtime_state.set_ws_connected(bool(self._ws.is_connected))

    async def _run_signal_tick(self):
        now = utcnow()
        async with self._state_lock:
            session = get_session(self._db_url)
            try:
                self._update_position_marks(session)
                self._manage_working_orders(session, now)
                self._submit_exit_orders(session, now)
                self._persist_ws_metrics_if_due(session, now)
                if not self._entries_frozen:
                    self._submit_entry_orders(session, now)
                else:
                    logger.debug("Skipping new entries: WS guardrail is active")

                if self._next_equity_sample_at is None or now >= self._next_equity_sample_at:
                    self._sample_equity(session, now)
                    self._next_equity_sample_at = now + timedelta(
                        seconds=self._settings.lab.scheduler.equity_sample_sec
                    )

                self._update_runtime_status_row(
                    session,
                    last_cycle_ts=now,
                    last_signal_tick_ts=now,
                    last_cycle_ok=True,
                    last_cycle_error=None,
                    cycle_failures_in_row=0,
                    ws_connected=bool(self._ws and self._ws.is_connected),
                    eligible_markets_last=len(self._shortlist),
                    subscribed_tokens_last=len(self._desired_subscriptions),
                )
                session.commit()
                runtime_state.update_cycle_ok(
                    markets_count=len(self._shortlist),
                    ws_connected=bool(self._ws and self._ws.is_connected),
                )
            finally:
                session.close()

    def _update_position_marks(self, session):
        active_portfolio_ids = set(self._portfolios_by_id)
        if not active_portfolio_ids:
            return
        open_positions = (
            session.query(LabPositionRow)
            .filter(LabPositionRow.portfolio_id.in_(active_portfolio_ids))
            .filter(LabPositionRow.status == "open")
            .all()
        )
        for position in open_positions:
            orderbook = self._ob_manager.get_orderbook(position.token_id)
            if orderbook is None:
                continue
            if orderbook.best_bid > 0:
                position.current_price = orderbook.best_bid

    @staticmethod
    def _position_price_delta(side: str, entry_price: float, mark_price: float) -> float:
        # Runtime buys native YES/NO tokens, so PnL is always token exit minus token entry.
        # Historical complement-style NO math belongs in EV scoring, not in filled-position accounting.
        return mark_price - entry_price

    def _manage_working_orders(self, session, now: datetime):
        for order_id in list(self._working_orders):
            order = self._working_orders.get(order_id)
            if order is None:
                continue

            orderbook = self._ob_manager.get_orderbook(order.token_id)
            if orderbook is None:
                continue

            self._engine.observe_book(order, orderbook)
            decision = self._engine.reprice_decision(
                order,
                now=now,
                is_exit=order.action == "SELL",
            )
            if decision.should_reprice:
                tick = self._tick_size_for(order.token_id)
                new_price = self._engine.quote_entry_price(orderbook, order.action, tick)
                new_queue = self._engine.visible_same_side_size(orderbook, order.action, new_price)
                self._engine.apply_reprice(
                    order,
                    new_price=new_price,
                    new_queue_ahead=new_queue,
                    now=now,
                )
                self._sync_order_row(session, order, closed=False)
                continue

            if not decision.expired:
                continue

            if (
                decision.forced_taker_exit
                and order.force_taker_allowed
                and order.reprices >= self._settings.lab.execution.force_exit_after_failed_reprices
            ):
                fill = self._engine.force_taker_fill(
                    order,
                    orderbook,
                    event_age_ms=self._orderbook_event_age_ms(orderbook, now),
                )
                if fill is not None:
                    fill.fee_usdc = self._taker_fee_usdc_for(order.token_id, fill.filled_size, fill.price)
                    fill.fee_rate_bps = self._fee_rate_bps_for(order.token_id)
                    order.closed_at = now
                    self._apply_fill(session, order, fill, now)
                    self._sync_order_row(session, order, closed=order.status == "filled")
                    if order.status == "filled":
                        self._drop_working_order(order.order_id)
                    continue

            self._engine.cancel(order, reason="expired", now=now)
            self._sync_order_row(session, order, closed=True)
            self._drop_working_order(order.order_id)

    def _portfolio_exit_limits(self, runtime: PortfolioRuntime) -> tuple[float, float, float]:
        stop_loss = runtime.config.stop_loss_pct
        take_profit = runtime.config.take_profit_pct
        max_hold = runtime.config.max_hold_hours
        if stop_loss is None:
            stop_loss = self._settings.lab.late_stage.stop_loss_pct if runtime.config.track == "late_stage" else self._settings.strategy.exit.stop_loss_pct
        if take_profit is None:
            take_profit = self._settings.lab.late_stage.take_profit_pct if runtime.config.track == "late_stage" else self._settings.strategy.exit.take_profit_pct
        if max_hold is None:
            max_hold = (self._settings.lab.late_stage.max_hold_minutes / 60.0) if runtime.config.track == "late_stage" else self._settings.lab.exit_time_hours
        return float(stop_loss), float(take_profit), float(max_hold)

    def _decision_key(
        self,
        runtime: PortfolioRuntime | None,
        market_row_id: int,
        decision: str,
        *,
        side: str | None,
        hypothesis: str | None,
    ) -> tuple[Any, ...]:
        return (
            runtime.row_id if runtime is not None else None,
            market_row_id,
            decision,
            side or "",
            hypothesis or "",
        )

    def _audit_decision(
        self,
        session,
        *,
        runtime: PortfolioRuntime | None,
        market_row_id: int,
        market: Market,
        decision: str,
        side: str | None = None,
        hypothesis: str | None = None,
        edge: float = 0.0,
        quality: MarketQualityAssessment | None = None,
        token_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ):
        quality = quality or MarketQualityAssessment(score=100.0)
        reasons = list(dict.fromkeys(quality.reasons))
        meta_reason = str((meta or {}).get("reason") or "").strip()
        if decision == "rejected" and meta_reason:
            reasons = list(dict.fromkeys([*reasons, meta_reason]))
        payload = {
            "decision": decision,
            "side": side,
            "hypothesis": hypothesis,
            "edge": round(edge, 6),
            "score": round(quality.score, 2),
            "reasons": reasons,
            "track": runtime.config.track if runtime is not None else "",
            "meta": meta or {},
        }
        signature = json.dumps(payload, sort_keys=True)
        decision_key = self._decision_key(
            runtime,
            market_row_id,
            decision,
            side=side,
            hypothesis=hypothesis,
        )
        if self._decision_fingerprints.get(decision_key) == signature:
            return
        self._decision_fingerprints[decision_key] = signature
        session.add(LabDecisionAuditRow(
            portfolio_id=runtime.row_id if runtime is not None else None,
            market_id=market_row_id,
            token_id=token_id,
            timestamp=utcnow(),
            decision=decision,
            track=runtime.config.track if runtime is not None else "control",
            portfolio_key=runtime.key if runtime is not None else None,
            hypothesis=hypothesis or "",
            side=side,
            edge=edge,
            quality_score=quality.score,
            expected_net_edge=quality.expected_net_edge,
            fee_rate=quality.estimated_fee_rate,
            estimated_slippage=quality.estimated_slippage,
            spread=quality.spread,
            bid_depth=quality.bid_depth,
            ask_depth=quality.ask_depth,
            time_to_resolution_hours=quality.time_to_resolution_hours,
            question_snapshot=market.question,
            category=market.category or "",
            reasons_json=reasons,
            meta_json=meta or {},
        ))

    def _apply_instant_fill(
        self,
        session,
        runtime: PortfolioRuntime,
        order: ShadowOrderState,
        *,
        price: float,
        timestamp: datetime,
        fill_type: str,
        preview: TakerFillPreview | None = None,
        latency_ms: float = 0.0,
    ):
        fill_size = order.size_remaining
        if fill_size <= 0:
            return
        order.price = price
        order.size_remaining = 0.0
        order.filled_size += fill_size
        order.filled_notional += fill_size * price
        order.status = "filled"
        order.closed_at = timestamp
        self._persist_new_order(session, runtime, order)
        self._apply_fill(
            session,
            order,
            ShadowFill(
                filled_size=fill_size,
                price=price,
                notional=fill_size * price,
                fill_type=fill_type,
                fee_usdc=self._taker_fee_usdc_for(order.token_id, fill_size, price),
                slippage_usdc=preview.slippage_usdc if preview is not None else 0.0,
                effective_fill_price=preview.avg_price if preview is not None else price,
                fee_rate_bps=self._fee_rate_bps_for(order.token_id),
                latency_ms=max(0.0, float(latency_ms)),
            ),
            timestamp,
        )
        self._sync_order_row(session, order, closed=True)
        self._drop_working_order(order.order_id)

    def _force_close_without_orderbook(
        self,
        session,
        runtime: PortfolioRuntime,
        position: LabPositionRow,
        *,
        market: Market | None,
        market_row: MarketRow | None,
        now: datetime,
        exit_reason: str,
    ) -> None:
        mark = position.current_price if position.current_price is not None else position.entry_price
        mark = max(0.0, min(1.0, float(mark or 0.0)))
        if mark <= 0.0 and position.entry_price:
            mark = max(0.0, min(1.0, float(position.entry_price)))

        market_condition_id = (
            market.id
            if market is not None
            else (market_row.polymarket_id if market_row is not None else str(position.market_id))
        )
        order = self._engine.create_order(
            portfolio_key=runtime.key,
            market_id=market_condition_id,
            market_db_id=position.market_id,
            token_id=position.token_id,
            event_id=position.event_id,
            side=position.side,
            action="SELL",
            price=mark,
            size=position.size,
            queue_ahead=0.0,
            hypothesis=position.hypothesis,
            edge=self._position_price_delta(position.side, position.entry_price, mark),
            now=now,
            force_taker_allowed=True,
        )
        order.reason = exit_reason
        order.order_kind = "forced_taker"
        order.forced_exit = True
        self._apply_instant_fill(
            session,
            runtime,
            order,
            price=mark,
            timestamp=now,
            fill_type="forced_taker_exit",
            latency_ms=0.0,
        )
        session.add(AuditRow(
            timestamp=now,
            event_type="shadow_forced_close_no_orderbook",
            details=json.dumps({
                "portfolio": runtime.key,
                "position_id": position.id,
                "market_id": position.market_id,
                "token_id": position.token_id,
                "side": position.side,
                "price": mark,
                "size": position.size,
                "exit_reason": exit_reason,
                "price_source": "position.current_price" if position.current_price is not None else "position.entry_price",
            }),
        ))

    def _submit_exit_orders(self, session, now: datetime):
        active_portfolio_ids = set(self._portfolios_by_id)
        if not active_portfolio_ids:
            return
        open_positions = (
            session.query(LabPositionRow)
            .filter(LabPositionRow.portfolio_id.in_(active_portfolio_ids))
            .filter(LabPositionRow.status == "open")
            .all()
        )
        for position in open_positions:
            runtime = self._portfolios_by_id.get(position.portfolio_id)
            if runtime is None:
                continue
            if self._has_working_order(
                portfolio_id=position.portfolio_id,
                market_id=position.market_id,
                token_id=position.token_id,
                action="SELL",
            ):
                continue

            stop_loss_pct, take_profit_pct, max_hold_hours = self._portfolio_exit_limits(runtime)
            opened_at = _as_utc(position.opened_at) or now
            hold_hours = max(0.0, (now - opened_at).total_seconds() / 3600.0)
            market = self._market_by_db_id(position.market_id)
            market_row = session.get(MarketRow, position.market_id) if market is None else None
            market_end_date = market.end_date if market is not None else (market_row.end_date if market_row is not None else None)
            if isinstance(market_end_date, datetime):
                end_dt = _as_utc(market_end_date)
                horizon_days = (end_dt - now).total_seconds() / 86400.0 if end_dt is not None else None
            else:
                horizon_days = time_to_resolution_days(market_end_date, now) if market_end_date is not None else None
            orderbook = self._ob_manager.get_orderbook(position.token_id)
            if orderbook is None:
                if runtime.config.track == "crypto15m":
                    no_book_exit_reason = None
                    if horizon_days is not None and horizon_days <= 0:
                        no_book_exit_reason = "market_resolved"
                    elif hold_hours >= max_hold_hours:
                        no_book_exit_reason = "time_exit_no_orderbook"
                    if no_book_exit_reason is not None:
                        self._force_close_without_orderbook(
                            session,
                            runtime,
                            position,
                            market=market,
                            market_row=market_row,
                            now=now,
                            exit_reason=no_book_exit_reason,
                        )
                continue

            mark = position.current_price if position.current_price is not None else position.entry_price
            if runtime.config.track == "crypto15m" and orderbook.best_bid > 0:
                mark = orderbook.best_bid
            pct_change = (mark - position.entry_price) / position.entry_price if position.entry_price > 0 else 0.0
            stop_pct_change = pct_change
            take_profit_pct_change = pct_change
            if runtime.config.track == "crypto15m" and position.entry_price > 0:
                size = max(float(position.size or 0.0), 1e-9)
                fee_per_share = self._taker_fee_usdc_for(position.token_id, size, mark) / size
                spread_buffer = max(float(orderbook.spread or 0.0), 0.0) * 0.25
                net_exit_mark = max(0.0, mark - fee_per_share - spread_buffer)
                stop_pct_change = (net_exit_mark - position.entry_price) / position.entry_price
                # Take-profit exits are maker orders in shadow mode; makers are fee-free, so
                # using taker-fee-adjusted marks here makes the bot miss valid small wins.
                take_profit_pct_change = pct_change
                stop_noise_margin = max(0.0025, min(0.01, max(float(orderbook.spread or 0.0), 0.0) * 0.25))
            else:
                net_exit_mark = mark
                fee_per_share = 0.0
                spread_buffer = 0.0
                stop_noise_margin = 0.0

            exit_reason = None
            if horizon_days is not None and horizon_days <= 0:
                exit_reason = "market_resolved"
            elif stop_pct_change <= -(stop_loss_pct + stop_noise_margin):
                exit_reason = "stop_loss"
            elif take_profit_pct_change >= take_profit_pct:
                exit_reason = "take_profit"
            elif hold_hours >= max_hold_hours:
                exit_reason = "time_exit"

            if exit_reason is None:
                continue
            if runtime.config.track == "crypto15m" and exit_reason == "stop_loss":
                stop_min_hold_sec = int(self._settings.lab.crypto15m.stop_min_hold_sec)
                if stop_min_hold_sec > 0 and hold_hours * 3600.0 < stop_min_hold_sec:
                    continue

            market_condition_id = market.id if market is not None else str(position.market_id)
            quote_price = self._engine.quote_entry_price(orderbook, "SELL", self._tick_size_for(position.token_id))
            queue = self._engine.visible_same_side_size(orderbook, "SELL", quote_price)
            order = self._engine.create_order(
                portfolio_key=runtime.key,
                market_id=market_condition_id,
                market_db_id=position.market_id,
                token_id=position.token_id,
                event_id=position.event_id,
                side=position.side,
                action="SELL",
                price=quote_price,
                size=position.size,
                queue_ahead=queue,
                hypothesis=position.hypothesis,
                edge=0.0,
                now=now,
                force_taker_allowed=exit_reason in {"stop_loss", "market_resolved"} or (
                    runtime.config.track == "crypto15m" and exit_reason == "time_exit"
                ),
            )
            order.reason = exit_reason
            order.edge = take_profit_pct_change if exit_reason == "take_profit" else stop_pct_change
            if (
                (runtime.config.track == "late_stage" and exit_reason == "market_resolved")
                or (runtime.config.track == "crypto15m" and exit_reason in {"market_resolved", "time_exit"})
            ):
                taker_price = orderbook.best_bid if orderbook.best_bid > 0 else quote_price
                order.order_kind = "forced_taker"
                order.forced_exit = True
                self._apply_instant_fill(
                    session,
                    runtime,
                    order,
                    price=taker_price,
                    timestamp=now,
                    fill_type="forced_taker_exit",
                    latency_ms=self._orderbook_event_age_ms(orderbook, now),
                )
            else:
                self._persist_new_order(session, runtime, order)

    def _should_use_taker_entry(
        self,
        runtime: PortfolioRuntime,
        market: Market,
        orderbook: Orderbook,
        signal: SignalOutput,
        quality: MarketQualityAssessment,
        now: datetime,
    ) -> bool:
        if runtime.config.track == "crypto15m":
            if not self._settings.lab.crypto15m.allow_taker_entry:
                return False
            horizon_days = time_to_resolution_days(market.end_date, now)
            if horizon_days is None:
                return False
            minutes_left = horizon_days * 24.0 * 60.0
            taker_minutes = runtime.config.allow_taker_entry_minutes or self._settings.lab.crypto15m.taker_entry_minutes
            if "expected_net_ev" in signal.metadata:
                edge_after_costs = signal.edge
            else:
                edge_after_costs = signal.edge - max(orderbook.spread, 0.0) - self._fee_rate_fraction_for(orderbook.market_id)
            if minutes_left <= taker_minutes and edge_after_costs >= self._settings.lab.crypto15m.min_net_ev:
                return True
            queue_risk = quality.bid_depth > (runtime.config.min_depth_usd * 2.0)
            return queue_risk and edge_after_costs >= self._settings.lab.crypto15m.min_net_ev
        if runtime.config.track != "late_stage":
            return False
        horizon_days = time_to_resolution_days(market.end_date, now)
        if horizon_days is None:
            return False
        minutes_left = horizon_days * 24.0 * 60.0
        taker_minutes = runtime.config.allow_taker_entry_minutes or self._settings.lab.late_stage.taker_entry_minutes
        if minutes_left <= taker_minutes:
            return True
        queue_risk = quality.bid_depth > (runtime.config.min_depth_usd * 2.0)
        decaying_signal = signal.edge >= (quality.estimated_fee_rate + quality.estimated_slippage + 0.02)
        return queue_risk and decaying_signal and orderbook.spread <= max(runtime.config.max_spread, 0.02)

    def _score_with_learned_gate(
        self,
        runtime: PortfolioRuntime,
        market: Market,
        ob_yes: Orderbook,
        ob_no: Orderbook,
        *,
        side: str,
        market_probability: float,
        external_data: dict[str, Any] | None,
    ) -> LearnedGateDecision:
        if runtime.config.track == "crypto15m":
            return LearnedGateDecision(
                enabled=False,
                accepted_artifact=False,
                predicted_yes_probability=market_probability,
                candidate_confidence=0.0,
                should_veto=False,
                reason="crypto15m_uses_h7_model",
            )
        if not runtime.config.use_learned_gate:
            return LearnedGateDecision(
                enabled=False,
                accepted_artifact=False,
                predicted_yes_probability=market_probability,
                candidate_confidence=0.0,
                should_veto=False,
                reason="ab_control_bypass",
            )
        return self._learned_gate.score_candidate(
            market,
            ob_yes,
            ob_no,
            side=side,
            market_probability=market_probability,
            external_data=external_data,
        )

    def _ensure_crypto15m_model_loaded(self):
        model_cfg = self._settings.strategy.crypto15m_model
        if self._crypto15m_loaded_at is not None:
            age = (utcnow() - self._crypto15m_loaded_at).total_seconds()
            if age < model_cfg.reload_interval_sec:
                return
        self._crypto15m_manifest = None
        self._crypto15m_bundle = None
        path = Path(model_cfg.artifact_path)
        if not path.exists():
            self._crypto15m_loaded_at = utcnow()
            return
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            if model_cfg.require_accepted_artifact and not bool(manifest.get("accepted")):
                self._crypto15m_manifest = manifest
                self._crypto15m_loaded_at = utcnow()
                return
            model_path_raw = str(manifest.get("model_path") or "").strip()
            if not model_path_raw:
                self._crypto15m_manifest = manifest
                self._crypto15m_loaded_at = utcnow()
                return
            model_path = self._resolve_artifact_path(model_path_raw)
            if model_path.is_dir():
                self._crypto15m_manifest = manifest
                self._crypto15m_loaded_at = utcnow()
                return
            if model_path.exists():
                with open(model_path, "rb") as fh:
                    self._crypto15m_bundle = pickle.load(fh)
                if isinstance(self._crypto15m_bundle, dict) and "model" in self._crypto15m_bundle:
                    self._crypto15m_bundle["model"] = _prepare_runtime_model(self._crypto15m_bundle["model"])
                self._crypto15m_manifest = manifest
        except Exception as exc:
            logger.warning(f"Crypto15m model load failed: {exc}")
        self._crypto15m_loaded_at = utcnow()

    @staticmethod
    def _resolve_artifact_path(raw_path: str) -> Path:
        path = Path(raw_path)
        if path.exists() or "\\" not in raw_path:
            return path
        normalized = Path(raw_path.replace("\\", "/"))
        return normalized if normalized.exists() else path

    def _with_crypto15m_model_context(self, external_data: dict[str, Any]) -> dict[str, Any]:
        if not self._settings.strategy.crypto15m_model.enabled:
            return external_data
        if external_data.get("crypto_ohlcv_stale"):
            enriched = dict(external_data)
            enriched["crypto15m_model_block_reason"] = "crypto_ohlcv_stale"
            return enriched
        self._ensure_crypto15m_model_loaded()
        if not self._crypto15m_bundle:
            return external_data
        columns = self._crypto15m_bundle.get("feature_columns") or CRYPTO15M_FEATURE_COLUMNS
        features = pd.DataFrame(
            [[float(external_data.get(column, 0.0)) for column in columns]],
            columns=columns,
        )
        model = self._crypto15m_bundle["model"]
        predicted = str(_predict_safely(model, features)[0]).upper()
        confidence = 0.0
        probability_by_class: dict[str, float] = {}
        if hasattr(model, "predict_proba"):
            probabilities = _predict_proba_safely(model, features)[0]
            confidence = float(max(probabilities))
            classes = [str(value).upper() for value in getattr(model, "classes_", [])]
            probability_by_class = {
                klass: float(probabilities[index])
                for index, klass in enumerate(classes)
                if index < len(probabilities)
            }
        yes_class_probability = probability_by_class.get("YES", 0.0)
        no_class_probability = probability_by_class.get("NO", 0.0)
        no_trade_probability = probability_by_class.get("NO_TRADE", 0.0)
        directional_total = yes_class_probability + no_class_probability
        if directional_total > 0:
            yes_probability = yes_class_probability / directional_total
        elif predicted == "YES":
            yes_probability = confidence
        elif predicted == "NO":
            yes_probability = 1.0 - confidence
        else:
            yes_probability = 0.5
        yes_probability = max(0.01, min(0.99, float(yes_probability)))
        enriched = dict(external_data)
        enriched.update({
            "crypto15m_model_label": predicted,
            "crypto15m_model_confidence": confidence,
            "crypto15m_model_yes_probability": yes_probability,
            "crypto15m_model_yes_class_probability": yes_class_probability,
            "crypto15m_model_no_class_probability": no_class_probability,
            "crypto15m_model_no_trade_probability": no_trade_probability,
        })
        if predicted in {"YES", "NO"}:
            enriched.update({
                "crypto15m_model_side": predicted,
            })
        return enriched

    def _prepare_signal_external_data(
        self,
        runtime: PortfolioRuntime,
        market: Market,
        orderbook: Orderbook,
        no_orderbook: Orderbook,
        now: datetime,
        *,
        base_external_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        external_data = dict(base_external_data) if base_external_data is not None else self._build_hypothesis_context(
            market,
            orderbook,
            no_orderbook,
            now,
        )
        if runtime.config.track == "crypto15m":
            external_data.update({
                "crypto15m_min_confidence": float(runtime.settings.lab.crypto15m.min_confidence),
                "crypto15m_min_net_ev": runtime.settings.lab.crypto15m.min_net_ev,
                "crypto15m_max_spread": runtime.settings.lab.crypto15m.max_spread,
                "crypto15m_momentum_threshold": runtime.settings.lab.crypto15m.momentum_threshold,
                "crypto15m_allow_no_trade_fallback": runtime.settings.lab.crypto15m.allow_no_trade_fallback,
                "crypto15m_no_trade_fallback_max_probability": runtime.settings.lab.crypto15m.no_trade_fallback_max_probability,
                "crypto15m_use_learned_gate": runtime.config.use_learned_gate,
                "crypto15m_relax_momentum_gate": False,
                "crypto15m_relax_regime_gates": False,
            })
            if runtime.config.use_learned_gate:
                external_data = self._with_crypto15m_model_context(external_data)
        return external_data

    def _score_with_ai_analyst(
        self,
        runtime: PortfolioRuntime,
        market: Market,
        signal: SignalOutput,
        trade_orderbook: Orderbook,
        no_orderbook: Orderbook,
        *,
        external_data: dict[str, Any],
        quality: MarketQualityAssessment,
        now: datetime,
    ) -> AnalystReview:
        if runtime.config.track != "crypto15m" or not runtime.config.use_ai_analyst:
            return AnalystReview(False, False, True, reason="disabled")
        return self._ai_analyst.review_candidate(
            portfolio_key=runtime.key,
            market=market,
            signal=signal,
            trade_orderbook=trade_orderbook,
            no_orderbook=no_orderbook,
            external_data=external_data,
            quality_score=float(quality.score),
            now=now,
        )

    def _submit_entry_orders(
        self,
        session,
        now: datetime,
        *,
        market_ids: set[str] | None = None,
        late_stage_only: bool = False,
    ):
        open_positions: dict[int, list[PositionInfo]] = {}
        open_markets_by_portfolio: dict[int, set[int]] = defaultdict(set)
        active_portfolio_ids = set(self._portfolios_by_id)
        session_open_positions = (
            session.query(LabPositionRow)
            .filter(LabPositionRow.portfolio_id.in_(active_portfolio_ids))
            .filter(LabPositionRow.status == "open")
            .all()
        )
        for position in session_open_positions:
            open_positions.setdefault(position.portfolio_id, []).append(PositionInfo(
                market_id=position.market_id,
                event_id=position.event_id,
                side=position.side,
                size=position.size,
                entry_price=position.entry_price,
            ))
            open_markets_by_portfolio[position.portfolio_id].add(position.market_id)
        latest_opened_at_by_portfolio: dict[int, datetime] = {}
        for portfolio_id, opened_at in (
            session.query(LabPositionRow.portfolio_id, func.max(LabPositionRow.opened_at))
            .filter(LabPositionRow.portfolio_id.in_(active_portfolio_ids))
            .group_by(LabPositionRow.portfolio_id)
            .all()
        ):
            normalized_opened_at = _as_utc(opened_at)
            if normalized_opened_at is not None:
                latest_opened_at_by_portfolio[int(portfolio_id)] = normalized_opened_at

        for market in self._shortlist:
            if market_ids is not None and market.id not in market_ids:
                continue
            row_id = self._market_rows.get(market.id)
            if row_id is None:
                continue

            yes_token, no_token = self._extract_tokens(market)
            if yes_token is None or no_token is None:
                continue

            ob_yes = self._ob_manager.get_orderbook(yes_token.token_id)
            ob_no = self._ob_manager.get_orderbook(no_token.token_id)
            if ob_yes is None or ob_no is None:
                continue
            self._record_market_microstate(market, now)
            external_data = self._build_hypothesis_context(market, ob_yes, ob_no, now)
            crypto_info = classify_crypto15m_updown_market(market.question, category=market.category, tags=market.tags)
            crypto_asset_allowed = self._crypto15m_asset_allowed(crypto_info.asset) if crypto_info.is_crypto15m else True

            for runtime in self._portfolio_runtimes:
                if late_stage_only and runtime.config.track != "late_stage":
                    continue
                signal_external_data = self._prepare_signal_external_data(
                    runtime,
                    market,
                    ob_yes,
                    ob_no,
                    now,
                    base_external_data=external_data,
                )
                audit_external_data = signal_external_data
                if runtime.config.track == "crypto15m" and not crypto_asset_allowed:
                    self._audit_decision(
                        session,
                        runtime=runtime,
                        market_row_id=row_id,
                        market=market,
                        decision="rejected",
                        quality=assess_market_quality(
                            self._settings.lab.market_quality,
                            runtime.config,
                            market,
                            ob_yes,
                            now=now,
                            expected_edge=0.0,
                            fee_rate=self._fee_rate_fraction_for(yes_token.token_id),
                        ),
                        meta={
                            "stage": "portfolio_filters",
                            "reason": "asset_disabled",
                            "asset": crypto_info.asset,
                            "trade_assets": sorted(self._crypto15m_trade_assets()),
                        },
                    )
                    continue
                if row_id in open_markets_by_portfolio.get(runtime.row_id, set()):
                    continue
                if runtime.config.track == "crypto15m":
                    crypto_cfg = self._settings.lab.crypto15m
                    active_positions = open_positions.get(runtime.row_id, [])
                    if len(active_positions) >= int(crypto_cfg.max_open_positions):
                        self._audit_decision(
                            session,
                            runtime=runtime,
                            market_row_id=row_id,
                            market=market,
                            decision="rejected",
                            quality=assess_market_quality(
                                self._settings.lab.market_quality,
                                runtime.config,
                                market,
                                ob_yes,
                                now=now,
                                expected_edge=0.0,
                                fee_rate=self._fee_rate_fraction_for(yes_token.token_id),
                            ),
                            meta={
                                "stage": "portfolio_limits",
                                "reason": "active_position_exists",
                                "max_open_positions": int(crypto_cfg.max_open_positions),
                                "open_positions": len(active_positions),
                            },
                        )
                        continue
                    latest_opened_at = latest_opened_at_by_portfolio.get(runtime.row_id)
                    if latest_opened_at is not None:
                        cooldown_sec = int(crypto_cfg.entry_cooldown_sec)
                        seconds_since_entry = max(0.0, (now - latest_opened_at).total_seconds())
                        if cooldown_sec > 0 and seconds_since_entry < cooldown_sec:
                            self._audit_decision(
                                session,
                                runtime=runtime,
                                market_row_id=row_id,
                                market=market,
                                decision="rejected",
                                quality=assess_market_quality(
                                    self._settings.lab.market_quality,
                                    runtime.config,
                                    market,
                                    ob_yes,
                                    now=now,
                                    expected_edge=0.0,
                                    fee_rate=self._fee_rate_fraction_for(yes_token.token_id),
                                ),
                                meta={
                                    "stage": "portfolio_limits",
                                    "reason": "entry_cooldown",
                                    "entry_cooldown_sec": cooldown_sec,
                                    "seconds_since_entry": seconds_since_entry,
                                },
                            )
                            continue
                if self._has_working_order(
                    portfolio_id=runtime.row_id,
                    market_id=row_id,
                    action="BUY",
                ):
                    continue
                if not self._market_passes_portfolio_filters(
                    runtime.config,
                    market,
                    ob_yes,
                    now,
                    check_orderbook=runtime.config.track != "crypto15m",
                ):
                    filter_meta = {"stage": "portfolio_filters"}
                    if runtime.config.track == "crypto15m":
                        filter_meta.update({
                            "crypto_ohlcv_fresh": audit_external_data.get("crypto_ohlcv_fresh"),
                            "crypto_ohlcv_stale": audit_external_data.get("crypto_ohlcv_stale"),
                            "crypto_ohlcv_age_sec": audit_external_data.get("crypto_ohlcv_age_sec"),
                            "crypto_ohlcv_exchange": audit_external_data.get("crypto_ohlcv_exchange"),
                            "threshold": runtime.config.crypto15m_confidence_threshold,
                        })
                    quality = assess_market_quality(
                        self._settings.lab.market_quality,
                        runtime.config,
                        market,
                        ob_yes,
                        now=now,
                        expected_edge=0.0,
                        fee_rate=self._fee_rate_fraction_for(yes_token.token_id),
                    )
                    self._audit_decision(
                        session,
                        runtime=runtime,
                        market_row_id=row_id,
                        market=market,
                        decision="rejected",
                        quality=quality,
                        meta=filter_meta,
                    )
                    continue

                signal, rejected_signal = self._select_signal(
                    runtime,
                    market,
                    ob_yes,
                    ob_no,
                    now,
                    external_data=signal_external_data,
                )
                if signal is None or signal.side is None:
                    no_signal_meta = {"stage": "strategy_no_signal"}
                    if runtime.config.track == "crypto15m":
                        rejected_reason = (
                            rejected_signal.rationale
                            if rejected_signal is not None and rejected_signal.rationale
                            else (signal.rationale if signal is not None and signal.rationale else "")
                        )
                        no_signal_meta.update({
                            "reason": rejected_reason or ("crypto_ohlcv_stale" if audit_external_data.get("crypto_ohlcv_stale") else "strategy_no_signal"),
                            "signal_rationale": (
                                rejected_reason or "signal_missing"
                            ),
                            "use_learned_gate": runtime.config.use_learned_gate,
                            "crypto15m_is_market": audit_external_data.get("crypto15m_is_market"),
                            "crypto15m_symbol": audit_external_data.get("crypto15m_symbol"),
                            "crypto15m_model_enabled": self._settings.strategy.crypto15m_model.enabled,
                            "crypto15m_manifest_loaded": bool(self._crypto15m_manifest),
                            "crypto15m_bundle_loaded": bool(self._crypto15m_bundle),
                            "model_label": audit_external_data.get("crypto15m_model_label"),
                            "model_side": audit_external_data.get("crypto15m_model_side"),
                            "model_confidence": audit_external_data.get("crypto15m_model_confidence"),
                            "model_yes_probability": audit_external_data.get("crypto15m_model_yes_probability"),
                            "model_no_trade_probability": audit_external_data.get("crypto15m_model_no_trade_probability"),
                            "crypto_ohlcv_fresh": audit_external_data.get("crypto_ohlcv_fresh"),
                            "crypto_ohlcv_stale": audit_external_data.get("crypto_ohlcv_stale"),
                            "crypto_ohlcv_age_sec": audit_external_data.get("crypto_ohlcv_age_sec"),
                            "crypto_ohlcv_exchange": audit_external_data.get("crypto_ohlcv_exchange"),
                            "threshold": runtime.config.crypto15m_confidence_threshold,
                            **((rejected_signal.metadata or {}) if rejected_signal is not None else {}),
                        })
                    self._audit_decision(
                        session,
                        runtime=runtime,
                        market_row_id=row_id,
                        market=market,
                        decision="rejected",
                        quality=assess_market_quality(
                            self._settings.lab.market_quality,
                            runtime.config,
                            market,
                            ob_yes,
                            now=now,
                            expected_edge=0.0,
                            fee_rate=self._fee_rate_fraction_for(yes_token.token_id),
                        ),
                        meta=no_signal_meta,
                    )
                    continue

                trade_token = yes_token if signal.side == "YES" else no_token
                trade_orderbook = ob_yes if signal.side == "YES" else ob_no
                if not self._market_passes_portfolio_filters(runtime.config, market, trade_orderbook, now):
                    quality = assess_market_quality(
                        self._settings.lab.market_quality,
                        runtime.config,
                        market,
                        trade_orderbook,
                        now=now,
                        expected_edge=signal.edge,
                        fee_rate=self._fee_rate_fraction_for(trade_token.token_id),
                    )
                    self._audit_decision(
                        session,
                        runtime=runtime,
                        market_row_id=row_id,
                        market=market,
                        decision="rejected",
                        side=signal.side,
                        hypothesis=signal.hypothesis_id,
                        edge=signal.edge,
                        quality=quality,
                        token_id=trade_token.token_id,
                        meta={"stage": "trade_side_filters"},
                    )
                    continue

                quality = assess_market_quality(
                    self._settings.lab.market_quality,
                    runtime.config,
                    market,
                    trade_orderbook,
                    now=now,
                    expected_edge=signal.edge,
                    fee_rate=self._fee_rate_fraction_for(trade_token.token_id),
                )
                if self._settings.lab.market_quality.enabled and quality.hard_reject:
                    self._audit_decision(
                        session,
                        runtime=runtime,
                        market_row_id=row_id,
                        market=market,
                        decision="rejected",
                        side=signal.side,
                        hypothesis=signal.hypothesis_id,
                        edge=signal.edge,
                        quality=quality,
                        token_id=trade_token.token_id,
                    )
                    continue

                self._audit_decision(
                    session,
                    runtime=runtime,
                    market_row_id=row_id,
                    market=market,
                    decision="candidate",
                    side=signal.side,
                    hypothesis=signal.hypothesis_id,
                    edge=signal.edge,
                    quality=quality,
                    token_id=trade_token.token_id,
                    meta={
                        "signal_rationale": signal.rationale,
                        "threshold": runtime.config.crypto15m_confidence_threshold,
                        **signal.metadata,
                    } if runtime.config.track == "crypto15m" else None,
                )

                gate = self._score_with_learned_gate(
                    runtime,
                    market,
                    ob_yes,
                    ob_no,
                    side=signal.side,
                    market_probability=signal.market_probability,
                    external_data=external_data,
                )
                if gate.enabled and gate.should_veto:
                    self._audit_decision(
                        session,
                        runtime=runtime,
                        market_row_id=row_id,
                        market=market,
                        decision="rejected",
                        side=signal.side,
                        hypothesis=signal.hypothesis_id,
                        edge=signal.edge,
                        quality=quality,
                        token_id=trade_token.token_id,
                        meta={
                            "stage": "learned_model",
                            "reason": gate.reason,
                            "predicted_yes_probability": gate.predicted_yes_probability,
                            "candidate_confidence": gate.candidate_confidence,
                            "expected_net_ev": gate.expected_net_ev,
                            "entry_price": gate.entry_price,
                        },
                    )
                    continue

                decision = runtime.risk.evaluate_trade(
                    p_model=signal.model_probability,
                    price_ask=ob_yes.best_ask,
                    price_bid=ob_yes.best_bid,
                    bankroll=runtime.bankroll,
                    open_positions=open_positions.get(runtime.row_id, []),
                    event_id=market.event_id,
                    fee=self._fee_rate_fraction_for(trade_token.token_id),
                    no_ask=ob_no.best_ask,
                )
                if decision.action != "trade" or decision.side != signal.side:
                    self._audit_decision(
                        session,
                        runtime=runtime,
                        market_row_id=row_id,
                        market=market,
                        decision="rejected",
                        side=signal.side,
                        hypothesis=signal.hypothesis_id,
                        edge=signal.edge,
                        quality=quality,
                        token_id=trade_token.token_id,
                        meta={"stage": "risk_gate", "reason": decision.reason},
                    )
                    continue

                reward_meta = self._crypto15m_reward_guard(
                    session,
                    runtime,
                    signal=signal,
                    stake=decision.stake,
                    now=now,
                )
                if runtime.config.track == "crypto15m" and not reward_meta.get("accepted", True):
                    self._audit_decision(
                        session,
                        runtime=runtime,
                        market_row_id=row_id,
                        market=market,
                        decision="rejected",
                        side=signal.side,
                        hypothesis=signal.hypothesis_id,
                        edge=signal.edge,
                        quality=quality,
                        token_id=trade_token.token_id,
                        meta={
                            "stage": "reward_guard",
                            "reason": reward_meta.get("reason") or "reward_guard_negative",
                            "threshold": runtime.config.crypto15m_confidence_threshold,
                            **reward_meta,
                        },
                    )
                    continue

                analyst_review = self._score_with_ai_analyst(
                    runtime,
                    market,
                    signal,
                    trade_orderbook,
                    ob_no,
                    external_data=audit_external_data,
                    quality=quality,
                    now=now,
                )
                ai_soft_override = False
                if analyst_review.enabled and analyst_review.reviewed and not analyst_review.allow:
                    ai_reason = analyst_review.reason or "ai_veto"
                    hard_ai_reasons = {
                        "too_close_to_resolution",
                        "hourly_budget_reached",
                        "crypto_ohlcv_stale",
                        "stale_ohlcv",
                        "spread_too_wide",
                        "low_depth",
                        "insufficient_depth",
                        "negative_ev",
                        "fee_adjusted_ev_negative",
                        "self_trade_risk",
                    }
                    ai_soft_override = (
                        runtime.config.track == "crypto15m"
                        and bool(reward_meta.get("strong_signal_override"))
                        and ai_reason not in hard_ai_reasons
                        and float(analyst_review.confidence or 0.0) < 0.90
                    )
                    if not ai_soft_override:
                        self._audit_decision(
                            session,
                            runtime=runtime,
                            market_row_id=row_id,
                            market=market,
                            decision="rejected",
                            side=signal.side,
                            hypothesis=signal.hypothesis_id,
                            edge=signal.edge,
                            quality=quality,
                            token_id=trade_token.token_id,
                            meta={
                                "stage": "ai_analyst",
                                "reason": ai_reason,
                                "ai_model": analyst_review.model,
                                "ai_confidence": analyst_review.confidence,
                                "ai_decision": analyst_review.raw_decision or "VETO",
                                "ai_tokens_used": analyst_review.tokens_used,
                                "ai_latency_ms": analyst_review.latency_ms,
                                "threshold": runtime.config.crypto15m_confidence_threshold,
                            },
                        )
                        continue

                use_taker = self._should_use_taker_entry(runtime, market, trade_orderbook, signal, quality, now)
                taker_preview: TakerFillPreview | None = None
                execution_meta: dict[str, Any] = {}
                if use_taker:
                    orderbook_event_age_ms = self._orderbook_event_age_ms(trade_orderbook, now)
                    taker_preview = self._engine.simulate_taker_fill_notional(
                        trade_orderbook,
                        "BUY",
                        decision.stake,
                        event_age_ms=orderbook_event_age_ms,
                    )
                    if taker_preview is None or taker_preview.notional < decision.stake * 0.95:
                        self._audit_decision(
                            session,
                            runtime=runtime,
                            market_row_id=row_id,
                            market=market,
                            decision="rejected",
                            side=signal.side,
                            hypothesis=signal.hypothesis_id,
                            edge=signal.edge,
                            quality=quality,
                            token_id=trade_token.token_id,
                            meta={"stage": "execution", "reason": "insufficient_depth"},
                        )
                        continue
                    fee_usdc = self._taker_fee_usdc_for(
                        trade_token.token_id,
                        taker_preview.filled_size,
                        taker_preview.avg_price,
                    )
                    fee_per_share = fee_usdc / taker_preview.filled_size if taker_preview.filled_size > 0 else 0.0
                    execution_net_ev = signal.edge - taker_preview.slippage_per_share - fee_per_share
                    if execution_net_ev < self._settings.lab.crypto15m.min_net_ev:
                        self._audit_decision(
                            session,
                            runtime=runtime,
                            market_row_id=row_id,
                            market=market,
                            decision="rejected",
                            side=signal.side,
                            hypothesis=signal.hypothesis_id,
                            edge=signal.edge,
                            quality=quality,
                            token_id=trade_token.token_id,
                            meta={
                                "stage": "execution",
                                "reason": "fee_adjusted_ev_negative",
                                "expected_net_ev": signal.edge,
                                "execution_net_ev": execution_net_ev,
                                "fee_usdc": fee_usdc,
                                "slippage_usdc": taker_preview.slippage_usdc,
                                "effective_fill_price": taker_preview.avg_price,
                                "fee_rate_bps": self._fee_rate_bps_for(trade_token.token_id),
                                "latency_ms": orderbook_event_age_ms,
                            },
                        )
                        continue
                    quote_price = taker_preview.avg_price
                    contracts = taker_preview.filled_size
                    execution_meta = {
                        "effective_fill_price": taker_preview.avg_price,
                        "slippage_usdc": taker_preview.slippage_usdc,
                        "slippage_per_share": taker_preview.slippage_per_share,
                        "fee_usdc": fee_usdc,
                        "fee_rate_bps": self._fee_rate_bps_for(trade_token.token_id),
                        "execution_net_ev": execution_net_ev,
                        "latency_ms": orderbook_event_age_ms,
                    }
                else:
                    quote_price = self._engine.quote_entry_price(
                        trade_orderbook,
                        "BUY",
                        self._tick_size_for(trade_token.token_id),
                    )
                    contracts = decision.stake / quote_price if quote_price > 0 else 0.0
                if contracts <= 0:
                    continue

                queue = self._engine.visible_same_side_size(
                    trade_orderbook,
                    "BUY",
                    quote_price,
                )
                order = self._engine.create_order(
                    portfolio_key=runtime.key,
                    market_id=market.id,
                    market_db_id=row_id,
                    token_id=trade_token.token_id,
                    event_id=market.event_id,
                    side=signal.side,
                    action="BUY",
                    price=quote_price,
                    size=contracts,
                    queue_ahead=queue,
                    hypothesis=signal.hypothesis_id,
                    edge=signal.edge,
                    now=now,
                    order_kind="taker" if use_taker else "maker",
                )
                self._audit_decision(
                    session,
                    runtime=runtime,
                    market_row_id=row_id,
                    market=market,
                    decision="accepted",
                    side=signal.side,
                    hypothesis=signal.hypothesis_id,
                    edge=signal.edge,
                    quality=quality,
                    token_id=trade_token.token_id,
                    meta={
                        "entry_mode": "taker" if use_taker else "maker",
                        "learned_model_enabled": gate.enabled,
                        "signal_rationale": signal.rationale,
                        "threshold": runtime.config.crypto15m_confidence_threshold,
                        "predicted_yes_probability": gate.predicted_yes_probability,
                        "candidate_confidence": gate.candidate_confidence,
                        "expected_net_ev": gate.expected_net_ev,
                        "learned_entry_price": gate.entry_price,
                        "ai_enabled": analyst_review.enabled,
                        "ai_reviewed": analyst_review.reviewed,
                        "ai_cached": analyst_review.cached,
                        "ai_allow": analyst_review.allow,
                        "ai_reason": analyst_review.reason,
                        "ai_soft_override": ai_soft_override,
                        "ai_confidence": analyst_review.confidence,
                        "ai_model": analyst_review.model,
                        "ai_decision": analyst_review.raw_decision,
                        "ai_tokens_used": analyst_review.tokens_used,
                        "ai_latency_ms": analyst_review.latency_ms,
                        "latency_ms": external_data.get("latency_ms"),
                        **signal.metadata,
                        **({"reward_guard": reward_meta} if runtime.config.track == "crypto15m" else {}),
                        **execution_meta,
                    },
                )
                session.add(SignalRow(
                    market_id=row_id,
                    timestamp=now,
                    hypothesis=signal.hypothesis_id,
                    model_probability=signal.model_probability,
                    market_probability=signal.market_probability,
                    edge=signal.edge,
                    action_taken=True,
                ))
                if use_taker:
                    self._apply_instant_fill(
                        session,
                        runtime,
                        order,
                        price=quote_price,
                        timestamp=now,
                        fill_type="taker_entry",
                        preview=taker_preview,
                        latency_ms=orderbook_event_age_ms,
                    )
                else:
                    self._persist_new_order(session, runtime, order)
                if runtime.config.track == "crypto15m":
                    open_positions.setdefault(runtime.row_id, []).append(PositionInfo(
                        market_id=row_id,
                        event_id=market.event_id,
                        side=signal.side,
                        size=contracts,
                        entry_price=quote_price,
                    ))
                    open_markets_by_portfolio[runtime.row_id].add(row_id)
                    latest_opened_at_by_portfolio[runtime.row_id] = now

    def _select_signal(
        self,
        runtime: PortfolioRuntime,
        market: Market,
        orderbook: Orderbook,
        no_orderbook: Orderbook,
        now: datetime,
        *,
        external_data: dict[str, Any] | None = None,
    ) -> tuple[SignalOutput | None, SignalOutput | None]:
        best: SignalOutput | None = None
        best_rejected: SignalOutput | None = None
        base_external_data = external_data or self._build_hypothesis_context(market, orderbook, no_orderbook, now)
        for hypothesis in runtime.hypotheses:
            hypothesis_context = base_external_data
            if hypothesis.spec.id == "H7":
                hypothesis_context = self._prepare_signal_external_data(
                    runtime,
                    market,
                    orderbook,
                    no_orderbook,
                    now,
                    base_external_data=base_external_data,
                )
            signal = hypothesis.evaluate(
                market_id=market.id,
                question=market.question,
                orderbook=orderbook,
                external_data=hypothesis_context if hypothesis.spec.id in {"H6", "H7"} else None,
            )
            if signal.side is None:
                if best_rejected is None or signal.confidence > best_rejected.confidence or signal.edge > best_rejected.edge:
                    best_rejected = signal
                continue
            if best is None or signal.edge > best.edge:
                best = signal
        return best, best_rejected

    async def _on_ws_event(self, event: dict[str, Any]):
        token_id = str(event.get("asset_id") or "")
        if not token_id:
            return
        event_type = str(event.get("event_type") or "")
        event_ts = self._coerce_ws_timestamp(event.get("timestamp"))
        if event.get("event_type") == "tick_size_change":
            new_tick = event.get("new_tick_size")
            if isinstance(new_tick, (int, float)) and new_tick > 0:
                self._token_tick_sizes[token_id] = float(new_tick)
            return

        market = self._token_to_market.get(token_id)
        if market is not None and self._event_relevant(event_type):
            last_microstate = self._market_microstate_last_at.get(market.id)
            if last_microstate is None or (event_ts - last_microstate).total_seconds() >= 1.0:
                self._record_market_microstate(market, event_ts)
                self._market_microstate_last_at[market.id] = event_ts
        if market is not None and event_type == "last_trade_price":
            trade_price = event.get("price")
            trade_side = str(event.get("side") or "")
            if isinstance(trade_price, (int, float)):
                self._remember_trade(market.id, event_ts, trade_side, float(trade_price))

        relevant_orders = list(self._orders_by_token.get(token_id, set()))
        should_recheck_market = False
        if market is not None:
            last_eval = self._event_eval_last_at.get(market.id)
            if last_eval is None or (event_ts - last_eval).total_seconds() >= 1.0:
                horizon_days = time_to_resolution_days(market.end_date, event_ts)
                if horizon_days is not None and horizon_days * 24.0 <= 12.0:
                    should_recheck_market = True
        if self._entries_frozen and not relevant_orders:
            self._entries_frozen = self._should_freeze_entries(self._ws_snapshot())
            if self._entries_frozen:
                return
        if not relevant_orders and not should_recheck_market:
            return

        async with self._state_lock:
            session = get_session(self._db_url)
            try:
                orderbook = self._ob_manager.get_orderbook(token_id)
                if orderbook is not None:
                    current_mark = orderbook.best_bid if orderbook.best_bid > 0 else None
                    if current_mark is not None:
                        open_positions = (
                            session.query(LabPositionRow)
                            .filter(LabPositionRow.portfolio_id.in_(set(self._portfolios_by_id)))
                            .filter(LabPositionRow.status == "open")
                            .filter(LabPositionRow.token_id == token_id)
                            .all()
                        )
                        for position in open_positions:
                            position.current_price = current_mark
                    for order_id in relevant_orders:
                        order = self._working_orders.get(order_id)
                        if order is not None:
                            self._engine.observe_book(order, orderbook)

                if event.get("event_type") == "last_trade_price":
                    trade_price = event.get("price")
                    trade_size = event.get("size")
                    trade_side = str(event.get("side") or "")
                    if isinstance(trade_price, (int, float)) and isinstance(trade_size, (int, float)):
                        fill_time = event_ts
                        for order_id in list(relevant_orders):
                            order = self._working_orders.get(order_id)
                            if order is None:
                                continue
                            fill = self._engine.process_trade(
                                order,
                                trade_price=float(trade_price),
                                trade_size=float(trade_size),
                                aggressor_side=trade_side,
                            )
                            if fill is None:
                                continue
                            if order.status == "filled":
                                order.closed_at = fill_time
                            self._apply_fill(session, order, fill, fill_time)
                            self._sync_order_row(session, order, closed=order.status == "filled")
                            if order.status == "filled":
                                self._drop_working_order(order.order_id)

                if should_recheck_market and market is not None:
                    self._event_eval_last_at[market.id] = event_ts
                    self._persist_ws_metrics_if_due(session, event_ts)
                if should_recheck_market and market is not None and not self._entries_frozen:
                    crypto_event_market = self._market_is_crypto15m_eligible(market, event_ts)
                    self._submit_entry_orders(
                        session,
                        event_ts,
                        market_ids={market.id},
                        late_stage_only=not crypto_event_market,
                    )

                self._update_runtime_status_row(
                    session,
                    ws_connected=bool(self._ws and self._ws.is_connected),
                    last_cycle_ts=event_ts,
                    subscribed_tokens_last=len(self._desired_subscriptions),
                )
                session.commit()
            finally:
                session.close()

    def _apply_fill(
        self,
        session,
        order: ShadowOrderState,
        fill: ShadowFill,
        timestamp: datetime,
    ):
        row_id = self._order_row_ids.get(order.order_id)
        if row_id is None:
            return

        runtime = self._portfolio_by_key(order.portfolio_key)
        if runtime is None:
            return

        fee_usdc = float(fill.fee_usdc or 0.0)
        if fee_usdc <= 0.0 and fill.fill_type in {"taker_entry", "forced_taker_exit"}:
            fee_usdc = self._taker_fee_usdc_for(order.token_id, fill.filled_size, fill.price)
            fill.fee_usdc = fee_usdc
            fill.fee_rate_bps = self._fee_rate_bps_for(order.token_id)
        fill.effective_fill_price = fill.effective_fill_price or fill.price
        fill_outcome_reason = ""

        session.add(LabFillRow(
            portfolio_id=runtime.row_id,
            order_id=row_id,
            market_id=order.market_db_id,
            token_id=order.token_id,
            timestamp=timestamp,
            side=order.side,
            price=fill.price,
            size=fill.filled_size,
            notional=fill.notional,
            fill_type=fill.fill_type,
        ))

        if order.action == "BUY":
            position = (
                session.query(LabPositionRow)
                .filter(LabPositionRow.portfolio_id == runtime.row_id)
                .filter(LabPositionRow.market_id == order.market_db_id)
                .filter(LabPositionRow.token_id == order.token_id)
                .filter(LabPositionRow.status == "open")
                .first()
            )
            if position is None:
                position = LabPositionRow(
                    portfolio_id=runtime.row_id,
                    market_id=order.market_db_id,
                    token_id=order.token_id,
                    event_id=order.event_id,
                    side=order.side,
                    strategy_key=runtime.key,
                    hypothesis=order.hypothesis,
                    entry_price=fill.price,
                    current_price=fill.price,
                    size=fill.filled_size,
                    opened_at=timestamp,
                    realized_pnl=-fee_usdc,
                    status="open",
                )
                session.add(position)
            else:
                new_size = position.size + fill.filled_size
                avg_entry = (
                    (position.entry_price * position.size) + fill.notional
                ) / new_size
                position.entry_price = avg_entry
                position.size = new_size
                position.current_price = fill.price
                position.realized_pnl = float(position.realized_pnl or 0.0) - fee_usdc
            if fee_usdc:
                runtime.bankroll -= fee_usdc
                runtime.risk.record_pnl(-fee_usdc)
            market = self._market_by_db_id(order.market_db_id)
            if market is not None:
                self._audit_decision(
                    session,
                    runtime=runtime,
                    market_row_id=order.market_db_id,
                    market=market,
                    decision="entered",
                    side=order.side,
                    hypothesis=order.hypothesis,
                    edge=order.edge,
                    token_id=order.token_id,
                    meta={
                        "fill_type": fill.fill_type,
                        "order_kind": order.order_kind,
                        "fee_usdc": fee_usdc,
                        "slippage_usdc": fill.slippage_usdc,
                        "effective_fill_price": fill.effective_fill_price,
                        "fee_rate_bps": fill.fee_rate_bps,
                        "latency_ms": fill.latency_ms,
                    },
                )
        else:
            position = (
                session.query(LabPositionRow)
                .filter(LabPositionRow.portfolio_id == runtime.row_id)
                .filter(LabPositionRow.market_id == order.market_db_id)
                .filter(LabPositionRow.token_id == order.token_id)
                .filter(LabPositionRow.status == "open")
                .first()
            )
            if position is None:
                return

            closed_size = min(position.size, fill.filled_size)
            gross = closed_size * self._position_price_delta(position.side, position.entry_price, fill.price)
            fee = min(fee_usdc, fee_usdc * closed_size / fill.filled_size) if fill.filled_size > 0 else 0.0
            realized = gross - fee
            fill_outcome_reason = "profit" if realized > 0 else "loss" if realized < 0 else "flat"

            position.size -= closed_size
            position.current_price = fill.price
            position.realized_pnl = float(position.realized_pnl or 0.0) + realized
            runtime.bankroll += realized
            runtime.risk.record_pnl(realized)

            if position.size <= 1e-9:
                position.size = 0.0
                position.status = "closed"
                position.closed_at = timestamp
                position.pnl = float(position.realized_pnl or 0.0)
                position.exit_reason = order.reason or "signal_exit"
                position.forced_exit = bool(order.forced_exit or fill.fill_type == "forced_taker_exit")

        session.add(AuditRow(
            timestamp=timestamp,
            event_type="shadow_fill",
            details=json.dumps({
                "portfolio": order.portfolio_key,
                "market_id": order.market_db_id,
                "token_id": order.token_id,
                "side": order.side,
                "action": order.action,
                "price": fill.price,
                "size": fill.filled_size,
                "fill_type": fill.fill_type,
                "trigger_reason": order.reason or "",
                "fill_outcome_reason": fill_outcome_reason,
                "forced_exit": order.forced_exit,
                "fee_usdc": fee_usdc,
                "slippage_usdc": fill.slippage_usdc,
                "effective_fill_price": fill.effective_fill_price,
                "fee_rate_bps": fill.fee_rate_bps,
                "latency_ms": fill.latency_ms,
            }),
        ))

    def _persist_new_order(self, session, runtime: PortfolioRuntime, order: ShadowOrderState):
        row = LabOrderRow(
            portfolio_id=runtime.row_id,
            market_id=order.market_db_id,
            token_id=order.token_id,
            event_id=order.event_id,
            side=order.side,
            action=order.action,
            price=order.price,
            size_total=order.size_total,
            size_remaining=order.size_remaining,
            filled_size=order.filled_size,
            status=order.status,
            order_kind=order.order_kind,
            queue_ahead=order.queue_ahead,
            visible_size_same_side=order.visible_size_same_side,
            reprices=order.reprices,
            ttl_sec=self._settings.lab.execution.ttl_sec,
            hypothesis=order.hypothesis,
            edge=order.edge,
            forced_exit=order.forced_exit,
            close_reason=order.reason or None,
            created_at=order.created_at,
            last_repriced_at=order.last_repriced_at,
            expires_at=order.expires_at,
            closed_at=order.closed_at,
        )
        session.add(row)
        session.flush()

        self._working_orders[order.order_id] = order
        self._order_row_ids[order.order_id] = row.id
        self._orders_by_token[order.token_id].add(order.order_id)

    def _sync_order_row(self, session, order: ShadowOrderState, *, closed: bool):
        row_id = self._order_row_ids.get(order.order_id)
        if row_id is None:
            return
        row = session.get(LabOrderRow, row_id)
        if row is None:
            return
        row.price = order.price
        row.size_remaining = order.size_remaining
        row.filled_size = order.filled_size
        row.status = order.status
        row.order_kind = order.order_kind
        row.queue_ahead = order.queue_ahead
        row.visible_size_same_side = order.visible_size_same_side
        row.reprices = order.reprices
        row.last_repriced_at = order.last_repriced_at
        row.expires_at = order.expires_at
        row.forced_exit = order.forced_exit
        row.close_reason = order.reason or None
        if closed:
            row.closed_at = order.closed_at or utcnow()

    def _sample_equity(self, session, now: datetime):
        for runtime in self._portfolio_runtimes:
            open_positions = (
                session.query(LabPositionRow)
                .filter(LabPositionRow.portfolio_id == runtime.row_id)
                .filter(LabPositionRow.status == "open")
                .all()
            )
            unrealized = sum(
                position.size
                * self._position_price_delta(
                    position.side,
                    position.entry_price,
                    position.current_price if position.current_price is not None else position.entry_price,
                )
                for position in open_positions
            )
            realized = runtime.bankroll - runtime.initial_bankroll
            equity = runtime.bankroll + unrealized
            runtime.peak_equity = max(runtime.peak_equity, equity)
            drawdown = 0.0
            if runtime.peak_equity > 0:
                drawdown = max(0.0, (runtime.peak_equity - equity) / runtime.peak_equity)

            session.add(LabEquityPointRow(
                portfolio_id=runtime.row_id,
                timestamp=now,
                bankroll=runtime.bankroll,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                equity=equity,
                drawdown_pct=drawdown,
            ))

    def _has_working_order(
        self,
        *,
        portfolio_id: int,
        market_id: int,
        action: str,
        token_id: str | None = None,
    ) -> bool:
        for order in self._working_orders.values():
            runtime = self._portfolio_by_key(order.portfolio_key)
            if runtime is None or runtime.row_id != portfolio_id:
                continue
            if order.market_db_id != market_id:
                continue
            if order.action != action:
                continue
            if token_id is not None and order.token_id != token_id:
                continue
            if order.status in {"working", "partial"}:
                return True
        return False

    def _drop_working_order(self, order_id: str):
        order = self._working_orders.pop(order_id, None)
        self._order_row_ids.pop(order_id, None)
        if order is not None:
            self._orders_by_token[order.token_id].discard(order_id)
            if not self._orders_by_token[order.token_id]:
                self._orders_by_token.pop(order.token_id, None)

    def _open_position_tokens(self) -> set[str]:
        session = get_session(self._db_url)
        try:
            active_portfolio_ids = set(self._portfolios_by_id)
            if not active_portfolio_ids:
                return set()
            rows = (
                session.query(LabPositionRow.token_id)
                .filter(LabPositionRow.portfolio_id.in_(active_portfolio_ids))
                .filter(LabPositionRow.status == "open")
                .all()
            )
            return {row[0] for row in rows if row[0]}
        finally:
            session.close()

    def _portfolio_by_key(self, key: str) -> PortfolioRuntime | None:
        for runtime in self._portfolio_runtimes:
            if runtime.key == key:
                return runtime
        return None

    def _market_by_db_id(self, market_db_id: int) -> Market | None:
        for market in self._shortlist:
            if self._market_rows.get(market.id) == market_db_id:
                return market
        for market in self._token_to_market.values():
            if self._market_rows.get(market.id) == market_db_id:
                return market
        return None

    def _tick_size_for(self, token_id: str) -> float:
        return self._token_tick_sizes.get(token_id, self._settings.lab.execution.tick_size_default)

    @staticmethod
    def _extract_tokens(market: Market) -> tuple[Token | None, Token | None]:
        yes_token = next((token for token in market.tokens if token.outcome.lower() in {"yes", "up"}), None)
        no_token = next((token for token in market.tokens if token.outcome.lower() in {"no", "down"}), None)
        return yes_token, no_token

    @staticmethod
    def _coerce_ws_timestamp(value: Any) -> datetime:
        if isinstance(value, datetime):
            return _as_utc(value) or utcnow()
        if isinstance(value, (int, float)):
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric = numeric / 1000.0
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        if isinstance(value, str) and value:
            normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
            try:
                return datetime.fromisoformat(normalized).astimezone(timezone.utc)
            except ValueError:
                return utcnow()
        return utcnow()

    def _ensure_market_row(self, session, market: Market) -> MarketRow:
        row = (
            session.query(MarketRow)
            .filter(MarketRow.polymarket_id == market.id)
            .first()
        )
        yes_token, no_token = self._extract_tokens(market)
        end_date = parse_market_end_date(market.end_date)

        if row is None:
            row = MarketRow(
                polymarket_id=market.id,
                event_id=market.event_id,
                question=market.question,
                category=market.category,
                end_date=end_date,
                resolution_source=market.resolution_source,
                active=market.active,
                volume_24h=market.volume_24h,
                yes_token_id=yes_token.token_id if yes_token else "",
                no_token_id=no_token.token_id if no_token else "",
                tags=list(market.tags or []),
            )
            session.add(row)
            session.flush()
            return row

        row.event_id = market.event_id
        row.question = market.question
        row.category = market.category
        row.end_date = end_date
        row.resolution_source = market.resolution_source
        row.active = market.active
        row.volume_24h = market.volume_24h
        row.tags = list(market.tags or [])
        if yes_token is not None:
            row.yes_token_id = yes_token.token_id
        if no_token is not None:
            row.no_token_id = no_token.token_id
        return row

    def _store_market_snapshot(
        self,
        session,
        market_row_id: int,
        market: Market,
        yes_orderbook: Orderbook,
        no_orderbook: Orderbook | None,
    ):
        now = utcnow()
        session.add(PriceHistoryRow(
            market_id=market_row_id,
            timestamp=now,
            bid=yes_orderbook.best_bid,
            ask=yes_orderbook.best_ask,
            mid=yes_orderbook.mid_price,
            spread=yes_orderbook.spread,
            volume_24h=market.volume_24h,
            depth_bid=yes_orderbook.depth("bid"),
            depth_ask=yes_orderbook.depth("ask"),
            no_mid=no_orderbook.mid_price if no_orderbook is not None else None,
            source="lab",
        ))
        session.add(OrderbookRawRow(
            market_id=market_row_id,
            timestamp=now,
            bids_json=[
                {"price": level.price, "size": level.size}
                for level in yes_orderbook.bids[:20]
            ],
            asks_json=[
                {"price": level.price, "size": level.size}
                for level in yes_orderbook.asks[:20]
            ],
            mid=yes_orderbook.mid_price,
            spread=yes_orderbook.spread,
        ))

    def _setup_signal_handlers(self):
        if sys.platform == "win32":
            return
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

    async def _shutdown(self):
        self._running = False
        if self._ws is not None:
            await self._ws.close()
        await self._crypto_ohlcv_feed.stop()
        self._ai_analyst.close()

        session = get_session(self._db_url)
        try:
            self._persist_ws_metrics_if_due(session, utcnow(), force=True)
            session.add(AuditRow(
                timestamp=utcnow(),
                event_type="shadow_lab_shutdown",
                details=json.dumps({
                    "portfolios": [runtime.key for runtime in self._portfolio_runtimes],
                    "markets": len(self._shortlist),
                    "working_orders": len(self._working_orders),
                }),
            ))
            self._update_runtime_status_row(
                session,
                ws_connected=False,
                last_cycle_ts=utcnow(),
                last_cycle_ok=True,
                last_cycle_error=None,
                subscribed_tokens_last=0,
            )
            session.commit()
        finally:
            session.close()

        await self._client.close()
        await self._alerts.close()
        runtime_state.set_ws_connected(False)
        logger.info("Shadow lab stopped")
