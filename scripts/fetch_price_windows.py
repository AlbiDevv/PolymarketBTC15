from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_settings
from exchange_client.polymarket import PolymarketClient
from historical import append_manifest, read_dataset, write_partitioned_parquet
from research.trade_costs import coerce_bool


def _clean_token_id(value) -> str:
    if value is None:
        return ""
    try:
        if value != value:
            return ""
    except TypeError:
        pass
    rendered = str(value).strip()
    if not rendered or rendered.lower() in {"nan", "none", "null"}:
        return ""
    if rendered.endswith(".0") and rendered[:-2].isdigit():
        return rendered[:-2]
    return rendered


def _to_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    rendered = str(value)
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(rendered).astimezone(timezone.utc)
    except ValueError:
        return None


def _coverage_days(points: list[datetime]) -> int:
    if not points:
        return 0
    return max(0, int((max(points) - min(points)).days))


async def _fetch_token_points(
    client: PolymarketClient,
    semaphore: asyncio.Semaphore,
    *,
    token_id: str,
    start: datetime,
    end: datetime,
    fidelity_sec: int,
) -> tuple[str, list[dict]]:
    async with semaphore:
        points = await client.get_prices_history(
            token_id,
            start_ts=int(start.timestamp()),
            end_ts=int(end.timestamp()),
            fidelity=fidelity_sec,
        )
        return token_id, points


async def _fetch_market_window(
    client: PolymarketClient,
    semaphore: asyncio.Semaphore,
    row: dict,
    settings,
) -> tuple[list[dict], datetime | None]:
    settled_at = row.get("__settled_at__") or row.get("__end_date__") or _to_dt(row.get("settled_at")) or _to_dt(row.get("end_date"))
    if settled_at is None:
        return [], None

    start = settled_at - timedelta(minutes=settings.historical.price_window.pre_event_minutes)
    end = settled_at + timedelta(minutes=settings.historical.price_window.post_event_minutes)
    token_specs = [
        ("YES", _clean_token_id(row.get("yes_token_id"))),
        ("NO", _clean_token_id(row.get("no_token_id"))),
    ]
    tasks = [
        _fetch_token_points(
            client,
            semaphore,
            token_id=token_id,
            start=start,
            end=end,
            fidelity_sec=settings.historical.price_window.fidelity_sec,
        )
        for _, token_id in token_specs
        if token_id
    ]
    if not tasks:
        return [], None

    fetched = await asyncio.gather(*tasks)
    token_to_side = {token_id: side for side, token_id in token_specs}
    out_rows: list[dict] = []
    for token_id, points in fetched:
        side = token_to_side[token_id]
        for point in points:
            out_rows.append({
                "date": settled_at.date().isoformat(),
                "market_id": str(row.get("market_id") or ""),
                "event_id": str(row.get("event_id") or ""),
                "question": str(row.get("question") or ""),
                "category": str(row.get("category") or ""),
                "outcome": str(row.get("outcome") or ""),
                "settled_at": settled_at.isoformat(),
                "token_id": token_id,
                "side": side,
                "timestamp": int(point["timestamp"]),
                "price": float(point["price"]),
            })
    return out_rows, settled_at if out_rows else None


