from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func

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
    ResearchModelArtifactRow,
    ResearchMotifRow,
)
from db.session import get_session
from monitor.stats_service import StatusSnapshot
from research.crypto15m import classify_crypto15m_updown_market

from .utils import horizon_bucket, time_to_resolution_days


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _crypto_asset_bucket(market: MarketRow | None) -> str:
    if market is None:
        return "Other"
    info = classify_crypto15m_updown_market(
        market.question or "",
        category=market.category or "",
        tags=market.tags or [],
    )
    if info.asset in {"BTC", "ETH"}:
        return info.asset
    text = f"{market.question or ''} {market.category or ''} {' '.join(market.tags or [])}".lower()
    if "bitcoin" in text or "btc" in text:
        return "BTC"
    if "ethereum" in text or "ether" in text or "eth" in text:
        return "ETH"
    return "Other"


def _threshold_label(portfolio_key: str | None) -> str:
    key = str(portfolio_key or "")
    if key == "Crypto15m_control":
        return "control"
    for marker in ("t65", "t70", "t75", "t80", "t90", "t95"):
        if marker in key:
            return f"{marker}_ai" if "analyst" in key else marker
    return key or "n/a"


def _active_portfolio_keys(settings: Any | None) -> set[str]:
    if settings is None:
        return set()
    lab = getattr(settings, "lab", None)
    if lab is None:
        return set()

    keys: set[str] = set()
    portfolios = list(getattr(lab, "portfolios", []) or [])
    ab = getattr(lab, "ab_testing", None)
    crypto_cfg = getattr(lab, "crypto15m", None)
    control_suffix = str(getattr(ab, "control_suffix", "control") or "control")
    learned_suffix = str(getattr(ab, "learned_suffix", "learned") or "learned")
    ab_enabled = bool(getattr(ab, "enabled", False))

    for portfolio in portfolios:
        key = str(getattr(portfolio, "key", "") or "").strip()
        if not key:
            continue
        track = str(getattr(portfolio, "track", "") or "")
        if not ab_enabled:
            keys.add(key)
            continue
        if track != "crypto15m" or crypto_cfg is None:
            keys.add(f"{key}_{control_suffix}")
            keys.add(f"{key}_{learned_suffix}")
            continue

        keys.add(f"{key}_{control_suffix}")
        ai_cfg = getattr(crypto_cfg, "ai_analyst", None)
        ai_enabled = bool(getattr(ai_cfg, "enabled", False))
        analyst_only_below = float(getattr(ai_cfg, "analyst_only_below_confidence", 0.0) or 0.0)
        for threshold in list(getattr(crypto_cfg, "ab_thresholds", []) or []):
            threshold_value = float(threshold)
            suffix = f"t{int(round(threshold_value * 100)):02d}"
            create_raw_learned = not (
                ai_enabled
                and analyst_only_below > 0.0
                and threshold_value < analyst_only_below
            )
            if create_raw_learned:
                keys.add(f"{key}_{suffix}_{learned_suffix}")
            if ai_enabled:
                keys.add(f"{key}_{suffix}_analyst")
    return keys


