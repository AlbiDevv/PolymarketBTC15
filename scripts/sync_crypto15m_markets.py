from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_settings
from exchange_client.polymarket import PolymarketClient
from historical import append_manifest, replace_dataset, write_partitioned_parquet
from research.crypto15m import CryptoMarketInfo, classify_crypto15m_updown_market


def _to_dt(value) -> datetime | None:
    if not value:
        return None
    rendered = str(value)
    if rendered.endswith("Z"):
        rendered = rendered[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(rendered)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_outcome(outcome: str) -> str:
    normalized = str(outcome or "").strip().upper()
    if normalized in {"YES", "UP"}:
        return "YES"
    if normalized in {"NO", "DOWN"}:
        return "NO"
    return ""


def _winner(raw: dict) -> str:
    tokens = raw.get("tokens") or []
    for token in tokens or []:
        if token.get("winner") is True:
            outcome = _normalize_outcome(str(token.get("outcome") or ""))
            if outcome:
                return outcome
    outcomes = PolymarketClient._parse_list_field(raw.get("outcomes"))
    prices = PolymarketClient._parse_list_field(raw.get("outcomePrices") or raw.get("outcome_prices"))
    best_idx = -1
    best_price = -1.0
    for idx, price in enumerate(prices):
        try:
            value = float(price)
        except (TypeError, ValueError):
            continue
        if value > best_price:
            best_idx = idx
            best_price = value
    if best_idx >= 0 and best_price >= 0.99 and best_idx < len(outcomes):
        return _normalize_outcome(str(outcomes[best_idx]))
    return ""


def _append_market_row(
    client: PolymarketClient,
    raw: dict,
    rows: list[dict],
    seen: set[str],
    history_days: int,
    force_asset: str = "",
) -> None:
    question = str(raw.get("question") or raw.get("title") or "")
    info = classify_crypto15m_updown_market(question, category=str(raw.get("category") or ""), tags=raw.get("tags") or [])
    if not info.is_crypto15m and force_asset:
        asset = force_asset.upper()
        forced_info = classify_crypto15m_updown_market(question)
        if forced_info.is_crypto15m:
            info = CryptoMarketInfo(True, asset=asset, symbol=f"{asset}/USDT", timeframe_minutes=15)
    if not info.is_crypto15m:
        return
    market_id = str(raw.get("condition_id") or raw.get("conditionId") or raw.get("id") or "")
    if not market_id or market_id in seen:
        return
    parsed = client._parse_market(raw)
    yes_token = next((token for token in parsed.tokens if token.outcome.lower() in {"yes", "up"}), None)
    no_token = next((token for token in parsed.tokens if token.outcome.lower() in {"no", "down"}), None)
    # Use the market end time as the trading cutoff. Gamma closedTime can lag the
    # actual 15m window and would leak post-resolution prices into training.
    settled_at = _to_dt(raw.get("endDate") or raw.get("end_date") or raw.get("endDateIso") or raw.get("closedTime") or raw.get("closed_time"))
    closed_at = _to_dt(raw.get("closedTime") or raw.get("closed_time"))
    if settled_at is None or yes_token is None or no_token is None:
        return
    if (datetime.now(timezone.utc) - settled_at).days > history_days:
        return
    seen.add(market_id)
    rows.append({
        "date": settled_at.date().isoformat(),
        "market_id": market_id,
        "event_id": str(raw.get("event_id") or raw.get("eventId") or ""),
        "question": question,
        "category": str(raw.get("category") or ""),
        "asset": info.asset,
        "symbol": info.symbol,
        "timeframe_minutes": info.timeframe_minutes,
        "outcome": _winner(raw),
        "settled_at": settled_at.isoformat(),
        "closed_at": closed_at.isoformat() if closed_at else "",
        "yes_token_id": yes_token.token_id,
        "no_token_id": no_token.token_id,
    })


async def _fetch_slug_payloads(client: PolymarketClient, slug: str) -> list[dict]:
    payloads: list[dict] = []
    try:
        market = await client._gamma_get(f"/markets/slug/{slug}")
        if isinstance(market, dict):
            payloads.append(market)
    except Exception:
        pass
    if payloads:
        return payloads
    try:
        event = await client._gamma_get(f"/events/slug/{slug}")
    except Exception:
        return []
    if not isinstance(event, dict):
        return []
    if event.get("markets"):
        return [market for market in event.get("markets", []) if isinstance(market, dict)]
    return [event]


async def _fetch_slug_jobs(client: PolymarketClient, jobs: list[tuple[str, str]], concurrency: int) -> list[tuple[str, list[dict]]]:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def one(asset: str, slug: str) -> tuple[str, list[dict]]:
        async with semaphore:
            return asset, await _fetch_slug_payloads(client, slug)

    return await asyncio.gather(*(one(asset, slug) for asset, slug in jobs))


async def _run(args):
    settings = load_settings(args.config)
    client = PolymarketClient(settings)
    rows: list[dict] = []
    seen: set[str] = set()
    slug_timeframes = [
        int(part.strip().rstrip("m"))
        for part in str(args.slug_timeframes).split(",")
        if part.strip()
    ]
    slug_assets = [
        part.strip().lower()
        for part in str(args.slug_assets).split(",")
        if part.strip()
    ]
    try:
        markets = await client.get_closed_markets(limit=100, max_pages=args.max_pages, order="closedTime", ascending=False)
        for raw in markets:
            _append_market_row(client, raw, rows, seen, settings.crypto_data.history_days)
        print({"stage": "closed_markets_scan", "raw": len(markets), "crypto15m_rows": len(rows)}, flush=True)

        if args.slug_scan:
            now = datetime.now(timezone.utc)
            start = now - timedelta(days=settings.crypto_data.history_days)
            start_epoch = int(start.timestamp() // 900 * 900)
            end_epoch = int(now.timestamp() // 900 * 900)
            cursor = end_epoch
            requested = 0
            slug_jobs: list[tuple[str, str]] = []
            while cursor >= start_epoch and requested < args.max_slug_requests:
                for timeframe in slug_timeframes:
                    if timeframe <= 0:
                        continue
                    # Slug timestamps are aligned to the market start time.
                    if cursor % (timeframe * 60) != 0:
                        continue
                    for prefix in slug_assets:
                        if requested >= args.max_slug_requests:
                            break
                        slug = f"{prefix}-updown-{timeframe}m-{cursor}"
                        requested += 1
                        slug_jobs.append((prefix, slug))
                    if requested >= args.max_slug_requests:
                        break
                cursor -= 300
            total_jobs = len(slug_jobs)
            chunk_size = max(1, args.slug_chunk_size)
            for start_idx in range(0, total_jobs, chunk_size):
                chunk = slug_jobs[start_idx:start_idx + chunk_size]
                for prefix, payloads in await _fetch_slug_jobs(client, chunk, args.slug_concurrency):
                    for market_payload in payloads:
                        _append_market_row(
                            client,
                            market_payload,
                            rows,
                            seen,
                            settings.crypto_data.history_days,
                            force_asset=prefix,
                        )
                print({
                    "stage": "slug_scan",
                    "done": min(start_idx + len(chunk), total_jobs),
                    "total": total_jobs,
                    "crypto15m_rows": len(rows),
                }, flush=True)
    finally:
        await client.close()

    if not args.append:
        replace_dataset(settings.historical.price_window.out_dir, "crypto15m_resolutions")
    written = write_partitioned_parquet(
        settings.historical.price_window.out_dir,
        "crypto15m_resolutions",
        rows,
        partition_key="date",
        filename_prefix="crypto15m_resolutions",
    )
    append_manifest(settings.historical.price_window.out_dir, "crypto15m_resolutions", {
        "rows": len(rows),
        "max_pages": args.max_pages,
        "slug_scan": args.slug_scan,
        "max_slug_requests": args.max_slug_requests,
        "slug_concurrency": args.slug_concurrency,
        "slug_chunk_size": args.slug_chunk_size,
        "slug_assets": slug_assets,
        "slug_timeframes": slug_timeframes,
        "history_days": settings.crypto_data.history_days,
        "append": args.append,
        "written_files": [str(path) for path in written],
    })
    print({"rows": len(rows), "written": [str(path) for path in written]})


def main():
    parser = argparse.ArgumentParser(description="Sync closed Polymarket BTC/ETH 15m/1h markets")
    parser.add_argument("--config", default=None)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--slug-scan", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--max-slug-requests", type=int, default=20000)
    parser.add_argument("--slug-concurrency", type=int, default=12)
    parser.add_argument("--slug-chunk-size", type=int, default=50)
    parser.add_argument("--slug-assets", default="btc,eth")
    parser.add_argument("--slug-timeframes", default="15")
    parser.add_argument("--append", action="store_true", help="Append a new resolutions batch instead of replacing the generated dataset")
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
