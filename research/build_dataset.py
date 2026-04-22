"""
Build research dataset v1 from SQLite (markets + price_history).

Usage:
  python -m research.build_dataset --config config/settings.yaml
  python -m research.build_dataset --db-url sqlite:///data.db --out research/artifacts/dataset_v1.csv

Synthetic / fallback rows are tagged in quality_flags (never silent mixing).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from .buckets import (
    BUCKET_POLICY_VERSION,
    liquidity_bucket,
    round_zone_bucket,
    spread_bucket,
    tail_bucket,
    tte_bucket,
)
from .data_quality import validate_row_v1
from .dataset_row import ResearchDatasetRowV1
from .definitions import (
    p_market_fallback_no_from_yes_complement,
    resolved_outcome_for_side,
)

DEFAULT_DATASET_VERSION = "ds_v1"
DEFAULT_FEATURE_VERSION = "fv1"
DEFAULT_SPLIT_VERSION = "unassigned"


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_synthetic_historical_mid(spread: float | None, source: str | None) -> bool:
    """Heuristic: collect_historical uses spread=0.02 and fabricated bid/ask around yes mid."""
    if source != "historical":
        return False
    if spread is None:
        return False
    return abs(spread - 0.02) < 1e-6


def build_rows_from_session(session, dataset_version: str, feature_version: str) -> tuple[list[ResearchDatasetRowV1], list[dict]]:
    """Returns (valid_rows, invalid_report_dicts)."""
    from db.models import MarketRow, PriceHistoryRow

    invalid: list[dict] = []
    rows: list[ResearchDatasetRowV1] = []

    markets = session.query(MarketRow).filter(MarketRow.outcome.isnot(None)).all()

    for m in markets:
        outcome = (m.outcome or "").upper()
        if outcome not in ("YES", "NO"):
            continue

        ph = (
            session.query(PriceHistoryRow)
            .filter(PriceHistoryRow.market_id == m.id)
            .order_by(PriceHistoryRow.timestamp.desc())
            .first()
        )
        if not ph:
            invalid.append({"market_id": m.polymarket_id, "reason": "no_price_history"})
            continue

        decision_ts = _utc(ph.timestamp) or datetime.now(timezone.utc)
        resolved_ts = _utc(m.settled_at) or _utc(m.end_date)

        yes_mid = float(ph.mid) if ph.mid is not None else None
        if yes_mid is None and ph.bid is not None and ph.ask is not None:
            yes_mid = (float(ph.bid) + float(ph.ask)) / 2.0

        if yes_mid is None:
            invalid.append({"market_id": m.polymarket_id, "reason": "no_mid"})
            continue

        spread = float(ph.spread) if ph.spread is not None else 0.0
        liq = float(ph.volume_24h or m.volume_24h or 0)
        src = ph.source or "live"
        qf_base: list[str] = []
        if _is_synthetic_historical_mid(ph.spread, src):
            qf_base.append("synthetic_mid_from_collector")

        end_anchor = resolved_ts or _utc(m.end_date)
        tte_sec = 0.0
        if end_anchor and decision_ts:
            tte_sec = max(0.0, (end_anchor - decision_ts).total_seconds())

        tte_b = tte_bucket(tte_sec)
        sp_b = spread_bucket(spread)
        lq_b = liquidity_bucket(liq)

        fee = 0.02
        no_mid_raw = getattr(ph, "no_mid", None)
        no_native = float(no_mid_raw) if no_mid_raw is not None and float(no_mid_raw) > 0 else None

        for side in ("YES", "NO"):
            token_id = m.yes_token_id if side == "YES" else m.no_token_id
            if not token_id:
                invalid.append({"market_id": m.polymarket_id, "side": side, "reason": "missing_token_id"})
                continue
            qf = list(qf_base)

            if side == "YES":
                p_mkt = max(0.01, min(0.99, yes_mid))
                exec_assump = "native_yes_mid"
                src_type = "native_yes"
            else:
                if no_native is not None:
                    p_mkt = max(0.01, min(0.99, no_native))
                    exec_assump = "native_no_mid"
                    src_type = "native_no"
                else:
                    p_mkt = max(0.01, min(0.99, p_market_fallback_no_from_yes_complement(yes_mid) or 0.5))
                    exec_assump = "complement_from_yes_mid"
                    qf.append("fallback_complement_no_book")
                    src_type = "complement_fallback"

            y = resolved_outcome_for_side(side, outcome)  # type: ignore
            rz = round_zone_bucket(p_mkt)
            tb = tail_bucket(p_mkt)

            row = ResearchDatasetRowV1(
                dataset_version=dataset_version,
                feature_version=feature_version,
                split_version=DEFAULT_SPLIT_VERSION,
                market_id=m.polymarket_id,
                event_id=m.event_id,
                token_id=token_id or "",
                side=side,  # type: ignore
                decision_ts=decision_ts,
                category=m.category or "unknown",
                time_to_resolution_sec=tte_sec,
                tte_bucket=tte_b,
                spread=spread,
                spread_bucket=sp_b,
                liquidity_proxy=liq,
                liquidity_bucket=lq_b,
                depth_bid=float(ph.depth_bid) if ph.depth_bid is not None else None,
                depth_ask=float(ph.depth_ask) if ph.depth_ask is not None else None,
                p_market=p_mkt,
                mid_price_if_needed_for_reference=yes_mid,
                execution_price_assumption=exec_assump,
                entry_fee_assumption=fee,
                exit_fee_assumption=fee,
                source="historical" if src == "historical" else "mixed",
                resolved_outcome_for_side=y,
                resolved_ts=resolved_ts,
                quality_flags=qf,
                extra={"round_zone_bucket": rz, "tail_bucket": tb, "bucket_policy": BUCKET_POLICY_VERSION},
                p_market_source=src_type,
            )

            vr = validate_row_v1(row)
            if not vr.ok:
                invalid.append({"market_id": m.polymarket_id, "side": side, "errors": vr.errors})
                continue
            rows.append(row)

    return rows, invalid


def export_csv(rows: list[ResearchDatasetRowV1], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        logger.warning("No rows to export")
        return
    fieldnames = [
        "dataset_version", "feature_version", "split_version",
        "market_id", "event_id", "token_id", "side", "decision_ts",
        "category", "time_to_resolution_sec", "tte_bucket",
        "spread", "spread_bucket", "liquidity_proxy", "liquidity_bucket",
        "depth_bid", "depth_ask", "p_market", "p_market_source", "mid_price_if_needed_for_reference",
        "execution_price_assumption", "entry_fee_assumption", "exit_fee_assumption",
        "source", "resolved_outcome_for_side", "resolved_ts",
        "quality_flags_json", "extra_json",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({
                "dataset_version": r.dataset_version,
                "feature_version": r.feature_version,
                "split_version": r.split_version,
                "market_id": r.market_id,
                "event_id": r.event_id or "",
                "token_id": r.token_id,
                "side": r.side,
                "decision_ts": r.decision_ts.isoformat(),
                "category": r.category,
                "time_to_resolution_sec": r.time_to_resolution_sec,
                "tte_bucket": r.tte_bucket,
                "spread": r.spread,
                "spread_bucket": r.spread_bucket,
                "liquidity_proxy": r.liquidity_proxy,
                "liquidity_bucket": r.liquidity_bucket,
                "depth_bid": r.depth_bid if r.depth_bid is not None else "",
                "depth_ask": r.depth_ask if r.depth_ask is not None else "",
                "p_market": r.p_market,
                "p_market_source": getattr(r, "p_market_source", "legacy_unlabeled"),
                "mid_price_if_needed_for_reference": r.mid_price_if_needed_for_reference if r.mid_price_if_needed_for_reference is not None else "",
                "execution_price_assumption": r.execution_price_assumption,
                "entry_fee_assumption": r.entry_fee_assumption,
                "exit_fee_assumption": r.exit_fee_assumption,
                "source": r.source,
                "resolved_outcome_for_side": r.resolved_outcome_for_side,
                "resolved_ts": r.resolved_ts.isoformat() if r.resolved_ts else "",
                "quality_flags_json": json.dumps(r.quality_flags),
                "extra_json": json.dumps(r.extra),
            })


def summary_report(rows: list[ResearchDatasetRowV1], invalid: list[dict]) -> str:
    n_syn = sum(1 for r in rows if "synthetic_mid_from_collector" in r.quality_flags)
    n_fb = sum(1 for r in rows if "fallback_complement_no_book" in r.quality_flags)
    no_rows = [r for r in rows if r.side == "NO"]
    n_no = len(no_rows)
    n_native_no = sum(1 for r in no_rows if getattr(r, "p_market_source", "") == "native_no")
    fb_share_no = (n_fb / n_no) if n_no else 0.0
    lines = [
        f"rows_valid: {len(rows)}",
        f"rows_invalid: {len(invalid)}",
        f"rows_synthetic_mid_flag: {n_syn}",
        f"rows_no_book_complement_flag: {n_fb}",
        f"no_side_native_mid_rows: {n_native_no} (of NO rows)",
        f"no_side_complement_fallback_share: {fb_share_no:.2%}",
        f"bucket_policy: {BUCKET_POLICY_VERSION}",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Build research dataset v1")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--db-url", type=str, default=None)
    parser.add_argument("--out", type=str, default="research/artifacts/dataset_v1.csv")
    parser.add_argument("--dataset-version", type=str, default=DEFAULT_DATASET_VERSION)
    parser.add_argument("--feature-version", type=str, default=DEFAULT_FEATURE_VERSION)
    args = parser.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    if args.config:
        from config import load_settings
        settings = load_settings(args.config)
        db_url = settings.database.url
    elif args.db_url:
        db_url = args.db_url
    else:
        from config import load_settings
        settings = load_settings(None)
        db_url = settings.database.url

    from db.session import get_session, init_db

    init_db(db_url)
    session = get_session(db_url)
    try:
        rows, invalid = build_rows_from_session(session, args.dataset_version, args.feature_version)
        out = Path(args.out)
        export_csv(rows, out)
        inv_path = out.with_suffix(".invalid.json")
        inv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(inv_path, "w", encoding="utf-8") as f:
            json.dump(invalid, f, indent=2)
        rep = summary_report(rows, invalid)
        rep_path = out.with_suffix(".summary.txt")
        with open(rep_path, "w", encoding="utf-8") as f:
            f.write(rep + "\n")
        logger.info(rep)
        logger.info(f"Wrote {out}, {inv_path}, {rep_path}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
