"""
Historical data collector for hypothesis calibration.

Fetches resolved markets from Gamma API and saves them to DB.
This builds the dataset needed for H2/H4 train() methods.

Data-quality and research requirements (dataset v1, p_market, leakage):
  see research/RESEARCH_SPRINT.md section 7 and research/dataset_spec_v1.yaml

Usage:
    python scripts/collect_historical.py              # collect resolved markets
    python scripts/collect_historical.py --pages 50   # fetch up to 50 pages

Output:
    Writes to markets + price_history tables with source="historical".
    Reports how many resolved markets with known outcomes were collected.
"""

from __future__ import annotations

import asyncio
import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from loguru import logger

from config import load_settings
from db.session import init_db, get_session
from db.models import MarketRow, PriceHistoryRow


async def collect_resolved_markets(gamma_url: str, max_pages: int = 20) -> list[dict]:
    """Fetch resolved/closed markets from Gamma API."""
    all_markets = []
    cursor = None

    async with httpx.AsyncClient(timeout=30.0) as client:
        for page in range(max_pages):
            params = {"limit": "100", "closed": "true"}
            if cursor:
                params["next_cursor"] = cursor

            logger.info(f"Fetching page {page + 1}...")
            resp = await client.get(f"{gamma_url}/markets", params=params)
            resp.raise_for_status()
            data = resp.json()

            items = []
            next_cursor = None
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("data", data.get("markets", []))
                next_cursor = data.get("next_cursor")

            if not items:
                break

            resolved_count = 0
            for m in items:
                tokens = m.get("tokens", [])
                if isinstance(tokens, str):
                    import json
                    try:
                        tokens = json.loads(tokens)
                    except Exception:
                        tokens = []

                has_winner = any(t.get("winner") is True for t in tokens)
                if has_winner or m.get("resolved"):
                    m["_tokens_parsed"] = tokens
                    all_markets.append(m)
                    resolved_count += 1

            logger.info(f"  Page {page + 1}: {len(items)} markets, {resolved_count} resolved")

            if not next_cursor or len(items) < 100:
                break
            cursor = next_cursor

    return all_markets


def save_to_db(markets: list[dict], db_url: str):
    """Save resolved markets to database for calibration."""
    session = get_session(db_url)
    new_markets = 0
    new_prices = 0

    try:
        for m in markets:
            condition_id = m.get("condition_id", m.get("id", ""))
            if not condition_id:
                continue

            existing = (
                session.query(MarketRow)
                .filter(MarketRow.polymarket_id == condition_id)
                .first()
            )

            tokens = m.get("_tokens_parsed", m.get("tokens", []))
            if isinstance(tokens, str):
                import json
                try:
                    tokens = json.loads(tokens)
                except Exception:
                    tokens = []

            # Determine outcome
            outcome = None
            yes_price = 0.0
            no_price = 0.0
            yes_token_id = ""
            no_token_id = ""

            for t in tokens:
                tok_outcome = t.get("outcome", "").upper()
                if tok_outcome == "YES":
                    yes_price = float(t.get("price", 0))
                    yes_token_id = t.get("token_id", "")
                    if t.get("winner") is True:
                        outcome = "YES"
                elif tok_outcome == "NO":
                    no_price = float(t.get("price", 0))
                    no_token_id = t.get("token_id", "")
                    if t.get("winner") is True:
                        outcome = "NO"

            if not existing:
                row = MarketRow(
                    polymarket_id=condition_id,
                    event_id=m.get("event_id"),
                    question=m.get("question", ""),
                    category=m.get("category", ""),
                    resolution_source=m.get("resolution_source", ""),
                    active=False,
                    volume_24h=float(m.get("volume_num_24hr", 0)),
                    yes_token_id=yes_token_id,
                    no_token_id=no_token_id,
                    outcome=outcome,
                )
                session.add(row)
                session.flush()
                new_markets += 1
            else:
                row = existing
                if outcome and not row.outcome:
                    row.outcome = outcome

            # Historical snapshot: YES mid + optional native NO token price from Gamma
            yes_mid = yes_price if yes_price > 0 else (1.0 - no_price if no_price > 0 else 0.0)
            no_mid = no_price if no_price > 0 else None
            if yes_mid > 0:
                existing_ph = (
                    session.query(PriceHistoryRow)
                    .filter(PriceHistoryRow.market_id == row.id)
                    .filter(PriceHistoryRow.source == "historical")
                    .first()
                )
                if not existing_ph:
                    session.add(PriceHistoryRow(
                        market_id=row.id,
                        timestamp=datetime.now(timezone.utc),
                        bid=yes_price - 0.01 if yes_price > 0.01 else 0,
                        ask=yes_price + 0.01 if yes_price < 0.99 else 1.0,
                        mid=yes_mid,
                        no_mid=no_mid,
                        spread=0.02,
                        volume_24h=float(m.get("volume_num_24hr", 0)),
                        source="historical",
                    ))
                    new_prices += 1

        session.commit()
        logger.info(f"Saved {new_markets} new markets, {new_prices} price points")

    except Exception as e:
        logger.error(f"DB save failed: {e}")
        session.rollback()
    finally:
        session.close()


def report_calibration_data(db_url: str):
    """Report what calibration data is available."""
    session = get_session(db_url)
    try:
        total = session.query(MarketRow).count()
        resolved = session.query(MarketRow).filter(MarketRow.outcome.isnot(None)).count()
        yes_wins = session.query(MarketRow).filter(MarketRow.outcome == "YES").count()
        no_wins = session.query(MarketRow).filter(MarketRow.outcome == "NO").count()
        prices = session.query(PriceHistoryRow).count()

        logger.info("=== Calibration Data Summary ===")
        logger.info(f"  Total markets: {total}")
        logger.info(f"  Resolved:      {resolved} ({yes_wins} YES, {no_wins} NO)")
        logger.info(f"  Price points:  {prices}")
        logger.info(f"  Ready for H2/H4 train(): {'YES' if resolved >= 50 else 'NO'} (need >= 50 resolved)")
    finally:
        session.close()


async def main():
    parser = argparse.ArgumentParser(description="Collect historical Polymarket data")
    parser.add_argument("--pages", type=int, default=20, help="Max pages to fetch")
    parser.add_argument("--config", type=str, default=None, help="Config file path")
    args = parser.parse_args()

    settings = load_settings(args.config)
    init_db(settings.database.url)

    logger.info(f"Collecting resolved markets from {settings.exchange.gamma_url}")

    markets = await collect_resolved_markets(
        gamma_url=settings.exchange.gamma_url,
        max_pages=args.pages,
    )

    logger.info(f"Fetched {len(markets)} resolved markets total")

    save_to_db(markets, settings.database.url)
    report_calibration_data(settings.database.url)


if __name__ == "__main__":
    asyncio.run(main())
