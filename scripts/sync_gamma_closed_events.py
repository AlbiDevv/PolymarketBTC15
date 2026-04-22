from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings
from exchange_client.polymarket import PolymarketClient
from historical import append_manifest, write_partitioned_parquet
from research.trade_costs import coerce_bool, coerce_fee_rate


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _market_key(item: dict) -> str:
    return str(item.get("condition_id") or item.get("conditionId") or item.get("id") or "")


def _dedupe_markets(markets: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for item in markets:
        key = _market_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _standard_binary_pair(yes_outcome: str, no_outcome: str) -> bool:
    pair = (yes_outcome.strip().lower(), no_outcome.strip().lower())
    return pair in {
        ("yes", "no"),
        ("up", "down"),
        ("over", "under"),
    }


def _normalize_resolution_rows(markets: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for item in markets:
        tokens = PolymarketClient._parse_tokens(item)
        yes_token = next((token for token in tokens if str(token.outcome).upper() == "YES"), None)
        no_token = next((token for token in tokens if str(token.outcome).upper() == "NO"), None)
        if (yes_token is None or no_token is None) and len(tokens) >= 2:
            yes_token = yes_token or tokens[0]
            no_token = no_token or tokens[1]
        winner_token = None
        outcome = ""
        for token in tokens:
            if token.winner is True:
                winner_token = token
                break
        if winner_token is None:
            priced_winners = [token for token in tokens if token.price >= 0.999]
            if len(priced_winners) == 1:
                winner_token = priced_winners[0]
        if winner_token is not None and yes_token is not None and no_token is not None:
            if winner_token.token_id == yes_token.token_id:
                outcome = "YES"
            elif winner_token.token_id == no_token.token_id:
                outcome = "NO"
            else:
                outcome = str(winner_token.outcome or "").upper()
        rows.append({
            "date": item.get("closedTime") or item.get("endDate") or item.get("end_date"),
            "market_id": str(item.get("condition_id") or item.get("conditionId") or item.get("id") or ""),
            "event_id": str(item.get("event_id") or item.get("eventId") or ""),
            "question": str(item.get("question") or ""),
            "category": str(item.get("category") or ""),
            "tags": item.get("tags") or [],
            "outcome": outcome,
            "yes_outcome": str(yes_token.outcome or "") if yes_token else "",
            "no_outcome": str(no_token.outcome or "") if no_token else "",
            "standard_binary_pair": _standard_binary_pair(
                str(yes_token.outcome or "") if yes_token else "",
                str(no_token.outcome or "") if no_token else "",
            ),
            "raw_winning_outcome": str(winner_token.outcome or "") if winner_token else "",
            "end_date": item.get("endDate") or item.get("end_date"),
            "settled_at": item.get("closedTime") or item.get("closed_time") or item.get("endDate"),
            "volume_24h": float(item.get("volume24hr") or item.get("volume_24h") or item.get("volume_num_24hr") or 0.0),
            "liquidity": float(item.get("liquidity") or item.get("liquidityClob") or 0.0),
            "fees_enabled": coerce_bool(item.get("feesEnabled"), default=True),
            "fee_rate": coerce_fee_rate(
                item.get("feeRate")
                or item.get("fee_rate")
                or item.get("feeRateBps")
                or item.get("fee_rate_bps"),
                default=0.02,
            ),
            "fee_type": str(item.get("feeType") or item.get("fee_type") or ""),
            "yes_token_id": yes_token.token_id if yes_token else "",
            "no_token_id": no_token.token_id if no_token else "",
            "source": "gamma",
        })
    return rows


async def _run(args):
    settings = load_settings(args.config)
    client = PolymarketClient(settings)
    try:
        events = await client.get_closed_events(limit=settings.historical.sync.batch_size, max_pages=settings.historical.sync.page_limit)
        markets = await client.get_closed_markets(limit=settings.historical.sync.batch_size, max_pages=settings.historical.sync.page_limit)
        if settings.historical.sync.date_backfill_days:
            cursor = _utcnow() - timedelta(days=settings.historical.sync.date_backfill_stride_days)
            stop_at = _utcnow() - timedelta(days=settings.historical.sync.date_backfill_days)
            while cursor >= stop_at:
                markets.extend(await client.get_closed_markets(
                    limit=settings.historical.sync.batch_size,
                    max_pages=settings.historical.sync.date_backfill_page_limit,
                    extra_params={"end_date_max": cursor.isoformat()},
                ))
                cursor -= timedelta(days=settings.historical.sync.date_backfill_stride_days)
        markets = _dedupe_markets(markets)
    finally:
        await client.close()

    out_dir = settings.historical.sync.out_dir
    event_rows = [{
        "date": item.get("closed_time") or item.get("end_date"),
        **{k: v for k, v in item.items() if k != "raw"},
    } for item in events]
    resolution_rows = _normalize_resolution_rows(markets)
    event_files = write_partitioned_parquet(out_dir, "events", event_rows, partition_key="date", filename_prefix="events")
    resolution_files = write_partitioned_parquet(out_dir, "resolutions", resolution_rows, partition_key="date", filename_prefix="resolutions")
    append_manifest(out_dir, "gamma_sync", {
        "events": len(event_rows),
        "markets": len(resolution_rows),
        "date_backfill_days": settings.historical.sync.date_backfill_days,
        "date_backfill_stride_days": settings.historical.sync.date_backfill_stride_days,
        "date_backfill_page_limit": settings.historical.sync.date_backfill_page_limit,
        "event_files": [str(path) for path in event_files],
        "resolution_files": [str(path) for path in resolution_files],
        "source": "gamma+markets",
    })
    print(f"events={len(event_rows)} resolutions={len(resolution_rows)}")


def main():
    parser = argparse.ArgumentParser(description="Sync closed Polymarket events/markets into parquet lake")
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
