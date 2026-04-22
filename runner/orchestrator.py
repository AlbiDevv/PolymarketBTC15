from __future__ import annotations

import asyncio
import json
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from loguru import logger

from config import Settings
from exchange_client.polymarket import PolymarketClient
from exchange_client.liquidity import LiquidityFilter
from exchange_client.circuit_breaker import CircuitBreaker, HeartbeatMonitor
from exchange_client.base import Market, Orderbook
from execution.broker import ExecutionBroker, TradeIntent, FillResult
from execution.paper_broker import PaperBroker, PaperBrokerConfig
from execution.live_broker import LiveBroker, DryRunBroker
from market_data.orderbook_manager import OrderbookManager
from market_data.ws_client import PolymarketWebSocket
from models.hypothesis import (
    HypothesisBase,
    SignalOutput,
    H1_NewsLag,
    H2_RoundNumberBias,
    H4_UnderpricedTails,
)
from risk.manager import RiskManager, TradeDecision
from risk.limits import PositionInfo
from monitor.alerts import TelegramAlerter
from monitor.cycle_alert_throttle import CycleAlertThrottle
from monitor.exit_reason_map import map_exit_reason_from_audit
from db.session import get_session, init_db
from db.models import (
    MarketRow,
    PriceHistoryRow,
    PositionRow,
    OrderRow,
    PnlLogRow,
    SignalRow,
    SettlementRow,
    AuditRow,
)
from db.gate_state import get_or_create_gate_state
from monitor import runtime_state


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PipelineCounters:
    """Per-cycle diagnostics: where markets dropped out of the signal → trade path."""
    skip_tokens: int = 0
    skip_orderbook: int = 0
    skip_spread: int = 0
    skip_liquidity: int = 0
    skip_no_signal: int = 0
    skip_duplicate: int = 0
    skip_risk: int = 0
    skip_risk_max_edge: float = 0.0
    evaluated_ok: int = 0  # reached hypothesis loop with OB + liq

    def reset(self) -> None:
        self.skip_tokens = 0
        self.skip_orderbook = 0
        self.skip_spread = 0
        self.skip_liquidity = 0
        self.skip_no_signal = 0
        self.skip_duplicate = 0
        self.skip_risk = 0
        self.skip_risk_max_edge = 0.0
        self.evaluated_ok = 0


