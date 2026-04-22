"""
Prediction Markets Trading System — Entry Point

Usage:
    python main.py                    # Start in dry_run mode (default from config)
    python main.py --mode live        # Override mode
    python main.py --mode shadow_maker
    python main.py --config path.yaml # Custom config file
    python main.py --collect-only     # Only collect data, no trading
    python main.py --dashboard-only   # Only run the read-only lab dashboard
"""

from __future__ import annotations

import asyncio
import argparse
import sys
import warnings
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

from config import load_settings
from db.session import init_db
from dashboard.app import run_dashboard
from lab import ShadowLabRunner
from runner.orchestrator import Orchestrator


warnings.filterwarnings(
    "ignore",
    message=r"`sklearn\.utils\.parallel\.delayed` should be used with `sklearn\.utils\.parallel\.Parallel`.*",
    category=UserWarning,
)


def setup_logging(settings):
    logger.remove()  # remove default handler

    # Console
    logger.add(
        sys.stderr,
        level=settings.logging.level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
    )

    # File
    log_path = Path(settings.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_path),
        level=settings.logging.level,
        rotation=settings.logging.rotation,
        retention=settings.logging.retention,
        encoding="utf-8",
    )


def parse_args():
    parser = argparse.ArgumentParser(description="Prediction Markets Trader")
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to settings.yaml",
    )
    parser.add_argument(
        "--mode",
        choices=["dry_run", "paper", "live", "shadow_maker"],
        default=None,
        help="Override trading mode",
    )
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="Only collect market data, no trading",
    )
    parser.add_argument(
        "--dashboard-only",
        action="store_true",
        help="Only run the shadow lab dashboard",
    )
    return parser.parse_args()


async def run_trader(settings):
    if settings.mode == "shadow_maker":
        runner = ShadowLabRunner(settings)
        await runner.start()
        return

    orch = Orchestrator(settings)
    await orch.start()


async def run_collector(settings):
    from exchange_client.polymarket import PolymarketClient
    from db.session import get_session
    from db.models import MarketRow, PriceHistoryRow
    from datetime import datetime

    client = PolymarketClient(settings)
    logger.info("Data collector started")

    try:
        while True:
            markets = await client.get_markets(active_only=True)
            session = get_session(settings.database.url)
            saved = 0

            for m in markets:
                if not m.tokens:
                    continue

                row = (
                    session.query(MarketRow)
                    .filter(MarketRow.polymarket_id == m.id)
                    .first()
                )
                if not row:
                    yes_token = next((t for t in m.tokens if t.outcome.lower() == "yes"), None)
                    no_token = next((t for t in m.tokens if t.outcome.lower() == "no"), None)
                    row = MarketRow(
                        polymarket_id=m.id,
                        event_id=m.event_id,
                        question=m.question,
                        category=m.category,
                        resolution_source=m.resolution_source,
                        active=m.active,
                        volume_24h=m.volume_24h,
                        yes_token_id=yes_token.token_id if yes_token else "",
                        no_token_id=no_token.token_id if no_token else "",
                    )
                    session.add(row)
                    session.flush()

                yes_token = next((t for t in m.tokens if t.outcome.lower() == "yes"), None)
                if yes_token:
                    try:
                        ob = await client.get_orderbook(yes_token.token_id)
                        session.add(
                            PriceHistoryRow(
                                market_id=row.id,
                                timestamp=datetime.utcnow(),
                                bid=ob.best_bid,
                                ask=ob.best_ask,
                                mid=ob.mid_price,
                                spread=ob.spread,
                                volume_24h=m.volume_24h,
                                depth_bid=ob.depth("bid"),
                                depth_ask=ob.depth("ask"),
                            )
                        )
                        saved += 1
                    except Exception as e:
                        logger.warning(f"Orderbook fetch failed for {m.id}: {e}")

            session.commit()
            session.close()
            logger.info(f"Collected data for {saved} markets")
            await asyncio.sleep(settings.collector.interval_sec)

    finally:
        await client.close()


def main():
    args = parse_args()

    settings = load_settings(args.config)
    if args.mode:
        settings.mode = args.mode

    setup_logging(settings)
    init_db(settings.database.url)

    logger.info(f"Mode: {settings.mode}")
    logger.info(f"Bankroll: ${settings.bankroll.initial}")
    logger.info(f"Edge threshold: {settings.strategy.edge_threshold}")

    if settings.mode == "live" and not settings.polymarket_api_key:
        logger.error("LIVE mode requires POLYMARKET_API_KEY in .env")
        sys.exit(1)

    if args.dashboard_only:
        run_dashboard(settings)
        return

    if args.collect_only:
        asyncio.run(run_collector(settings))
    else:
        asyncio.run(run_trader(settings))


if __name__ == "__main__":
    main()
