from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_settings
from exchange_client.polymarket import PolymarketClient
from historical import append_manifest, read_dataset, replace_dataset, write_partitioned_parquet


async def _fetch_token(client: PolymarketClient, token_id: str, start, end, fidelity: int) -> list[dict]:
    return await client.get_prices_history(
        token_id,
        start_ts=int(start.timestamp()),
        end_ts=int(end.timestamp()),
        fidelity=fidelity,
    )


async def _fetch_market(client: PolymarketClient, settings, row, semaphore: asyncio.Semaphore) -> tuple[int, list[dict]]:
    async with semaphore:
        settled_at = row["settled_at_dt"]
        start = settled_at - timedelta(minutes=settings.historical.price_window.pre_event_minutes)
        end = settled_at + timedelta(minutes=settings.historical.price_window.post_event_minutes)
        fetched = await asyncio.gather(
            _fetch_token(client, str(row["yes_token_id"]), start, end, settings.historical.price_window.fidelity_sec),
            _fetch_token(client, str(row["no_token_id"]), start, end, settings.historical.price_window.fidelity_sec),
            return_exceptions=True,
        )
    rows: list[dict] = []
    for side, points in [("YES", fetched[0]), ("NO", fetched[1])]:
        if isinstance(points, Exception):
            continue
        for point in points:
            rows.append({
                "date": settled_at.date().isoformat(),
                "market_id": str(row["market_id"]),
                "event_id": str(row.get("event_id") or ""),
                "question": str(row["question"]),
                "category": str(row.get("category") or ""),
                "outcome": str(row.get("outcome") or ""),
                "settled_at": settled_at.isoformat(),
                "asset": str(row.get("asset") or ""),
                "symbol": str(row.get("symbol") or ""),
                "timeframe_minutes": int(row.get("timeframe_minutes") or 0),
                "token_id": str(row["yes_token_id"] if side == "YES" else row["no_token_id"]),
                "side": side,
                "timestamp": int(point["timestamp"]),
                "price": float(point["price"]),
            })
    return (1 if rows else 0), rows


async def _run(args):
    settings = load_settings(args.config)
    resolutions = read_dataset(settings.historical.price_window.out_dir, "crypto15m_resolutions")
    if resolutions.empty:
        print({"rows": 0, "reason": "no_crypto15m_resolutions"})
        return
    resolutions["settled_at_dt"] = pd.to_datetime(resolutions["settled_at"], utc=True, format="mixed")
    resolutions = (
        resolutions.sort_values(["market_id", "settled_at_dt"], ascending=[True, True])
        .drop_duplicates("market_id", keep="first")
        .sort_values("settled_at_dt", ascending=False)
    )
    existing_market_ids: set[str] = set()
    if args.skip_existing and args.append:
        existing = read_dataset(settings.historical.price_window.out_dir, "crypto15m_price_windows")
        if not existing.empty and "market_id" in existing.columns:
            existing_market_ids = {str(value) for value in existing["market_id"].dropna().unique()}
            resolutions = resolutions[~resolutions["market_id"].astype(str).isin(existing_market_ids)]
    resolutions = resolutions.head(args.max_markets)

    client = PolymarketClient(settings)
    rows: list[dict] = []
    processed = 0
    try:
        semaphore = asyncio.Semaphore(max(1, args.concurrency))
        tasks = [
            _fetch_market(client, settings, row, semaphore)
            for _, row in resolutions.iterrows()
        ]
        total = len(tasks)
        chunk_size = max(1, args.chunk_size)
        for start_idx in range(0, total, chunk_size):
            chunk = tasks[start_idx:start_idx + chunk_size]
            for result in await asyncio.gather(*chunk):
                market_processed, market_rows = result
                processed += market_processed
                rows.extend(market_rows)
            print({
                "stage": "price_windows",
                "done": min(start_idx + len(chunk), total),
                "total": total,
                "markets_processed": processed,
                "rows": len(rows),
            }, flush=True)
    finally:
        await client.close()

    if not args.append:
        replace_dataset(settings.historical.price_window.out_dir, "crypto15m_price_windows")
    written = write_partitioned_parquet(
        settings.historical.price_window.out_dir,
        "crypto15m_price_windows",
        rows,
        partition_key="date",
        filename_prefix="crypto15m_price_windows",
    )
    append_manifest(settings.historical.price_window.out_dir, "crypto15m_price_windows", {
        "markets_processed": processed,
        "rows": len(rows),
        "markets_skipped_existing": len(existing_market_ids),
        "skip_existing": args.skip_existing,
        "append": args.append,
        "concurrency": args.concurrency,
        "chunk_size": args.chunk_size,
        "written_files": [str(path) for path in written],
    })
    print({"markets_processed": processed, "rows": len(rows), "written": [str(path) for path in written]})


def main():
    parser = argparse.ArgumentParser(description="Fetch Polymarket price windows for crypto15m markets")
    parser.add_argument("--config", default=None)
    parser.add_argument("--max-markets", type=int, default=500)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--chunk-size", type=int, default=250)
    parser.add_argument("--skip-existing", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--append", action="store_true", help="Append a new price-window batch instead of replacing the generated dataset")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