class LabStatsService:
    def __init__(
        self,
        db_url: str,
        initial_bankroll: float,
        mode: str = "shadow_maker",
        settings: Any | None = None,
    ):
        self._db_url = db_url
        self._initial = initial_bankroll
        self._mode = mode
        self._settings = settings

    def _session(self):
        return get_session(self._db_url)

    def _portfolio_rows(self, session) -> list[LabPortfolioRow]:
        rows = session.query(LabPortfolioRow).order_by(LabPortfolioRow.key.asc()).all()
        active_keys = _active_portfolio_keys(self._settings)
        if active_keys:
            active_rows = [row for row in rows if row.key in active_keys]
            if active_rows:
                return active_rows
        ab_rows = []
        for row in rows:
            portfolio_meta = (row.settings_json or {}).get("portfolio") or {}
            ab_group = str(portfolio_meta.get("ab_group") or "")
            if (
                ab_group == "control"
                or ab_group.startswith("learned_")
                or ab_group.startswith("analyst_")
                or row.key.endswith("_control")
                or row.key.endswith("_learned")
                or row.key.endswith("_analyst")
            ):
                ab_rows.append(row)
        return ab_rows or rows

    def _portfolio_meta(self, row: LabPortfolioRow) -> dict[str, Any]:
        settings_json = row.settings_json or {}
        portfolio_meta = settings_json.get("portfolio") or {}
        return {
            "pack": portfolio_meta.get("pack") or row.key.split("_")[-1],
            "track": portfolio_meta.get("track") or "control",
            "ab_group": portfolio_meta.get("ab_group") or "single",
            "base_key": portfolio_meta.get("base_key") or row.key,
            "use_learned_gate": bool(portfolio_meta.get("use_learned_gate", True)),
            "hypotheses": portfolio_meta.get("hypotheses") or [],
            "combo_mode": bool(portfolio_meta.get("combo_mode")),
            "max_horizon_days": portfolio_meta.get("max_horizon_days"),
            "min_quality_score": portfolio_meta.get("min_quality_score"),
        }

    def _runtime_row(self, session) -> LabRuntimeStatusRow | None:
        return session.query(LabRuntimeStatusRow).order_by(LabRuntimeStatusRow.id.asc()).first()

    def _latest_ws_metric(self, session) -> LabWsMetricRow | None:
        return session.query(LabWsMetricRow).order_by(LabWsMetricRow.timestamp.desc()).first()

    def _latest_artifact(self, session) -> ResearchModelArtifactRow | None:
        return (
            session.query(ResearchModelArtifactRow)
            .order_by(ResearchModelArtifactRow.created_at.desc())
            .first()
        )

    def _portfolio_summary(self, session, row: LabPortfolioRow) -> dict[str, Any]:
        meta = self._portfolio_meta(row)
        orders = session.query(LabOrderRow).filter(LabOrderRow.portfolio_id == row.id).all()
        fills = session.query(LabFillRow).filter(LabFillRow.portfolio_id == row.id).all()
        open_positions = (
            session.query(LabPositionRow)
            .filter(LabPositionRow.portfolio_id == row.id)
            .filter(LabPositionRow.status == "open")
            .all()
        )
        closed_positions = (
            session.query(LabPositionRow)
            .filter(LabPositionRow.portfolio_id == row.id)
            .filter(LabPositionRow.status == "closed")
            .all()
        )
        latest_equity = (
            session.query(LabEquityPointRow)
            .filter(LabEquityPointRow.portfolio_id == row.id)
            .order_by(LabEquityPointRow.timestamp.desc())
            .first()
        )

        realized_pnl = sum((position.pnl or position.realized_pnl or 0.0) for position in closed_positions)
        unrealized_pnl = sum(
            position.size * (
                (position.current_price if position.current_price is not None else position.entry_price)
                - position.entry_price
            )
            for position in open_positions
        )
        bankroll = float(latest_equity.bankroll) if latest_equity is not None else row.initial_bankroll + realized_pnl
        equity = float(latest_equity.equity) if latest_equity is not None else bankroll + unrealized_pnl
        drawdown_pct = float(latest_equity.drawdown_pct) if latest_equity is not None else 0.0

        order_map = {order.id: order for order in orders}
        total_size = sum(order.size_total for order in orders)
        total_filled = sum(order.filled_size for order in orders)
        fill_rate = (total_filled / total_size) if total_size else 0.0
        exit_fills = [fill for fill in fills if (order_map.get(fill.order_id) and order_map[fill.order_id].action == "SELL")]
        forced_taker_exit_count = sum(1 for fill in exit_fills if fill.fill_type == "forced_taker_exit")
        forced_taker_exit_ratio = (forced_taker_exit_count / len(exit_fills)) if exit_fills else 0.0

        wins = sum(1 for position in closed_positions if (position.pnl or 0.0) > 0)
        losses = sum(1 for position in closed_positions if (position.pnl or 0.0) <= 0)
        closed_trades = len(closed_positions)
        hit_rate = (wins / closed_trades) if closed_trades else 0.0
        expectancy = (realized_pnl / closed_trades) if closed_trades else 0.0

        hold_hours: list[float] = []
        for position in closed_positions:
            opened_at = _as_utc(position.opened_at)
            closed_at = _as_utc(position.closed_at)
            if opened_at and closed_at:
                hold_hours.append((closed_at - opened_at).total_seconds() / 3600.0)
        avg_hold_hours = (sum(hold_hours) / len(hold_hours)) if hold_hours else 0.0

        slippages: list[float] = []
        for fill in fills:
            order = order_map.get(fill.order_id)
            if order is None:
                continue
            if order.action == "BUY":
                slippages.append(max(0.0, fill.price - order.price))
            else:
                slippages.append(max(0.0, order.price - fill.price))
        avg_slippage = (sum(slippages) / len(slippages)) if slippages else 0.0

        exposure_by_category: dict[str, float] = defaultdict(float)
        exposure_by_horizon: dict[str, float] = defaultdict(float)
        for position in open_positions:
            market = session.get(MarketRow, position.market_id)
            notional = position.size * position.entry_price
            category = market.category if market and market.category else "uncategorized"
            exposure_by_category[category] += notional
            days = time_to_resolution_days(market.end_date.isoformat() if market and market.end_date else None) if market else None
            exposure_by_horizon[horizon_bucket(days)] += notional

        today = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        closed_today = [
            position for position in closed_positions
            if _as_utc(position.closed_at) and _as_utc(position.closed_at) >= today
        ]
        trades_today = len(closed_today)
        realized_today = sum((position.pnl or 0.0) for position in closed_today)
        candidate_count_24h = (
            session.query(LabDecisionAuditRow)
            .filter(LabDecisionAuditRow.portfolio_id == row.id)
            .filter(LabDecisionAuditRow.timestamp >= _utcnow() - timedelta(hours=24))
            .filter(LabDecisionAuditRow.decision == "candidate")
            .count()
        )
        reject_count_24h = (
            session.query(LabDecisionAuditRow)
            .filter(LabDecisionAuditRow.portfolio_id == row.id)
            .filter(LabDecisionAuditRow.timestamp >= _utcnow() - timedelta(hours=24))
            .filter(LabDecisionAuditRow.decision == "rejected")
            .count()
        )
        audits_24h = (
            session.query(LabDecisionAuditRow)
            .filter(LabDecisionAuditRow.portfolio_id == row.id)
            .filter(LabDecisionAuditRow.timestamp >= _utcnow() - timedelta(hours=24))
            .all()
        )
        markets_seen_24h = len({audit.market_id for audit in audits_24h})
        accepted_count_24h = sum(1 for audit in audits_24h if audit.decision in {"accepted", "entered"})
        reject_stages_24h: Counter[str] = Counter()
        for audit in audits_24h:
            if audit.decision != "rejected":
                continue
            audit_meta = audit.meta_json or {}
            stage = str(audit_meta.get("stage") or "unknown")
            reason = str(audit_meta.get("reason") or "")
            reject_stages_24h[f"{stage}:{reason}" if reason else stage] += 1

        return {
            "key": row.key,
            "pack": meta["pack"],
            "track": meta["track"],
            "ab_group": meta["ab_group"],
            "base_key": meta["base_key"],
            "use_learned_gate": meta["use_learned_gate"],
            "hypotheses": meta["hypotheses"],
            "combo_mode": meta["combo_mode"],
            "max_horizon_days": meta["max_horizon_days"],
            "min_quality_score": meta["min_quality_score"],
            "initial_bankroll": row.initial_bankroll,
            "bankroll": bankroll,
            "realized_pnl": realized_pnl,
            "realized_today": realized_today,
            "unrealized_pnl": unrealized_pnl,
            "equity": equity,
            "drawdown_pct": drawdown_pct,
            "open_positions": len(open_positions),
            "closed_trades": closed_trades,
            "trades_today": trades_today,
            "wins": wins,
            "losses": losses,
            "hit_rate": hit_rate,
            "expectancy_per_trade": expectancy,
            "order_total_size": total_size,
            "order_filled_size": total_filled,
            "fill_rate": fill_rate,
            "avg_slippage_vs_quote": avg_slippage,
            "avg_hold_hours": avg_hold_hours,
            "forced_exit_count": sum(1 for position in closed_positions if position.forced_exit),
            "exit_fill_count": len(exit_fills),
            "forced_taker_exit_count": forced_taker_exit_count,
            "forced_taker_exit_ratio": forced_taker_exit_ratio,
            "exposure_by_category": dict(exposure_by_category),
            "exposure_by_horizon_bucket": dict(exposure_by_horizon),
            "candidate_count_24h": candidate_count_24h,
            "reject_count_24h": reject_count_24h,
            "markets_seen_24h": markets_seen_24h,
            "signals_24h": candidate_count_24h,
            "accepted_count_24h": accepted_count_24h,
            "fills_count": len(fills),
            "reject_stages_24h": dict(reject_stages_24h),
        }

    def portfolio_summaries(self) -> list[dict[str, Any]]:
        session = self._session()
        try:
            return [self._portfolio_summary(session, row) for row in self._portfolio_rows(session)]
        finally:
            session.close()

    def acceptance_gates(self) -> list[dict[str, Any]]:
        gates: list[dict[str, Any]] = []
        for summary in self.portfolio_summaries():
            eligible = (
                summary["closed_trades"] >= 20
                and summary["fill_rate"] >= 0.10
                and summary["drawdown_pct"] <= 0.10
            )
            gates.append({
                "key": summary["key"],
                "eligible": eligible,
                "closed_trades": summary["closed_trades"],
                "fill_rate": summary["fill_rate"],
                "drawdown_pct": summary["drawdown_pct"],
                "realized_pnl": summary["realized_pnl"],
                "expectancy_per_trade": summary["expectancy_per_trade"],
            })
        return gates

    def daily_summaries(self, portfolio_key: str | None = None) -> list[dict[str, Any]]:
        session = self._session()
        try:
            portfolios = self._portfolio_rows(session)
            key_to_id = {row.key: row.id for row in portfolios}
            daily: dict[tuple[str, str], dict[str, Any]] = {}

            query = session.query(LabPositionRow).filter(LabPositionRow.status == "closed")
            if portfolio_key:
                portfolio_id = key_to_id.get(portfolio_key)
                if portfolio_id is None:
                    return []
                query = query.filter(LabPositionRow.portfolio_id == portfolio_id)

            for position in query.all():
                closed_at = _as_utc(position.closed_at)
                if closed_at is None:
                    continue
                row = next((portfolio for portfolio in portfolios if portfolio.id == position.portfolio_id), None)
                if row is None:
                    continue
                day_key = closed_at.date().isoformat()
                bucket = daily.setdefault((row.key, day_key), {
                    "portfolio_key": row.key,
                    "date": day_key,
                    "realized_pnl": 0.0,
                    "trades": 0,
                    "wins": 0,
                })
                bucket["realized_pnl"] += float(position.pnl or 0.0)
                bucket["trades"] += 1
                if (position.pnl or 0.0) > 0:
                    bucket["wins"] += 1

            out = list(daily.values())
            for item in out:
                item["hit_rate"] = (item["wins"] / item["trades"]) if item["trades"] else 0.0
            out.sort(key=lambda item: (item["date"], item["portfolio_key"]), reverse=True)
            return out
        finally:
            session.close()

    def overview(self) -> dict[str, Any]:
        summaries = self.portfolio_summaries()
        gates = {item["key"]: item for item in self.acceptance_gates()}
        for summary in summaries:
            summary["acceptance"] = gates.get(summary["key"], {})

        winner = None
        if summaries:
            winner = sorted(
                summaries,
                key=lambda item: (
                    1 if item["acceptance"].get("eligible") else 0,
                    item["realized_pnl"],
                    -item["drawdown_pct"],
                    item["expectancy_per_trade"],
                ),
                reverse=True,
            )[0]["key"]

        aggregate = {
            "bankroll": sum(item["bankroll"] for item in summaries),
            "equity": sum(item["equity"] for item in summaries),
            "realized_pnl": sum(item["realized_pnl"] for item in summaries),
            "unrealized_pnl": sum(item["unrealized_pnl"] for item in summaries),
            "open_positions": sum(item["open_positions"] for item in summaries),
            "closed_trades": sum(item["closed_trades"] for item in summaries),
            "candidates_24h": sum(item["candidate_count_24h"] for item in summaries),
            "rejections_24h": sum(item["reject_count_24h"] for item in summaries),
        }
        track_aggregates: dict[str, dict[str, float]] = defaultdict(lambda: {
            "equity": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "open_positions": 0.0,
            "closed_trades": 0.0,
        })
        ab_group_aggregates: dict[str, dict[str, float]] = defaultdict(lambda: {
            "initial_bankroll": 0.0,
            "bankroll": 0.0,
            "equity": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "open_positions": 0.0,
            "closed_trades": 0.0,
            "candidate_count_24h": 0.0,
            "reject_count_24h": 0.0,
            "order_total_size": 0.0,
            "order_filled_size": 0.0,
            "exit_fill_count": 0.0,
            "forced_taker_exit_count": 0.0,
        })
        for item in summaries:
            bucket = track_aggregates[item["track"]]
            bucket["equity"] += item["equity"]
            bucket["realized_pnl"] += item["realized_pnl"]
            bucket["unrealized_pnl"] += item["unrealized_pnl"]
            bucket["open_positions"] += item["open_positions"]
            bucket["closed_trades"] += item["closed_trades"]
            ab_bucket = ab_group_aggregates[item["ab_group"]]
            ab_bucket["initial_bankroll"] += item["initial_bankroll"]
            ab_bucket["bankroll"] += item["bankroll"]
            ab_bucket["equity"] += item["equity"]
            ab_bucket["realized_pnl"] += item["realized_pnl"]
            ab_bucket["unrealized_pnl"] += item["unrealized_pnl"]
            ab_bucket["open_positions"] += item["open_positions"]
            ab_bucket["closed_trades"] += item["closed_trades"]
            ab_bucket["candidate_count_24h"] += item["candidate_count_24h"]
            ab_bucket["reject_count_24h"] += item["reject_count_24h"]
            ab_bucket["order_total_size"] += item["order_total_size"]
            ab_bucket["order_filled_size"] += item["order_filled_size"]
            ab_bucket["exit_fill_count"] += item["exit_fill_count"]
            ab_bucket["forced_taker_exit_count"] += item["forced_taker_exit_count"]
        for bucket in ab_group_aggregates.values():
            total_size = float(bucket.get("order_total_size") or 0.0)
            exit_fill_count = float(bucket.get("exit_fill_count") or 0.0)
            bucket["fill_rate"] = (float(bucket.get("order_filled_size") or 0.0) / total_size) if total_size else 0.0
            bucket["forced_taker_exit_ratio"] = (
                float(bucket.get("forced_taker_exit_count") or 0.0) / exit_fill_count
            ) if exit_fill_count else 0.0

        session = self._session()
        try:
            runtime_row = self._runtime_row(session)
            ws_metric = self._latest_ws_metric(session)
            artifact = self._latest_artifact(session)
            runtime = {
                "mode": runtime_row.mode if runtime_row else self._mode,
                "started_at": runtime_row.started_at.isoformat() if runtime_row and runtime_row.started_at else None,
                "last_cycle_ts": runtime_row.last_cycle_ts.isoformat() if runtime_row and runtime_row.last_cycle_ts else None,
                "last_cycle_ok": runtime_row.last_cycle_ok if runtime_row else True,
                "last_cycle_error": runtime_row.last_cycle_error if runtime_row else None,
                "ws_connected": runtime_row.ws_connected if runtime_row else False,
                "markets_fetched_last": runtime_row.markets_fetched_last if runtime_row else 0,
                "eligible_markets_last": runtime_row.eligible_markets_last if runtime_row else 0,
                "subscribed_tokens_last": runtime_row.subscribed_tokens_last if runtime_row else 0,
                "ws": {
                    "health_score": ws_metric.health_score if ws_metric else 0.0,
                    "gap_count": ws_metric.gap_count if ws_metric else 0,
                    "max_gap_sec": ws_metric.max_gap_sec if ws_metric else 0.0,
                    "last_message_age_sec": ws_metric.last_message_age_sec if ws_metric else 0.0,
                    "messages_per_minute": ws_metric.messages_per_minute if ws_metric else 0.0,
                    "entries_frozen": ws_metric.entries_frozen if ws_metric else False,
                    "forced_taker_exit_ratio": ws_metric.forced_taker_exit_ratio if ws_metric else 0.0,
                    "maker_fill_ratio": ws_metric.maker_fill_ratio if ws_metric else 0.0,
                },
                "learning": {
                    "artifact_key": artifact.artifact_key if artifact else None,
                    "accepted": bool(artifact.accepted) if artifact else False,
                    "enabled": bool(artifact.enabled) if artifact else False,
                    "high_conf_accuracy": artifact.high_conf_accuracy if artifact else 0.0,
                    "high_conf_net_ev": artifact.high_conf_net_ev if artifact else 0.0,
                    "calibration_error": artifact.calibration_error if artifact else 0.0,
                    "training_fresh_until": artifact.training_fresh_until.isoformat() if artifact and artifact.training_fresh_until else None,
                    "verdict": (artifact.metrics_json or {}).get("verdict") if artifact else None,
                },
            }
        finally:
            session.close()

        verdict = self.verdict(summaries=summaries, ab_groups=dict(ab_group_aggregates), runtime=runtime)

        return {
            "generated_at": _utcnow().isoformat(),
            "winner_key": winner,
            "aggregate": aggregate,
            "tracks": dict(track_aggregates),
            "ab_groups": dict(ab_group_aggregates),
            "verdict": verdict,
            "runtime": runtime,
            "portfolios": summaries,
            "daily": self.daily_summaries(),
        }

    def portfolio_detail(self, key: str) -> dict[str, Any]:
        session = self._session()
        try:
            row = (
                session.query(LabPortfolioRow)
                .filter(LabPortfolioRow.key == key)
                .first()
            )
            if row is None:
                raise KeyError(key)

            summary = self._portfolio_summary(session, row)
            equity_curve = [
                {
                    "timestamp": point.timestamp.isoformat(),
                    "bankroll": point.bankroll,
                    "realized_pnl": point.realized_pnl,
                    "unrealized_pnl": point.unrealized_pnl,
                    "equity": point.equity,
                    "drawdown_pct": point.drawdown_pct,
                }
                for point in (
                    session.query(LabEquityPointRow)
                    .filter(LabEquityPointRow.portfolio_id == row.id)
                    .order_by(LabEquityPointRow.timestamp.asc())
                    .limit(5000)
                    .all()
                )
            ]

            open_positions = []
            for position in (
                session.query(LabPositionRow)
                .filter(LabPositionRow.portfolio_id == row.id)
                .filter(LabPositionRow.status == "open")
                .all()
            ):
                market = session.get(MarketRow, position.market_id)
                open_positions.append({
                    "question": market.question if market else "",
                    "category": market.category if market else "",
                    "side": position.side,
                    "entry_price": position.entry_price,
                    "current_price": position.current_price,
                    "size": position.size,
                    "unrealized_pnl": position.size * (
                        (position.current_price if position.current_price is not None else position.entry_price)
                        - position.entry_price
                    ),
                })

            recent_fills = []
            order_rows = {
                order.id: order
                for order in session.query(LabOrderRow).filter(LabOrderRow.portfolio_id == row.id).all()
            }
            for fill in (
                session.query(LabFillRow)
                .filter(LabFillRow.portfolio_id == row.id)
                .order_by(LabFillRow.timestamp.desc())
                .limit(100)
                .all()
            ):
                order = order_rows.get(fill.order_id)
                recent_fills.append({
                    "timestamp": fill.timestamp.isoformat(),
                    "side": fill.side,
                    "price": fill.price,
                    "size": fill.size,
                    "fill_type": fill.fill_type,
                    "order_kind": order.order_kind if order else "",
                    "action": order.action if order else "",
                    "quoted_price": order.price if order else None,
                })

            return {
                "summary": summary,
                "equity_curve": equity_curve,
                "open_positions": open_positions,
                "recent_fills": recent_fills,
                "daily": self.daily_summaries(portfolio_key=key),
            }
        finally:
            session.close()

    def rejections(self, limit: int = 25) -> list[dict[str, Any]]:
        session = self._session()
        try:
            rows = (
                session.query(LabDecisionAuditRow)
                .filter(LabDecisionAuditRow.decision == "rejected")
                .order_by(LabDecisionAuditRow.timestamp.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "timestamp": row.timestamp.isoformat(),
                    "portfolio_key": row.portfolio_key,
                    "track": row.track,
                    "question": row.question_snapshot,
                    "side": row.side,
                    "hypothesis": row.hypothesis,
                    "reasons": row.reasons_json or [],
                    "meta": row.meta_json or {},
                    "quality_score": row.quality_score,
                    "expected_net_edge": row.expected_net_edge,
                }
                for row in rows
            ]
        finally:
            session.close()

    def candidates(self, limit: int = 25) -> list[dict[str, Any]]:
        session = self._session()
        try:
            rows = (
                session.query(LabDecisionAuditRow)
                .filter(LabDecisionAuditRow.decision.in_(("candidate", "accepted", "entered")))
                .order_by(LabDecisionAuditRow.timestamp.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "timestamp": row.timestamp.isoformat(),
                    "decision": row.decision,
                    "portfolio_key": row.portfolio_key,
                    "track": row.track,
                    "question": row.question_snapshot,
                    "side": row.side,
                    "hypothesis": row.hypothesis,
                    "edge": row.edge,
                    "quality_score": row.quality_score,
                    "reasons": row.reasons_json or [],
                }
                for row in rows
            ]
        finally:
            session.close()

    def crypto15m_snapshot(self) -> dict[str, Any]:
        session = self._session()
        try:
            rows = (
                session.query(LabPortfolioRow)
                .filter(LabPortfolioRow.key.like("Crypto15m%"))
                .order_by(LabPortfolioRow.key.asc())
                .all()
            )
            active_keys = _active_portfolio_keys(self._settings)
            if active_keys:
                active_rows = [row for row in rows if row.key in active_keys]
                if active_rows:
                    rows = active_rows
            ids = [row.id for row in rows]
            since = _utcnow() - timedelta(hours=24)
            decisions = []
            if ids:
                decisions = (
                    session.query(LabDecisionAuditRow)
                    .filter(LabDecisionAuditRow.portfolio_id.in_(ids))
                    .filter(LabDecisionAuditRow.timestamp >= since)
                    .all()
                )
            reason_counts: Counter[str] = Counter()
            latest_ohlcv_age = None
            latest_ohlcv_exchange = ""
            latest_model_meta: dict[str, Any] = {}
            latest_decision_ts = None
            latest_decision_reason = ""
            latest_decision_stage = ""
            for audit in decisions:
                meta = audit.meta_json or {}
                reason = str(meta.get("reason") or meta.get("stage") or audit.decision or "unknown")
                if audit.decision == "rejected":
                    reason_counts[reason] += 1
                if latest_decision_ts is None or (_as_utc(audit.timestamp) or audit.timestamp) > latest_decision_ts:
                    latest_decision_ts = _as_utc(audit.timestamp) or audit.timestamp
                    latest_decision_reason = reason
                    latest_decision_stage = str(meta.get("stage") or audit.decision or "unknown")
                if "crypto_ohlcv_age_sec" in meta:
                    latest_ohlcv_age = float(meta.get("crypto_ohlcv_age_sec") or 0.0)
                    latest_ohlcv_exchange = str(meta.get("crypto_ohlcv_exchange") or "")
                if "model_yes_probability" in meta or "expected_net_ev" in meta:
                    latest_model_meta = dict(meta)

            market_cache: dict[int, MarketRow | None] = {}

            def market_for(market_id: int | None) -> MarketRow | None:
                if not market_id:
                    return None
                if market_id not in market_cache:
                    market_cache[market_id] = session.get(MarketRow, market_id)
                return market_cache[market_id]

            asset_stats: dict[str, dict[str, Any]] = {
                key: {
                    "realized_pnl": 0.0,
                    "unrealized_pnl": 0.0,
                    "orders": 0,
                    "order_size": 0.0,
                    "order_filled_size": 0.0,
                    "fills": 0,
                    "fill_notional": 0.0,
                    "open_positions": 0,
                    "closed_positions": 0,
                    "best_threshold": "n/a",
                    "best_threshold_realized_pnl": 0.0,
                    "last_reject_reason": "",
                }
                for key in ("BTC", "ETH", "Other")
            }
            threshold_rows: dict[str, dict[str, Any]] = {}

            if ids:
                portfolio_by_id = {row.id: row.key for row in rows}
                pnl_by_asset_threshold: dict[tuple[str, str], float] = defaultdict(float)
                order_aggregates = (
                    session.query(
                        LabOrderRow.market_id,
                        func.count(LabOrderRow.id),
                        func.sum(LabOrderRow.size_total),
                        func.sum(LabOrderRow.filled_size),
                    )
                    .filter(LabOrderRow.portfolio_id.in_(ids))
                    .group_by(LabOrderRow.market_id)
                    .all()
                )
                for market_id, count, size_total, filled_size in order_aggregates:
                    asset = _crypto_asset_bucket(market_for(market_id))
                    bucket = asset_stats[asset]
                    bucket["orders"] += int(count or 0)
                    bucket["order_size"] += float(size_total or 0.0)
                    bucket["order_filled_size"] += float(filled_size or 0.0)

                fill_aggregates = (
                    session.query(
                        LabFillRow.market_id,
                        func.count(LabFillRow.id),
                        func.sum(LabFillRow.notional),
                    )
                    .filter(LabFillRow.portfolio_id.in_(ids))
                    .group_by(LabFillRow.market_id)
                    .all()
                )
                for market_id, count, notional in fill_aggregates:
                    asset = _crypto_asset_bucket(market_for(market_id))
                    bucket = asset_stats[asset]
                    bucket["fills"] += int(count or 0)
                    bucket["fill_notional"] += float(notional or 0.0)

                position_rows = (
                    session.query(LabPositionRow)
                    .filter(LabPositionRow.portfolio_id.in_(ids))
                    .all()
                )
                for position in position_rows:
                    asset = _crypto_asset_bucket(market_for(position.market_id))
                    threshold = _threshold_label(portfolio_by_id.get(position.portfolio_id))
                    realized = float(position.pnl if position.pnl is not None else position.realized_pnl or 0.0)
                    unrealized = float(position.realized_pnl or 0.0) + (
                        position.size
                        * (
                            (position.current_price if position.current_price is not None else position.entry_price)
                            - position.entry_price
                        )
                    )
                    if position.status == "open":
                        asset_stats[asset]["open_positions"] += 1
                        asset_stats[asset]["unrealized_pnl"] += unrealized
                    else:
                        asset_stats[asset]["closed_positions"] += 1
                        asset_stats[asset]["realized_pnl"] += realized
                        pnl_by_asset_threshold[(asset, threshold)] += realized

                for (asset, threshold), pnl in pnl_by_asset_threshold.items():
                    bucket = asset_stats[asset]
                    if bucket["best_threshold"] == "n/a" or pnl > float(bucket["best_threshold_realized_pnl"] or 0.0):
                        bucket["best_threshold"] = threshold
                        bucket["best_threshold_realized_pnl"] = pnl

                recent_rejects = (
                    session.query(LabDecisionAuditRow)
                    .filter(LabDecisionAuditRow.portfolio_id.in_(ids))
                    .filter(LabDecisionAuditRow.timestamp >= since)
                    .filter(LabDecisionAuditRow.decision == "rejected")
                    .order_by(LabDecisionAuditRow.timestamp.desc())
                    .limit(500)
                    .all()
                )
                for audit in recent_rejects:
                    asset = _crypto_asset_bucket(market_for(audit.market_id))
                    if asset_stats[asset]["last_reject_reason"]:
                        continue
                    meta = audit.meta_json or {}
                    reason = str(meta.get("reason") or meta.get("stage") or ",".join(audit.reasons_json or []) or "unknown")
                    asset_stats[asset]["last_reject_reason"] = reason

            orders_count = (
                session.query(LabOrderRow)
                .filter(LabOrderRow.portfolio_id.in_(ids))
                .count()
                if ids else 0
            )
            fills_count = (
                session.query(LabFillRow)
                .filter(LabFillRow.portfolio_id.in_(ids))
                .count()
                if ids else 0
            )
            latest_ws = self._latest_ws_metric(session)
        finally:
            session.close()

        summaries = [item for item in self.portfolio_summaries() if item["key"].startswith("Crypto15m")]
        best = max(summaries, key=lambda item: item["equity"], default=None)
        for item in summaries:
            label = _threshold_label(str(item.get("key") or ""))
            threshold_rows[label] = {
                "key": item.get("key"),
                "equity": float(item.get("equity") or 0.0),
                "realized_pnl": float(item.get("realized_pnl") or 0.0),
                "unrealized_pnl": float(item.get("unrealized_pnl") or 0.0),
                "fills": int(item.get("fills_count") or 0),
                "orders_filled_size": float(item.get("order_filled_size") or 0.0),
                "fill_rate": float(item.get("fill_rate") or 0.0),
                "candidates_24h": int(item.get("candidate_count_24h") or 0),
                "accepted_24h": int(item.get("accepted_count_24h") or 0),
                "rejects_24h": int(item.get("reject_count_24h") or 0),
            }
        for bucket in asset_stats.values():
            size = float(bucket.get("order_size") or 0.0)
            bucket["fill_rate"] = (float(bucket.get("order_filled_size") or 0.0) / size) if size else 0.0
        manifest = self._read_crypto15m_manifest()
        crypto_cfg = self._settings.lab.crypto15m if self._settings is not None else None
        active_thresholds = []
        trade_assets = []
        if crypto_cfg is not None:
            if self._settings is not None and getattr(self._settings.lab.ab_testing, "enabled", False):
                active_thresholds = [f"t{int(round(float(value) * 100)):02d}" for value in crypto_cfg.ab_thresholds]
            else:
                active_thresholds = [f"t{int(round(float(crypto_cfg.min_confidence) * 100)):02d}"]
            trade_assets = [str(asset).upper() for asset in crypto_cfg.trade_assets]
        return {
            "manifest": manifest,
            "portfolios": summaries,
            "best_portfolio": best,
            "assets": asset_stats,
            "thresholds": threshold_rows,
            "active_thresholds": active_thresholds,
            "trade_assets": trade_assets,
            "orders_count": orders_count,
            "fills_count": fills_count,
            "candidate_count_24h": sum(item.get("candidate_count_24h", 0) for item in summaries),
            "accepted_count_24h": sum(item.get("accepted_count_24h", 0) for item in summaries),
            "reject_count_24h": sum(item.get("reject_count_24h", 0) for item in summaries),
            "reject_reasons_24h": dict(reason_counts.most_common(8)),
            "top_reject_reason": next(iter(dict(reason_counts.most_common(1)).keys()), ""),
            "last_decision_ts": latest_decision_ts.isoformat() if latest_decision_ts else None,
            "last_decision_reason": latest_decision_reason,
            "last_decision_stage": latest_decision_stage,
            "latest_ohlcv_age_sec": latest_ohlcv_age,
            "latest_ohlcv_exchange": latest_ohlcv_exchange,
            "latest_model_meta": latest_model_meta,
            "ws_connected": bool(latest_ws.connected) if latest_ws else False,
            "ws_health_score": float(latest_ws.health_score) if latest_ws else 0.0,
            "ws_subscribed_tokens": int(latest_ws.subscribed_tokens) if latest_ws else 0,
        }

    def _read_crypto15m_manifest(self) -> dict[str, Any]:
        settings = self._settings
        if settings is None:
            return {}
        raw_path = str(getattr(settings.strategy.crypto15m_model, "artifact_path", "") or "")
        if not raw_path:
            return {}
        path = Path(raw_path)
        if not path.exists() and "\\" in raw_path:
            path = Path(raw_path.replace("\\", "/"))
        if not path.exists():
            return {"path": raw_path, "exists": False}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["path"] = str(path)
            data["exists"] = True
            model_path_raw = str(data.get("model_path") or "")
            model_path = Path(model_path_raw)
            if not model_path.exists() and "\\" in model_path_raw:
                model_path = Path(model_path_raw.replace("\\", "/"))
            data["model_exists"] = model_path.exists()
            return data
        except Exception as exc:
            return {"path": str(path), "exists": True, "error": str(exc)}

    def portfolio_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "key": item["key"],
                "track": item["track"],
                "ab_group": item["ab_group"],
                "base_key": item["base_key"],
                "pack": item["pack"],
                "hypotheses": item["hypotheses"],
                "min_quality_score": item["min_quality_score"],
                "candidate_count_24h": item["candidate_count_24h"],
                "reject_count_24h": item["reject_count_24h"],
                "markets_seen_24h": item.get("markets_seen_24h", 0),
                "signals_24h": item.get("signals_24h", 0),
                "accepted_count_24h": item.get("accepted_count_24h", 0),
                "fills_count": item.get("fills_count", 0),
                "reject_stages_24h": item.get("reject_stages_24h", {}),
                "realized_pnl": item["realized_pnl"],
                "closed_trades": item["closed_trades"],
            }
            for item in self.portfolio_summaries()
        ]

    def motifs(self, limit: int = 10) -> list[dict[str, Any]]:
        session = self._session()
        try:
            rows = (
                session.query(ResearchMotifRow)
                .order_by(ResearchMotifRow.confidence_score.desc(), ResearchMotifRow.sample_size.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "artifact_key": row.artifact_key,
                    "motif_key": row.motif_key,
                    "sample_size": row.sample_size,
                    "hit_rate": row.hit_rate,
                    "expected_value": row.expected_value,
                    "confidence_score": row.confidence_score,
                }
                for row in rows
            ]
        finally:
            session.close()

    def latest_learning_artifact(self) -> dict[str, Any] | None:
        session = self._session()
        try:
            row = self._latest_artifact(session)
            if row is None:
                return None
            return {
                "artifact_key": row.artifact_key,
                "accepted": bool(row.accepted),
                "enabled": bool(row.enabled),
                "artifact_path": row.artifact_path,
                "manifest_path": row.manifest_path,
                "high_conf_accuracy": row.high_conf_accuracy,
                "high_conf_net_ev": row.high_conf_net_ev,
                "calibration_error": row.calibration_error,
                "training_fresh_until": row.training_fresh_until.isoformat() if row.training_fresh_until else None,
                "holdouts": row.holdout_summary_json or [],
                "verdict": (row.metrics_json or {}).get("verdict"),
            }
        finally:
            session.close()

    def verdict(
        self,
        *,
        summaries: list[dict[str, Any]] | None = None,
        ab_groups: dict[str, dict[str, Any]] | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        summaries = summaries if summaries is not None else self.portfolio_summaries()
        if ab_groups is None:
            ab_groups = defaultdict(lambda: {
                "initial_bankroll": 0.0,
                "equity": 0.0,
                "realized_pnl": 0.0,
                "closed_trades": 0.0,
            })
            for item in summaries:
                bucket = ab_groups[item["ab_group"]]
                bucket["initial_bankroll"] += item["initial_bankroll"]
                bucket["equity"] += item["equity"]
                bucket["realized_pnl"] += item["realized_pnl"]
                bucket["closed_trades"] += item["closed_trades"]
        if runtime is None:
            session = self._session()
            try:
                artifact = self._latest_artifact(session)
                learning = {
                    "accepted": bool(artifact.accepted) if artifact else False,
                    "verdict": (artifact.metrics_json or {}).get("verdict") if artifact else None,
                }
            finally:
                session.close()
        else:
            learning = (runtime or {}).get("learning") or {}
        control = ab_groups.get("control") or {}
        learned = ab_groups.get("learned") or {}
        artifact_accepted = bool(learning.get("accepted"))
        if not artifact_accepted:
            return {
                "status": "rejected",
                "reason": ((learning.get("verdict") or {}).get("reason") if learning else None) or "artifact_not_accepted",
                "artifact_accepted": False,
            }
        learned_equity = float(learned.get("equity") or 0.0)
        learned_initial = float(learned.get("initial_bankroll") or 0.0)
        control_equity = float(control.get("equity") or 0.0)
        if learned_equity > learned_initial and learned_equity > control_equity:
            status = "positive"
            reason = "learned_above_start_and_control"
        else:
            status = "not_confirmed"
            if learned_equity <= learned_initial:
                reason = "learned_below_start"
            elif learned_equity <= control_equity:
                reason = "learned_below_control"
            else:
                reason = "live_ab_not_confirmed"
        return {
            "status": status,
            "reason": reason,
            "artifact_accepted": True,
            "learned_equity": learned_equity,
            "learned_initial_bankroll": learned_initial,
            "control_equity": control_equity,
        }

    def prometheus_metrics(self) -> str:
        overview = self.overview()
        runtime = overview["runtime"]
        ws = runtime.get("ws") or {}
        learning = runtime.get("learning") or {}
        lines = [
            "# HELP shadow_lab_ws_health_score Latest WebSocket health score.",
            "# TYPE shadow_lab_ws_health_score gauge",
            f"shadow_lab_ws_health_score {float(ws.get('health_score') or 0.0):.6f}",
            "# HELP shadow_lab_ws_gap_count_total Observed websocket gap count.",
            "# TYPE shadow_lab_ws_gap_count_total counter",
            f"shadow_lab_ws_gap_count_total {int(ws.get('gap_count') or 0)}",
            "# HELP shadow_lab_ws_last_message_age_seconds Age of last websocket message.",
            "# TYPE shadow_lab_ws_last_message_age_seconds gauge",
            f"shadow_lab_ws_last_message_age_seconds {float(ws.get('last_message_age_sec') or 0.0):.6f}",
            "# HELP shadow_lab_entries_frozen Whether new entries are frozen by ws guardrails.",
            "# TYPE shadow_lab_entries_frozen gauge",
            f"shadow_lab_entries_frozen {1 if ws.get('entries_frozen') else 0}",
            "# HELP shadow_lab_forced_taker_exit_ratio Ratio of forced taker exits.",
            "# TYPE shadow_lab_forced_taker_exit_ratio gauge",
            f"shadow_lab_forced_taker_exit_ratio {float(ws.get('forced_taker_exit_ratio') or 0.0):.6f}",
            "# HELP shadow_lab_maker_fill_ratio Ratio of maker-like fills.",
            "# TYPE shadow_lab_maker_fill_ratio gauge",
            f"shadow_lab_maker_fill_ratio {float(ws.get('maker_fill_ratio') or 0.0):.6f}",
            "# HELP shadow_lab_learning_artifact_accepted Whether latest model artifact passed holdouts.",
            "# TYPE shadow_lab_learning_artifact_accepted gauge",
            f"shadow_lab_learning_artifact_accepted {1 if learning.get('accepted') else 0}",
            "# HELP shadow_lab_learning_high_conf_accuracy Latest holdout high-confidence accuracy.",
            "# TYPE shadow_lab_learning_high_conf_accuracy gauge",
            f"shadow_lab_learning_high_conf_accuracy {float(learning.get('high_conf_accuracy') or 0.0):.6f}",
            "# HELP shadow_lab_learning_high_conf_net_ev Latest holdout high-confidence net EV.",
            "# TYPE shadow_lab_learning_high_conf_net_ev gauge",
            f"shadow_lab_learning_high_conf_net_ev {float(learning.get('high_conf_net_ev') or 0.0):.6f}",
            "# HELP shadow_lab_learning_calibration_error Latest model calibration error.",
            "# TYPE shadow_lab_learning_calibration_error gauge",
            f"shadow_lab_learning_calibration_error {float(learning.get('calibration_error') or 0.0):.6f}",
        ]
        training_fresh_until = learning.get("training_fresh_until")
        if training_fresh_until:
            rendered = str(training_fresh_until)
            rendered = rendered[:-1] + "+00:00" if rendered.endswith("Z") else rendered
            fresh_until = _as_utc(datetime.fromisoformat(rendered))
            freshness_sec = max(0.0, (fresh_until - _utcnow()).total_seconds()) if fresh_until else 0.0
            lines.extend([
                "# HELP shadow_lab_learning_freshness_seconds Seconds until learned artifact expires.",
                "# TYPE shadow_lab_learning_freshness_seconds gauge",
                f"shadow_lab_learning_freshness_seconds {freshness_sec:.6f}",
            ])
        for item in overview["portfolios"]:
            label = item["key"].replace('"', "")
            lines.append(f'shadow_lab_portfolio_realized_pnl{{portfolio="{label}"}} {float(item["realized_pnl"]):.6f}')
            lines.append(f'shadow_lab_portfolio_closed_trades{{portfolio="{label}"}} {int(item["closed_trades"])}')
            lines.append(f'shadow_lab_portfolio_fill_rate{{portfolio="{label}"}} {float(item["fill_rate"]):.6f}')
        for group, item in (overview.get("ab_groups") or {}).items():
            group_label = str(group).replace('"', "")
            lines.append(f'shadow_lab_ab_equity{{group="{group_label}"}} {float(item.get("equity") or 0.0):.6f}')
            lines.append(f'shadow_lab_ab_realized_pnl{{group="{group_label}"}} {float(item.get("realized_pnl") or 0.0):.6f}')
            lines.append(f'shadow_lab_ab_closed_trades{{group="{group_label}"}} {int(item.get("closed_trades") or 0)}')
            lines.append(f'shadow_lab_ab_fill_rate{{group="{group_label}"}} {float(item.get("fill_rate") or 0.0):.6f}')
            lines.append(f'shadow_lab_ab_forced_taker_exit_ratio{{group="{group_label}"}} {float(item.get("forced_taker_exit_ratio") or 0.0):.6f}')
        verdict = overview.get("verdict") or {}
        verdict_map = {"positive": 1, "not_confirmed": 0, "rejected": -1}
        lines.append("# HELP shadow_lab_training_verdict Combined offline/live training verdict.")
        lines.append("# TYPE shadow_lab_training_verdict gauge")
        lines.append(f"shadow_lab_training_verdict {verdict_map.get(str(verdict.get('status') or 'rejected'), -1)}")
        return "\n".join(lines) + "\n"

    def get_status(self, runtime: dict[str, Any] | None = None) -> StatusSnapshot:
        session = self._session()
        try:
            portfolio_rows = self._portfolio_rows(session)
            portfolio_ids = [row.id for row in portfolio_rows]
            portfolio_meta = {row.id: self._portfolio_meta(row) | {"key": row.key} for row in portfolio_rows}
            latest_eq_subq = (
                session.query(
                    LabEquityPointRow.portfolio_id.label("portfolio_id"),
                    func.max(LabEquityPointRow.timestamp).label("max_ts"),
                )
                .group_by(LabEquityPointRow.portfolio_id)
                .subquery()
            )
            latest_points = (
                session.query(LabEquityPointRow)
                .join(
                    latest_eq_subq,
                    (LabEquityPointRow.portfolio_id == latest_eq_subq.c.portfolio_id)
                    & (LabEquityPointRow.timestamp == latest_eq_subq.c.max_ts),
                )
                .all()
            )
            latest_point_by_portfolio = {row.portfolio_id: row for row in latest_points}

            bankroll = 0.0
            unrealized_pnl = 0.0
            drawdown_pct = 0.0
            ab_groups: dict[str, dict[str, Any]] = {}
            for portfolio in portfolio_rows:
                point = latest_point_by_portfolio.get(portfolio.id)
                equity = float(point.equity if point is not None else portfolio.initial_bankroll or 0.0)
                realized = float(point.realized_pnl if point is not None else 0.0)
                drawdown = float(point.drawdown_pct if point is not None else 0.0)
                unrealized = float(point.unrealized_pnl if point is not None else 0.0)
                bankroll += equity
                unrealized_pnl += unrealized
                drawdown_pct = max(drawdown_pct, drawdown)
                meta = portfolio_meta.get(portfolio.id, {})
                ab_group = str(meta.get("ab_group") or "single")
                bucket = ab_groups.setdefault(ab_group, {
                    "equity": 0.0,
                    "realized_pnl": 0.0,
                    "closed_trades": 0,
                    "fill_rate": 0.0,
                    "forced_taker_exit_ratio": 0.0,
                })
                bucket["equity"] += equity
                bucket["realized_pnl"] += realized

            now = _utcnow()
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            open_positions_count = 0
            realized_pnl_today = 0.0
            trades_today = 0
            if portfolio_ids:
                open_positions_count = int(
                    session.query(func.count(LabPositionRow.id))
                    .filter(LabPositionRow.portfolio_id.in_(portfolio_ids))
                    .filter(LabPositionRow.status == "open")
                    .scalar()
                    or 0
                )
                realized_pnl_today = float(
                    session.query(func.coalesce(func.sum(func.coalesce(LabPositionRow.pnl, LabPositionRow.realized_pnl)), 0.0))
                    .filter(LabPositionRow.portfolio_id.in_(portfolio_ids))
                    .filter(LabPositionRow.status == "closed")
                    .filter(LabPositionRow.closed_at >= day_start)
                    .scalar()
                    or 0.0
                )
                trades_today = int(
                    session.query(func.count(LabPositionRow.id))
                    .filter(LabPositionRow.portfolio_id.in_(portfolio_ids))
                    .filter(LabPositionRow.status == "closed")
                    .filter(LabPositionRow.closed_at >= day_start)
                    .scalar()
                    or 0
                )

            runtime_db = self._runtime_row(session)
            ws_metric = self._latest_ws_metric(session)
            artifact = self._latest_artifact(session)

            candidate_count_24h = 0
            accepted_count_24h = 0
            reject_count_24h = 0
            top_reject_reason = None
            last_decision_ts = None
            latest_ohlcv_age_sec = None
            if self._settings is not None and getattr(self._settings.lab, "crypto15m", None) is not None:
                since = now - timedelta(hours=24)
                prefix = "Crypto15m%"
                counts = (
                    session.query(LabDecisionAuditRow.decision, func.count(LabDecisionAuditRow.id))
                    .filter(LabDecisionAuditRow.timestamp >= since)
                    .filter(LabDecisionAuditRow.portfolio_key.like(prefix))
                    .group_by(LabDecisionAuditRow.decision)
                    .all()
                )
                count_map = {str(decision): int(count or 0) for decision, count in counts}
                candidate_count_24h = count_map.get("candidate", 0)
                accepted_count_24h = count_map.get("accepted", 0)
                reject_count_24h = count_map.get("rejected", 0)

                latest_decision = (
                    session.query(LabDecisionAuditRow)
                    .filter(LabDecisionAuditRow.portfolio_key.like(prefix))
                    .order_by(LabDecisionAuditRow.timestamp.desc())
                    .first()
                )
                if latest_decision is not None:
                    last_decision_ts = latest_decision.timestamp.isoformat() if latest_decision.timestamp else None
                    meta = latest_decision.meta_json or {}
                    if meta.get("crypto_ohlcv_age_sec") is not None:
                        latest_ohlcv_age_sec = float(meta.get("crypto_ohlcv_age_sec") or 0.0)

                recent_rejects = (
                    session.query(LabDecisionAuditRow)
                    .filter(LabDecisionAuditRow.timestamp >= since)
                    .filter(LabDecisionAuditRow.portfolio_key.like(prefix))
                    .filter(LabDecisionAuditRow.decision == "rejected")
                    .order_by(LabDecisionAuditRow.timestamp.desc())
                    .limit(500)
                    .all()
                )
                reason_counter: Counter[str] = Counter()
                for audit in recent_rejects:
                    meta = audit.meta_json or {}
                    reason = str(meta.get("reason") or meta.get("stage") or ",".join(audit.reasons_json or []) or "unknown")
                    reason_counter[reason] += 1
                top_reject_reason = reason_counter.most_common(1)[0][0] if reason_counter else None

            runtime_row = {
                "mode": runtime_db.mode if runtime_db else self._mode,
                "last_cycle_ts": runtime_db.last_cycle_ts.isoformat() if runtime_db and runtime_db.last_cycle_ts else None,
                "ws_connected": bool(runtime_db.ws_connected) if runtime_db else False,
                "last_cycle_error": runtime_db.last_cycle_error if runtime_db else None,
                "eligible_markets_last": int(runtime_db.eligible_markets_last or 0) if runtime_db else 0,
                "subscribed_tokens_last": int(runtime_db.subscribed_tokens_last or 0) if runtime_db else 0,
            }
            ws = {
                "health_score": float(ws_metric.health_score or 0.0) if ws_metric else 0.0,
                "entries_frozen": bool(ws_metric.entries_frozen) if ws_metric else False,
                "forced_taker_exit_ratio": float(ws_metric.forced_taker_exit_ratio or 0.0) if ws_metric else 0.0,
            }
            learning = {
                "artifact_key": artifact.artifact_key if artifact else None,
            }
        finally:
            session.close()
        return StatusSnapshot(
            mode=str(runtime_row.get("mode") or self._mode),
            bankroll=float(bankroll),
            realized_pnl_today=realized_pnl_today,
            unrealized_pnl=unrealized_pnl,
            open_positions_count=open_positions_count,
            trades_today=trades_today,
            drawdown_pct=drawdown_pct,
            last_cycle_ts=runtime_row.get("last_cycle_ts"),
            ws_connected=bool(runtime_row.get("ws_connected")),
            gate_status=str(runtime_row.get("mode") or self._mode),
            paper_days=0,
            last_cycle_error=runtime_row.get("last_cycle_error"),
            markets_fetched_last=int(runtime_row.get("eligible_markets_last") or 0),
            subscribed_tokens_last=int(runtime_row.get("subscribed_tokens_last") or 0),
            ws_health_score=float(ws.get("health_score") or 0.0),
            entries_frozen=bool(ws.get("entries_frozen")),
            forced_taker_exit_ratio=float(ws.get("forced_taker_exit_ratio") or 0.0),
            learned_artifact_key=learning.get("artifact_key"),
            ab_groups=ab_groups,
            candidate_count_24h=candidate_count_24h,
            accepted_count_24h=accepted_count_24h,
            reject_count_24h=reject_count_24h,
            top_reject_reason=top_reject_reason,
            last_decision_ts=last_decision_ts,
            latest_ohlcv_age_sec=latest_ohlcv_age_sec,
        )

    def pnl_breakdown(self) -> dict[str, Any]:
        summaries = self.portfolio_summaries()
        now = _utcnow()
        week_ago = now - timedelta(days=7)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        wins_today = 0
        session = self._session()
        try:
            closed = session.query(LabPositionRow).filter(LabPositionRow.status == "closed").all()
            pnl_7d = sum(
                (position.pnl or 0.0)
                for position in closed
                if _as_utc(position.closed_at) and _as_utc(position.closed_at) >= week_ago
            )
            wins_today = sum(
                1
                for position in closed
                if _as_utc(position.closed_at)
                and _as_utc(position.closed_at) >= day_start
                and (position.pnl or 0.0) > 0
            )
        finally:
            session.close()

        wins = sum(item["wins"] for item in summaries)
        losses = sum(item["losses"] for item in summaries)
        trades_total = sum(item["closed_trades"] for item in summaries)
        all_time = sum(item["realized_pnl"] for item in summaries)
        today_realized = sum(item["realized_today"] for item in summaries)
        unrealized = sum(item["unrealized_pnl"] for item in summaries)
        best_trade = None
        worst_trade = None

        session = self._session()
        try:
            pnl_values = [value for value, in session.query(LabPositionRow.pnl).filter(LabPositionRow.status == "closed").all() if value is not None]
            if pnl_values:
                best_trade = max(pnl_values)
                worst_trade = min(pnl_values)
        finally:
            session.close()

        return {
            "today_realized": today_realized,
            "trades_today": sum(item["trades_today"] for item in summaries),
            "wins_today": wins_today,
            "d7_realized": pnl_7d,
            "all_time_realized": all_time,
            "unrealized": unrealized,
            "bankroll": sum(item["equity"] for item in summaries),
            "trades_total": trades_total,
            "wins": wins,
            "losses": losses,
            "avg_pnl": (all_time / trades_total) if trades_total else 0.0,
            "best_trade": best_trade,
            "worst_trade": worst_trade,
            "ab_groups": self.overview().get("ab_groups") or {},
        }

    def open_positions_detail(self) -> list[dict[str, Any]]:
        session = self._session()
        try:
            portfolio_meta = {
                row.id: self._portfolio_meta(row) | {"key": row.key}
                for row in self._portfolio_rows(session)
            }
            rows = (
                session.query(LabPositionRow)
                .filter(LabPositionRow.status == "open")
                .all()
            )
            out = []
            for position in rows:
                market = session.get(MarketRow, position.market_id)
                mark = position.current_price if position.current_price is not None else position.entry_price
                hold_hours = 0.0
                opened_at = _as_utc(position.opened_at)
                if opened_at:
                    hold_hours = (_utcnow() - opened_at).total_seconds() / 3600.0
                meta = portfolio_meta.get(position.portfolio_id, {})
                out.append({
                    "portfolio_key": meta.get("key", ""),
                    "ab_group": meta.get("ab_group", "single"),
                    "base_key": meta.get("base_key", ""),
                    "question": market.question if market else "",
                    "market_id": position.market_id,
                    "token_id": position.token_id,
                    "side": position.side,
                    "entry": position.entry_price,
                    "mark": mark,
                    "size": position.size,
                    "unrealized_pnl": position.size * (mark - position.entry_price),
                    "hold_hours": hold_hours,
                })
            return out
        finally:
            session.close()

    def recent_trades(self, n: int = 15) -> list[dict[str, Any]]:
        session = self._session()
        try:
            portfolio_meta = {
                row.id: self._portfolio_meta(row) | {"key": row.key}
                for row in self._portfolio_rows(session)
            }
            rows = (
                session.query(LabPositionRow)
                .filter(LabPositionRow.status == "closed")
                .order_by(LabPositionRow.closed_at.desc())
                .limit(n)
                .all()
            )
            out = []
            for position in rows:
                market = session.get(MarketRow, position.market_id)
                meta = portfolio_meta.get(position.portfolio_id, {})
                out.append({
                    "portfolio_key": meta.get("key", ""),
                    "ab_group": meta.get("ab_group", "single"),
                    "base_key": meta.get("base_key", ""),
                    "opened_at": position.opened_at,
                    "closed_at": position.closed_at,
                    "side": position.side,
                    "size": position.size,
                    "entry": position.entry_price,
                    "exit": position.current_price if position.current_price is not None else position.entry_price,
                    "pnl": position.pnl,
                    "exit_reason": position.exit_reason or "unknown",
                    "forced_exit": bool(position.forced_exit),
                    "question": market.question if market else "",
                })
            return out
        finally:
            session.close()

    def exit_reason_counts_today(self) -> dict[str, int]:
        session = self._session()
        try:
            today = _utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            rows = (
                session.query(LabPositionRow)
                .filter(LabPositionRow.status == "closed")
                .filter(LabPositionRow.closed_at >= today)
                .all()
            )
            counter: Counter[str] = Counter()
            for position in rows:
                counter[position.exit_reason or "unknown"] += 1
            return dict(counter)
        finally:
            session.close()

    def gate_snapshot(self) -> dict[str, Any]:
        session = self._session()
        try:
            first_equity = session.query(func.min(LabEquityPointRow.timestamp)).scalar()
            runtime_row = self._runtime_row(session)
            days = 0
            if first_equity is not None:
                days = max(0, (_utcnow() - _as_utc(first_equity)).days)
            closed_trades = session.query(LabPositionRow).filter(LabPositionRow.status == "closed").count()
            realized = session.query(func.coalesce(func.sum(LabPositionRow.pnl), 0.0)).filter(LabPositionRow.status == "closed").scalar() or 0.0
            return {
                "paper_started_at": first_equity,
                "paper_days_completed": days,
                "paper_trades_count": closed_trades,
                "paper_realized_pnl": realized,
                "paper_errors_count": self.error_counts(24),
                "gate_status": runtime_row.mode if runtime_row else self._mode,
            }
        finally:
            session.close()

    def error_counts(self, hours: int = 24) -> int:
        session = self._session()
        try:
            since = _utcnow() - timedelta(hours=hours)
            return (
                session.query(AuditRow)
                .filter(AuditRow.timestamp >= since)
                .filter(AuditRow.event_type.like("%error%"))
                .count()
            )
        finally:
            session.close()

    def markets_count(self) -> int:
        session = self._session()
        try:
            return session.query(MarketRow).count()
        finally:
            session.close()