class Orchestrator:
    """
    Main trading loop — paper-trading-first architecture.

    Each cycle:
      1. Fetch market data (REST snapshot; WS updates between cycles if connected)
      2. Review open positions → exit if stop-loss / take-profit / time-out
      3. Evaluate new signals → risk check → execute via broker
      4. Poll settlements
      5. Write PnL snapshot
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = PolymarketClient(settings)
        self._liquidity = LiquidityFilter(settings.liquidity)
        self._circuit = CircuitBreaker(
            failure_threshold=3, recovery_timeout_sec=300,
        )
        self._heartbeat = HeartbeatMonitor(timeout_sec=180)

        init_db(settings.database.url)
        self._db_url = settings.database.url

        bankroll = settings.bankroll.initial
        self._risk = RiskManager(settings, initial_bankroll=bankroll)
        self._bankroll = bankroll
        self._fee_rate = getattr(settings.strategy, "fee_rate", 0.02)

        self._hypotheses = self._init_hypotheses(settings)
        self._broker = self._init_broker(settings)
        self._alerter = self._init_alerter(settings)

        # Local orderbook cache — populated by REST, optionally updated by WS
        self._ob_manager = OrderbookManager()
        self._ws: PolymarketWebSocket | None = None

        self._running = False
        self._cycle_count = 0
        self._recent_order_keys: set[str] = set()
        self._cycle_alerts = CycleAlertThrottle()
        self._pipe = PipelineCounters()
        self._market_cache: list[Market] = []
        self._liquid_market_cache: list[Market] = []
        self._last_market_refresh: datetime | None = None
    # ──────────────────── Init helpers ────────────────────

    @staticmethod
    def _init_hypotheses(settings: Settings) -> list[HypothesisBase]:
        registry: dict[str, type[HypothesisBase]] = {
            "H1": H1_NewsLag,
            "H2": H2_RoundNumberBias,
            "H4": H4_UnderpricedTails,
        }
        active = []
        for h_id in settings.strategy.hypotheses:
            cls = registry.get(h_id)
            if cls:
                active.append(cls())
                logger.info(f"Hypothesis {h_id} ({cls.__name__}) enabled")
            else:
                logger.warning(f"Unknown hypothesis '{h_id}' in config — skipped")
        if not active:
            logger.warning("No hypotheses enabled! Bot will collect data only.")
        return active

    def _init_broker(self, settings: Settings) -> ExecutionBroker:
        mode = settings.mode
        if mode == "dry_run":
            logger.info("Execution broker: DRY RUN (log only)")
            return DryRunBroker()
        elif mode == "paper":
            cfg = PaperBrokerConfig(fee_rate=self._fee_rate)
            logger.info("Execution broker: PAPER (simulated fills)")
            return PaperBroker(cfg)
        elif mode == "live":
            logger.info("Execution broker: LIVE (real orders)")
            return LiveBroker(self._client, fee_rate=self._fee_rate)
        else:
            logger.warning(f"Unknown mode '{mode}', defaulting to dry_run")
            return DryRunBroker()

    @staticmethod
    def _init_alerter(settings: Settings) -> TelegramAlerter:
        return TelegramAlerter(
            bot_token=settings.alerts.telegram_bot_token,
            chat_id=settings.alerts.telegram_chat_id,
            enabled=settings.alerts.telegram_enabled,
        )

    # ──────────────────────────── Lifecycle ────────────────────────────

    async def start(self):
        self._running = True
        runtime_state.mark_started()
        self._setup_signal_handlers()

        logger.info(
            f"Orchestrator starting | mode={self._settings.mode} | "
            f"broker={self._broker.mode} | "
            f"bankroll=${self._bankroll:.2f} | "
            f"hypotheses={[h.spec.id for h in self._hypotheses]} | "
            f"exit={{'sl': {self._settings.strategy.exit.stop_loss_pct:.0%}, "
            f"'tp': {self._settings.strategy.exit.take_profit_pct:.0%}}} | "
            f"cycle={self._settings.strategy.cycle_interval_sec}s | "
            f"market_refresh={self._settings.strategy.market_refresh_sec}s | "
            f"live_decision={self._settings.strategy.live_decision_interval_sec}s"
        )

        await self._reconcile_on_startup()
        await self._start_websocket()

        s0 = get_session(self._db_url)
        try:
            self._sync_gate_state(s0)
            s0.commit()
        finally:
            s0.close()

        if self._settings.alerts.telegram_enabled:
            await self._alerter.notify_startup(
                self._settings.mode,
                self._bankroll,
                [h.spec.id for h in self._hypotheses],
                self._settings.strategy.cycle_interval_sec,
            )

        try:
            while self._running:
                await self._run_cycle()
                self._heartbeat.beat()
                await asyncio.sleep(self._decision_interval_sec())
        except asyncio.CancelledError:
            logger.info("Orchestrator cancelled")
        finally:
            await self._shutdown()

    async def stop(self):
        self._running = False

    def _decision_interval_sec(self) -> int:
        if self._settings.mode == "live":
            return self._settings.strategy.live_decision_interval_sec
        return self._settings.strategy.cycle_interval_sec

    def _should_refresh_markets(self) -> bool:
        if not self._market_cache:
            return True
        if self._last_market_refresh is None:
            return True
        elapsed = (_utcnow() - self._last_market_refresh).total_seconds()
        return elapsed >= self._settings.strategy.market_refresh_sec

    async def _get_cycle_markets(self) -> tuple[list[Market], list[Market], bool]:
        use_cache = self._settings.mode == "live" and not self._should_refresh_markets()
        if use_cache:
            return self._market_cache, self._liquid_market_cache, False

        markets = await self._client.get_markets(active_only=True)
        liquid_markets = [m for m in markets if self._liquidity.check_market(m).passed]
        liquid_markets.sort(key=lambda market: market.volume_24h, reverse=True)
        liquid_markets = liquid_markets[: self._settings.strategy.max_markets_per_cycle]
        self._market_cache = markets
        self._liquid_market_cache = liquid_markets
        self._last_market_refresh = _utcnow()
        return markets, liquid_markets, True

    async def _start_websocket(self):
        """Prepare WS client. Actual connection starts on first subscription."""
        ws_url = self._settings.exchange.ws_url
        if not ws_url:
            return
        try:
            self._ws = PolymarketWebSocket(
                ws_url=ws_url,
                ob_manager=self._ob_manager,
                on_error=lambda e: logger.warning(f"WS error: {e}"),
                on_disconnect=lambda: self._ob_manager.invalidate_all(),
            )
            logger.info("WebSocket client ready (connects on first token subscribe)")
        except Exception as e:
            logger.warning(f"WebSocket start failed (non-fatal): {e}")
            self._ws = None

    # ───────────────────── Reconciliation ─────────────────────

    async def _reconcile_on_startup(self):
        logger.info("Running startup reconciliation...")
        session = get_session(self._db_url)
        report = {
            "timestamp": _utcnow().isoformat(),
            "stale_orders_cancelled": 0,
            "positions_synced": 0,
            "discrepancies": [],
        }

        try:
            pending_orders = (
                session.query(OrderRow)
                .filter(OrderRow.status == "pending")
                .all()
            )
            for order in pending_orders:
                if order.exchange_order_id:
                    cancelled = await self._broker.cancel(order.exchange_order_id)
                    if cancelled:
                        report["stale_orders_cancelled"] += 1
                order.status = "cancelled"
                order.cancelled_at = _utcnow()

            if self._settings.mode == "live":
                try:
                    exchange_positions = await self._client.get_positions()
                    local_open = (
                        session.query(PositionRow)
                        .filter(PositionRow.status == "open")
                        .all()
                    )
                    exchange_tokens = {p.token_id for p in exchange_positions}
                    local_tokens = {p.token_id for p in local_open}

                    for token in local_tokens - exchange_tokens:
                        report["discrepancies"].append(f"Local position {token} not on exchange")
                    for token in exchange_tokens - local_tokens:
                        report["discrepancies"].append(f"Exchange position {token} not in local DB")
                    report["positions_synced"] = len(exchange_tokens & local_tokens)
                except Exception as e:
                    logger.warning(f"Position sync skipped: {e}")
                    report["discrepancies"].append(f"Position sync error: {e}")

            last_pnl = session.query(PnlLogRow).order_by(PnlLogRow.date.desc()).first()
            if last_pnl:
                self._bankroll = last_pnl.bankroll
                logger.info(f"Restored bankroll from DB: ${self._bankroll:.2f}")

            session.add(AuditRow(
                timestamp=_utcnow(), event_type="reconciliation",
                details=json.dumps(report),
            ))
            session.commit()
        except Exception as e:
            logger.error(f"Reconciliation failed: {e}")
            session.rollback()
        finally:
            session.close()

        if report["discrepancies"]:
            for d in report["discrepancies"]:
                logger.warning(f"  reconciliation: {d}")

    # ──────────────────────── Main Cycle ────────────────────────

    async def _run_cycle(self):
        self._cycle_count += 1
        logger.info(f"--- Cycle {self._cycle_count} ---")

        if not self._circuit.is_allowed:
            logger.warning("Circuit breaker OPEN — skipping cycle")
            return

        try:
            markets, liquid_markets, refreshed = await self._get_cycle_markets()
            self._circuit.record_success()
        except Exception as e:
            self._circuit.record_failure(str(e))
            if self._market_cache:
                markets = self._market_cache
                liquid_markets = self._liquid_market_cache
                refreshed = False
                logger.warning(f"get_markets failed, using cached snapshot: {e}")
            else:
                runtime_state.update_cycle_error(f"get_markets: {e}")
                logger.error(f"get_markets failed: {e}")
                if self._settings.alerts.telegram_enabled:
                    for msg in self._cycle_alerts.on_failure(f"get_markets: {e}"):
                        await self._alerter.notify_cycle_failure(msg)
                return

        source = "REST" if refreshed else "CACHE"
        logger.info(f"Fetched {len(markets)} active markets [{source}]")
        logger.info(f"{len(liquid_markets)} markets pass volume filter")

        cycle_committed_ok = False
        session = get_session(self._db_url)
        try:
            # === Phase 1: Review open positions (exit management) ===
            exits_this_cycle = await self._review_open_positions(session, markets)

            # === Phase 2: Evaluate new opportunities ===
            open_positions_db = (
                session.query(PositionRow).filter(PositionRow.status == "open").all()
            )
            open_positions = [
                PositionInfo(
                    market_id=p.market_id,
                    event_id=p.event_id or self._get_event_id(session, p.market_id),
                    side=p.side, size=p.size, entry_price=p.entry_price,
                )
                for p in open_positions_db
            ]

            self._pipe.reset()
            trades_this_cycle = 0
            for market in liquid_markets:
                try:
                    traded = await self._evaluate_market(session, market, open_positions)
                    if traded:
                        trades_this_cycle += 1
                except Exception as e:
                    logger.error(f"Error evaluating {market.id}: {e}")

            # === Phase 3: Settlements ===
            await self._poll_settlements(session)

            self._write_daily_pnl_snapshot(session)
            self._log_cycle_summary(
                session, trades_this_cycle, exits_this_cycle, len(liquid_markets),
            )
            self._sync_gate_state(session)
            session.commit()
            runtime_state.update_cycle_ok(
                markets_count=len(markets),
                ws_connected=bool(self._ws and self._ws.is_connected),
            )
            cycle_committed_ok = True
        except Exception as e:
            logger.error(f"Cycle {self._cycle_count} failed: {e}")
            session.rollback()
            runtime_state.update_cycle_error(str(e))
            err_sess = get_session(self._db_url)
            try:
                self._bump_paper_error(err_sess)
                err_sess.commit()
            finally:
                err_sess.close()
            if self._settings.alerts.telegram_enabled:
                for msg in self._cycle_alerts.on_failure(str(e)):
                    await self._alerter.notify_cycle_failure(msg)
        finally:
            session.close()

        if self._settings.alerts.telegram_enabled and cycle_committed_ok:
            if self._cycle_alerts.on_success():
                await self._alerter.notify_recovery()

        self._recent_order_keys.clear()

    # ═══════════════════ EXIT MANAGEMENT ═══════════════════

    async def _review_open_positions(self, session, markets: list[Market]) -> int:
        """
        Check each open position against exit rules, then close via broker (SELL)
        against the native outcome-token orderbook.
        """
        exit_cfg = self._settings.strategy.exit
        if not exit_cfg.enabled:
            return 0

        open_positions = (
            session.query(PositionRow).filter(PositionRow.status == "open").all()
        )
        if not open_positions:
            return 0

        market_map: dict[str, Market] = {m.id: m for m in markets}
        exits = 0

        for pos in open_positions:
            market_row = session.get(MarketRow, pos.market_id)
            if not market_row:
                continue

            current_price = await self._mark_price_for_position(pos, market_row, market_map)
            if current_price is None:
                continue

            pos.current_price = current_price
            entry_cost = pos.entry_price
            pct_change = (current_price - entry_cost) / entry_cost if entry_cost > 0 else 0

            exit_reason = None
            if pct_change <= -exit_cfg.stop_loss_pct:
                exit_reason = (
                    f"STOP-LOSS ({pct_change:+.1%} <= -{exit_cfg.stop_loss_pct:.0%})"
                )
            elif pct_change >= exit_cfg.take_profit_pct:
                exit_reason = (
                    f"TAKE-PROFIT ({pct_change:+.1%} >= +{exit_cfg.take_profit_pct:.0%})"
                )
            elif exit_cfg.time_exit_hours > 0 and pos.opened_at:
                age = (
                    _utcnow() - pos.opened_at.replace(tzinfo=timezone.utc)
                    if pos.opened_at.tzinfo is None
                    else _utcnow() - pos.opened_at
                )
                if age > timedelta(hours=exit_cfg.time_exit_hours):
                    exit_reason = (
                        f"TIME-EXIT (open {age.total_seconds()/3600:.1f}h > "
                        f"{exit_cfg.time_exit_hours}h)"
                    )

            if not exit_reason:
                continue

            market = market_map.get(market_row.polymarket_id)
            if not market:
                logger.warning(
                    f"Exit skipped: market {market_row.polymarket_id} not in this cycle's list"
                )
                continue

            if await self._execute_exit_via_broker(
                session, pos, market_row, market, exit_reason,
            ):
                exits += 1

        return exits

    async def _mark_price_for_position(
        self,
        pos: PositionRow,
        market_row: MarketRow,
        market_map: dict[str, Market],
    ) -> float | None:
        """Long outcome token: mark at best bid on that token's book (sale price)."""
        ob = await self._ensure_orderbook_for_token(pos.token_id)
        if ob and ob.best_bid > 0:
            return ob.best_bid

        market = market_map.get(market_row.polymarket_id)
        if market and market.tokens:
            for t in market.tokens:
                if t.token_id == pos.token_id:
                    return t.price
        return None

    async def _ensure_orderbook_for_token(self, token_id: str) -> Orderbook | None:
        if not token_id:
            return None
        local = self._ob_manager.get(token_id)
        ob = self._ob_manager.get_orderbook(token_id)
        if ob and local and not local.is_stale:
            return ob
        try:
            ob = await self._client.get_orderbook(token_id)
            self._ob_manager.apply_snapshot(token_id, ob)
            self._circuit.record_success()
            if self._ws:
                await self._ws.subscribe([token_id])
            return ob
        except Exception as e:
            self._circuit.record_failure(str(e))
            logger.debug(f"orderbook fetch failed for {token_id[:12]}...: {e}")
            return None

    async def _execute_exit_via_broker(
        self,
        session,
        pos: PositionRow,
        market_row: MarketRow,
        market: Market,
        reason: str,
    ) -> bool:
        ob = await self._ensure_orderbook_for_token(pos.token_id)
        if not ob or not ob.bids:
            logger.warning(f"Exit: no bids for token {pos.token_id[:16]}...")
            return False

        floor = max(0.001, ob.best_bid * 0.01)
        intent = TradeIntent(
            market_id=str(market_row.id),
            condition_id=market.id,
            token_id=pos.token_id,
            side=pos.side,
            action="SELL",
            price=floor,
            stake_usd=0.0,
            contracts=pos.size,
            entry_price=pos.entry_price,
        )

        fill = await self._broker.execute(intent, ob)
        if fill.status == "REJECTED" or fill.filled_contracts <= 0:
            logger.warning(f"Exit broker rejected: {fill.reason}")
            return False

        self._apply_exit_fill(session, pos, market_row, intent, fill, reason)
        return True

    def _apply_exit_fill(
        self,
        session,
        pos: PositionRow,
        market_row: MarketRow,
        intent: TradeIntent,
        fill: FillResult,
        reason: str,
    ):
        filled = fill.filled_contracts
        avg = fill.avg_fill_price
        entry = pos.entry_price
        pnl = filled * (avg - entry) - fill.fees_usd

        self._bankroll += pnl
        self._risk.record_pnl(pnl)

        if self._settings.mode == "paper":
            gs = get_or_create_gate_state(session)
            gs.paper_trades_count += 1
            gs.paper_realized_pnl += pnl

        orig_size = pos.size
        remaining = orig_size - filled

        if remaining > 1e-6:
            pos.size = remaining
            pos.current_price = avg
            logger.info(
                f"EXIT PARTIAL [{reason}]: '{market_row.question[:50]}' | "
                f"closed {filled:.2f} of {orig_size:.2f} @ {avg:.4f} | PnL=${pnl:.2f}"
            )
        else:
            pos.pnl = pnl
            pos.status = "closed"
            pos.closed_at = _utcnow()
            pos.current_price = avg
            pos.exit_reason = map_exit_reason_from_audit(reason)
            logger.info(
                f"EXIT [{reason}]: '{market_row.question[:50]}' | "
                f"side={pos.side} | entry={entry:.4f} → exit={avg:.4f} | "
                f"PnL=${pnl:.2f} | bankroll=${self._bankroll:.2f}"
            )

        session.add(OrderRow(
            market_id=market_row.id,
            exchange_order_id=fill.order_id,
            token_id=intent.token_id,
            side="SELL",
            price=avg,
            size=filled,
            filled_size=filled,
            status=fill.status.lower(),
        ))

        session.add(AuditRow(
            timestamp=_utcnow(), event_type="position_exit",
            details=json.dumps({
                "market_id": market_row.id,
                "reason": reason,
                "side": pos.side,
                "entry_price": entry,
                "exit_price": avg,
                "contracts": filled,
                "pnl": round(pnl, 4),
                "bankroll": round(self._bankroll, 2),
                "mode": self._broker.mode,
                "order_id": fill.order_id,
                "fees_usd": round(fill.fees_usd, 4),
                "slippage": round(fill.slippage, 6),
            }),
        ))

        asyncio.ensure_future(
            self._alerter.send_position_exit(market_row.question, reason, pnl)
        )

    def _bump_paper_error(self, session):
        if self._settings.mode != "paper":
            return
        gs = get_or_create_gate_state(session)
        gs.paper_errors_count += 1

    def _sync_gate_state(self, session):
        gs = get_or_create_gate_state(session)
        gs.gate_status = self._settings.mode
        if self._settings.mode == "paper":
            if gs.paper_started_at is None:
                gs.paper_started_at = _utcnow()
            t0 = gs.paper_started_at
            if t0 is not None:
                t0 = t0.replace(tzinfo=timezone.utc) if t0.tzinfo is None else t0
                gs.paper_days_completed = max(0, (_utcnow() - t0).days)

    # ═══════════════════ SIGNAL EVALUATION ═══════════════════

    async def _evaluate_market(
        self,
        session,
        market: Market,
        open_positions: list[PositionInfo],
    ) -> bool:
        if not market.tokens or len(market.tokens) < 2:
            self._pipe.skip_tokens += 1
            return False

        yes_token = next((t for t in market.tokens if t.outcome.lower() == "yes"), None)
        no_token = next((t for t in market.tokens if t.outcome.lower() == "no"), None)
        if not yes_token:
            self._pipe.skip_tokens += 1
            return False

        ob_yes = await self._ensure_orderbook_for_token(yes_token.token_id)
        if not ob_yes:
            self._pipe.skip_orderbook += 1
            return False

        ob_no: Orderbook | None = None
        if no_token:
            ob_no = await self._ensure_orderbook_for_token(no_token.token_id)

        if ob_yes.spread > self._settings.strategy.max_spread:
            self._pipe.skip_spread += 1
            return False

        ob_check = self._liquidity.check_orderbook(ob_yes, self._settings.strategy.stake_max)
        if not ob_check.passed:
            self._pipe.skip_liquidity += 1
            return False

        self._pipe.evaluated_ok += 1

        market_row = self._ensure_market_row(session, market)

        no_mid_val = ob_no.mid_price if ob_no else None

        session.add(PriceHistoryRow(
            market_id=market_row.id, timestamp=_utcnow(),
            bid=ob_yes.best_bid, ask=ob_yes.best_ask, mid=ob_yes.mid_price,
            spread=ob_yes.spread, volume_24h=market.volume_24h,
            depth_bid=ob_check.bid_depth, depth_ask=ob_check.ask_depth,
            no_mid=no_mid_val,
        ))

        best_signal: SignalOutput | None = None
        for hyp in self._hypotheses:
            try:
                sig = hyp.evaluate(
                    market_id=market.id,
                    question=market_row.question,
                    orderbook=ob_yes,
                )
            except Exception as e:
                logger.debug(f"{hyp.spec.id} error on {market.id}: {e}")
                continue

            if sig.side is None:
                continue

            session.add(SignalRow(
                market_id=market_row.id,
                hypothesis=sig.hypothesis_id,
                model_probability=sig.model_probability,
                market_probability=sig.market_probability,
                edge=sig.edge,
                action_taken=False,
            ))

            if best_signal is None or sig.edge > best_signal.edge:
                best_signal = sig

        if best_signal is None or best_signal.side is None:
            self._pipe.skip_no_signal += 1
            return False

        order_key = f"{market.id}:{best_signal.side}"
        if order_key in self._recent_order_keys:
            self._pipe.skip_duplicate += 1
            return False

        no_ask = ob_no.best_ask if ob_no else None
        decision = self._risk.evaluate_trade(
            p_model=best_signal.model_probability,
            price_ask=ob_yes.best_ask,
            price_bid=ob_yes.best_bid,
            bankroll=self._bankroll,
            open_positions=open_positions,
            event_id=market.event_id,
            fee=self._fee_rate,
            no_ask=no_ask,
        )

        if decision.action != "trade":
            self._pipe.skip_risk += 1
            ev = decision.ev
            if ev is not None and ev.best_edge > self._pipe.skip_risk_max_edge:
                self._pipe.skip_risk_max_edge = ev.best_edge
            return False

        last_signal = (
            session.query(SignalRow)
            .filter(SignalRow.market_id == market_row.id)
            .filter(SignalRow.hypothesis == best_signal.hypothesis_id)
            .order_by(SignalRow.id.desc())
            .first()
        )
        if last_signal:
            last_signal.action_taken = True

        if decision.side == "YES":
            token_id = yes_token.token_id
            entry_price = decision.price
            exec_ob = ob_yes
        else:
            if not no_token or ob_no is None:
                logger.debug(f"NO trade skipped: missing NO token/book for {market.id}")
                return False
            token_id = no_token.token_id
            entry_price = decision.price
            exec_ob = ob_no

        contracts = decision.stake / entry_price if entry_price > 0 else 0

        intent = TradeIntent(
            market_id=str(market_row.id),
            condition_id=market.id,
            token_id=token_id,
            side=decision.side,
            action="BUY",
            price=entry_price,
            stake_usd=decision.stake,
            contracts=contracts,
            hypothesis_id=best_signal.hypothesis_id,
            edge=best_signal.edge,
        )

        fill = await self._broker.execute(intent, exec_ob)
        self._record_fill(session, market_row, intent, fill, decision, best_signal)

        if fill.status in ("FILLED", "PARTIAL", "PAPER_FILL", "POSTED"):
            self._recent_order_keys.add(order_key)

        return fill.filled_contracts > 0

    # ───────────────── Record fill results ─────────────────

    def _record_fill(
        self, session, market_row: MarketRow, intent: TradeIntent,
        fill: FillResult, decision: TradeDecision, signal: SignalOutput,
    ):
        logger.info(
            f"TRADE [{self._broker.mode}]: {intent.side} on "
            f"'{market_row.question[:60]}' | "
            f"stake=${intent.stake_usd:.2f}, {intent.contracts:.2f} contracts "
            f"@ {intent.price:.4f} | fill={fill.status} "
            f"{fill.filled_contracts:.2f} @ {fill.avg_fill_price:.4f} | "
            f"edge={signal.edge:.3f} via {signal.hypothesis_id}"
        )

        session.add(OrderRow(
            market_id=market_row.id,
            exchange_order_id=fill.order_id,
            token_id=intent.token_id,
            side=intent.side,
            price=intent.price,
            size=fill.filled_contracts,
            filled_size=fill.filled_contracts,
            status=fill.status.lower(),
        ))

        if fill.filled_contracts > 0 and fill.status in (
            "FILLED", "PARTIAL", "PAPER_FILL", "POSTED"
        ):
            actual_price = fill.avg_fill_price if fill.avg_fill_price > 0 else intent.price
            session.add(PositionRow(
                market_id=market_row.id,
                token_id=intent.token_id,
                side=intent.side,
                entry_price=actual_price,
                current_price=actual_price,
                size=fill.filled_contracts,
                event_id=market_row.event_id,
                status="open",
            ))
            session.add(AuditRow(
                timestamp=_utcnow(), event_type="trade_opened",
                details=json.dumps({
                    "market": market_row.question[:80],
                    "side": intent.side,
                    "token_id": intent.token_id,
                    "price": actual_price,
                    "contracts": fill.filled_contracts,
                    "stake_usd": round(fill.filled_usd, 2),
                    "slippage": round(fill.slippage, 4),
                    "fees_usd": round(fill.fees_usd, 4),
                    "edge": signal.edge,
                    "hypothesis": signal.hypothesis_id,
                    "mode": self._broker.mode,
                    "order_id": fill.order_id,
                }),
            ))
            if self._settings.alerts.telegram_enabled:
                asyncio.ensure_future(
                    self._alerter.send_trade_opened(
                        market_row.question,
                        intent.side,
                        intent.stake_usd,
                        fill.filled_contracts,
                        actual_price,
                        signal.edge,
                        signal.hypothesis_id,
                    )
                )

    # ═══════════════════ SETTLEMENT ═══════════════════

    async def _poll_settlements(self, session):
        open_positions = (
            session.query(PositionRow).filter(PositionRow.status == "open").all()
        )
        if not open_positions:
            return

        market_ids_checked: set[int] = set()

        for pos in open_positions:
            if pos.market_id in market_ids_checked:
                continue
            market_ids_checked.add(pos.market_id)

            market_row = session.get(MarketRow, pos.market_id)
            if not market_row or market_row.outcome:
                self._settle_position(session, pos, market_row)
                continue

            try:
                api_data = await self._client.get_market_resolution(market_row.polymarket_id)
            except Exception as e:
                logger.debug(f"Resolution check for {market_row.polymarket_id}: {e}")
                continue

            if not api_data:
                continue
            if not api_data.get("resolved", False):
                continue

            outcome = api_data.get("outcome", "")
            status = api_data.get("status", "resolved")

            market_row.outcome = outcome.upper() if outcome else None
            market_row.settled_at = _utcnow()

            existing = (
                session.query(SettlementRow)
                .filter(SettlementRow.market_id == market_row.id)
                .first()
            )
            if not existing:
                session.add(SettlementRow(
                    market_id=market_row.id,
                    status=status,
                    outcome=market_row.outcome,
                    resolved_at=_utcnow(),
                    payout_details=api_data,
                ))

            logger.info(
                f"Resolution detected: '{market_row.question[:50]}' → "
                f"{market_row.outcome} ({status})"
            )

        for pos in open_positions:
            if pos.status != "open":
                continue
            market_row = session.get(MarketRow, pos.market_id)
            self._settle_position(session, pos, market_row)

    def _settle_position(self, session, pos: PositionRow, market_row: MarketRow | None):
        if not market_row or not market_row.outcome:
            return

        settlement = (
            session.query(SettlementRow)
            .filter(SettlementRow.market_id == market_row.id)
            .first()
        )
        status = settlement.status if settlement else "resolved"

        if status == "disputed":
            if pos.status != "disputed":
                pos.status = "disputed"
                logger.warning(f"DISPUTED: '{market_row.question[:50]}'")
                session.add(AuditRow(
                    timestamp=_utcnow(), event_type="settlement_disputed",
                    details=json.dumps({"market_id": market_row.id}),
                ))
            return

        if status == "cancelled":
            pos.pnl = 0
            pos.status = "closed"
            pos.closed_at = _utcnow()
            pos.exit_reason = "settlement_cancelled"
            session.add(AuditRow(
                timestamp=_utcnow(), event_type="settlement_cancelled",
                details=json.dumps({"market_id": market_row.id}),
            ))
            return

        outcome = market_row.outcome
        won = (
            (pos.side == "YES" and outcome == "YES")
            or (pos.side == "NO" and outcome == "NO")
        )

        if won:
            gross = 1.0 - pos.entry_price
            pnl = pos.size * (gross - gross * self._fee_rate)
        else:
            pnl = -(pos.size * pos.entry_price)

        pos.pnl = pnl
        pos.status = "closed"
        pos.closed_at = _utcnow()
        pos.exit_reason = "settlement"
        self._bankroll += pnl
        self._risk.record_pnl(pnl)

        if self._settings.mode == "paper":
            gs = get_or_create_gate_state(session)
            gs.paper_trades_count += 1
            gs.paper_realized_pnl += pnl

        logger.info(
            f"SETTLEMENT: '{market_row.question[:50]}' → {outcome} | "
            f"side={pos.side} | PnL=${pnl:.2f} | bankroll=${self._bankroll:.2f}"
        )
        session.add(AuditRow(
            timestamp=_utcnow(), event_type="settlement_resolved",
            details=json.dumps({
                "market_id": market_row.id, "outcome": outcome,
                "side": pos.side, "pnl": round(pnl, 4),
                "bankroll": round(self._bankroll, 2),
            }),
        ))

        asyncio.ensure_future(self._alerter.send_settlement(
            question=market_row.question[:100],
            outcome=outcome, pnl=pnl, side=pos.side,
        ))

    # ═══════════════════ HELPERS ═══════════════════

    def _ensure_market_row(self, session, market: Market) -> MarketRow:
        row = (
            session.query(MarketRow)
            .filter(MarketRow.polymarket_id == market.id)
            .first()
        )
        if row:
            row.volume_24h = market.volume_24h
            row.active = market.active
            return row

        yes_token = next((t for t in market.tokens if t.outcome.lower() == "yes"), None)
        no_token = next((t for t in market.tokens if t.outcome.lower() == "no"), None)

        row = MarketRow(
            polymarket_id=market.id,
            event_id=market.event_id,
            question=market.question,
            category=market.category,
            resolution_source=market.resolution_source,
            active=market.active,
            volume_24h=market.volume_24h,
            yes_token_id=yes_token.token_id if yes_token else "",
            no_token_id=no_token.token_id if no_token else "",
        )
        session.add(row)
        session.flush()
        return row

    @staticmethod
    def _get_event_id(session, market_id: int) -> str | None:
        row = session.get(MarketRow, market_id)
        return row.event_id if row else None

    def _write_daily_pnl_snapshot(self, session):
        today = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        existing = session.query(PnlLogRow).filter(PnlLogRow.date == today).first()

        closed_today = (
            session.query(PositionRow)
            .filter(PositionRow.status == "closed")
            .filter(PositionRow.closed_at >= today)
            .all()
        )
        realized = sum(p.pnl or 0 for p in closed_today)
        trades = len(closed_today)
        wins = sum(1 for p in closed_today if (p.pnl or 0) > 0)
        hit = wins / trades if trades else 0

        # Unrealized PnL for open positions
        open_pos = session.query(PositionRow).filter(PositionRow.status == "open").all()
        unrealized = sum(
            p.size * ((p.current_price or p.entry_price) - p.entry_price)
            for p in open_pos
        )

        if existing:
            existing.realized_pnl = realized
            existing.unrealized_pnl = unrealized
            existing.bankroll = self._bankroll
            existing.trades_count = trades
            existing.hit_rate = hit
        else:
            session.add(PnlLogRow(
                date=today,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                bankroll=self._bankroll,
                trades_count=trades,
                hit_rate=hit,
            ))

    def _log_cycle_summary(
        self, session, trades: int, exits: int, liquid_n: int = 0,
    ):
        open_count = session.query(PositionRow).filter(PositionRow.status == "open").count()
        ws_status = "connected" if (self._ws and self._ws.is_connected) else "off"
        p = self._pipe
        pipe_s = (
            f"pipeline[{liquid_n} liq]: "
            f"tok={p.skip_tokens} no_ob={p.skip_orderbook} spread={p.skip_spread} "
            f"liq_filt={p.skip_liquidity} ok_ob={p.evaluated_ok} "
            f"no_signal={p.skip_no_signal} risk={p.skip_risk}"
            f"(max_ev_skipped={p.skip_risk_max_edge:.3f}) dup={p.skip_duplicate}"
        )
        logger.info(
            f"Cycle {self._cycle_count} done | "
            f"new_trades={trades} exits={exits} | "
            f"open={open_count} | "
            f"bankroll=${self._bankroll:.2f} | "
            f"ws={ws_status} ws_subs={self._ob_manager.subscribed_count} | "
            f"circuit={self._circuit.state.value} | {pipe_s}"
        )

    def _setup_signal_handlers(self):
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

    async def _shutdown(self):
        logger.info("Shutting down orchestrator...")
        if self._ws:
            await self._ws.close()

        session = get_session(self._db_url)
        try:
            session.add(AuditRow(
                timestamp=_utcnow(), event_type="shutdown",
                details=json.dumps({
                    "cycle_count": self._cycle_count,
                    "bankroll": round(self._bankroll, 2),
                    "mode": self._settings.mode,
                }),
            ))
            session.commit()
        finally:
            session.close()

        cancelled = await self._broker.cancel_all()
        logger.info(f"Cancelled {cancelled} pending orders")
        await self._client.close()
        if self._settings.alerts.telegram_enabled:
            await self._alerter.notify_shutdown(
                self._settings.mode, self._cycle_count, self._bankroll,
            )
        await self._alerter.close()
        logger.info("Orchestrator stopped")