async def _run(args):
    settings = load_settings(args.config)
    resolutions = read_dataset(settings.historical.price_window.out_dir, "resolutions")
    if resolutions.empty:
        print("no resolutions found")
        return
    if "market_id" in resolutions.columns:
        if "standard_binary_pair" in resolutions.columns:
            resolutions["__standard_known__"] = resolutions["standard_binary_pair"].notna()
            resolutions = resolutions.sort_values("__standard_known__")
        resolutions = resolutions.drop_duplicates(subset=["market_id"], keep="last")
    if "settled_at" in resolutions.columns:
        resolutions["__settled_at__"] = resolutions["settled_at"].map(_to_dt)
    else:
        resolutions["__settled_at__"] = None
    if "end_date" in resolutions.columns:
        resolutions["__end_date__"] = resolutions["end_date"].map(_to_dt)
    else:
        resolutions["__end_date__"] = None
    resolutions = resolutions.sort_values(
        by=["__settled_at__", "__end_date__"],
        ascending=[False, False],
        na_position="last",
    )

    client = PolymarketClient(settings)
    semaphore = asyncio.Semaphore(settings.historical.price_window.concurrency)
    out_rows: list[dict] = []
    processed = 0
    settled_points: list[datetime] = []
    scheduled_by_day: dict[str, int] = {}
    ready = False
    try:
        pending: list[asyncio.Task] = []
        for _, row in resolutions.iterrows():
            row_dict = row.to_dict()
            settled_at = row_dict.get("__settled_at__") or row_dict.get("__end_date__") or _to_dt(row_dict.get("settled_at")) or _to_dt(row_dict.get("end_date"))
            if settled_at is None:
                continue
            if "standard_binary_pair" in row_dict and not coerce_bool(row_dict.get("standard_binary_pair"), default=True):
                continue
            if not _clean_token_id(row_dict.get("yes_token_id")) or not _clean_token_id(row_dict.get("no_token_id")):
                continue
            day_key = settled_at.date().isoformat()
            per_day_limit = settings.historical.price_window.max_markets_per_settlement_day
            if per_day_limit and scheduled_by_day.get(day_key, 0) >= per_day_limit:
                continue
            scheduled_by_day[day_key] = scheduled_by_day.get(day_key, 0) + 1

            pending.append(asyncio.create_task(_fetch_market_window(client, semaphore, row_dict, settings)))
            if len(pending) < settings.historical.price_window.concurrency:
                continue

            for market_rows, settled_at in await asyncio.gather(*pending):
                if market_rows and settled_at is not None:
                    out_rows.extend(market_rows)
                    processed += 1
                    settled_points.append(settled_at)
                coverage_days = _coverage_days(settled_points)
                ready = (
                    processed >= settings.historical.price_window.min_markets_required
                    and coverage_days >= settings.historical.price_window.min_coverage_days
                )
                if ready or processed >= settings.historical.price_window.max_markets_per_run:
                    break
            pending = []
            if ready or processed >= settings.historical.price_window.max_markets_per_run:
                break

        if pending and processed < settings.historical.price_window.max_markets_per_run:
            for market_rows, settled_at in await asyncio.gather(*pending):
                if market_rows and settled_at is not None:
                    out_rows.extend(market_rows)
                    processed += 1
                    settled_points.append(settled_at)
                coverage_days = _coverage_days(settled_points)
                ready = (
                    processed >= settings.historical.price_window.min_markets_required
                    and coverage_days >= settings.historical.price_window.min_coverage_days
                )
                if ready or processed >= settings.historical.price_window.max_markets_per_run:
                    break
    finally:
        await client.close()

    coverage_days = _coverage_days(settled_points)
    ready = (
        processed >= settings.historical.price_window.min_markets_required
        and coverage_days >= settings.historical.price_window.min_coverage_days
    )
    status = "ready" if ready else "not_enough_history"
    written = write_partitioned_parquet(
        settings.historical.price_window.out_dir,
        "price_windows",
        out_rows,
        partition_key="date",
        filename_prefix="price_windows",
    )
    append_manifest(settings.historical.price_window.out_dir, "price_windows", {
        "markets_processed": processed,
        "rows": len(out_rows),
        "coverage_days": coverage_days,
        "status": status,
        "settlement_days_scheduled": len(scheduled_by_day),
        "max_markets_per_settlement_day": settings.historical.price_window.max_markets_per_settlement_day,
        "min_markets_required": settings.historical.price_window.min_markets_required,
        "min_coverage_days": settings.historical.price_window.min_coverage_days,
        "written_files": [str(path) for path in written],
    })
    print(f"processed_markets={processed} rows={len(out_rows)} coverage_days={coverage_days} status={status}")
    if status != "ready" and settings.historical.price_window.fail_on_insufficient_history:
        raise SystemExit("not_enough_history")


def main():
    parser = argparse.ArgumentParser(description="Fetch Polymarket historical price windows into parquet lake")
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
