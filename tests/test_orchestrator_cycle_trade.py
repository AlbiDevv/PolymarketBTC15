"""
Один полный цикл оркестратора с моками Gamma/CLOB: рынок и стаканы подобраны так,
чтобы H2 давал сигнал YES и EV проходил edge_threshold — ожидается dry_run сделка.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exchange_client.base import Market, Orderbook, OrderbookLevel, Token
from runner.orchestrator import Orchestrator


def _reset_db_engine():
    import db.session as db_session

    db_session._engine = None
    db_session._SessionFactory = None


def _yes_book_h2_zone() -> Orderbook:
    """mid≈0.11 в зоне H2 (0.08–0.12), bias +0.03 → p_model=0.14, EV_yes ≥ edge 0.02."""
    return Orderbook(
        market_id="yes_tok_e2e",
        bids=[
            OrderbookLevel(0.108, 12_000),
            OrderbookLevel(0.107, 12_000),
        ],
        asks=[
            OrderbookLevel(0.112, 12_000),
            OrderbookLevel(0.113, 12_000),
        ],
        timestamp=1_000_000.0,
    )


def _no_book() -> Orderbook:
    return Orderbook(
        market_id="no_tok_e2e",
        bids=[OrderbookLevel(0.885, 12_000), OrderbookLevel(0.884, 12_000)],
        asks=[OrderbookLevel(0.889, 12_000), OrderbookLevel(0.890, 12_000)],
        timestamp=1_000_000.0,
    )


def test_single_cycle_dry_run_executes_trade(tmp_path):
    dbfile = tmp_path / "cycle_e2e.db"
    db_url = f"sqlite:///{dbfile.as_posix()}"

    yaml_path = tmp_path / "settings.yaml"
    yaml_path.write_text(
        f"""
mode: dry_run
database:
  url: "{db_url}"
strategy:
  edge_threshold: 0.02
  stake_min: 1.0
  stake_max: 2.0
  max_spread: 0.15
  hypotheses:
    - H2
liquidity:
  min_daily_volume: 1
  min_depth_usd: 1
  max_price_impact: 0.1
bankroll:
  initial: 500
alerts:
  telegram_enabled: false
logging:
  level: WARNING
  file: "{(tmp_path / 't.log').as_posix()}"
""",
        encoding="utf-8",
    )

    _reset_db_engine()

    from config import load_settings

    settings = load_settings(yaml_path)

    mkt = Market(
        id="cond_e2e_trade",
        question="E2E orchestrator YES in H2 zone?",
        category="test",
        end_date=None,
        resolution_source="",
        active=True,
        volume_24h=50_000.0,
        tokens=[
            Token(token_id="yes_tok_e2e", outcome="Yes", price=0.11),
            Token(token_id="no_tok_e2e", outcome="No", price=0.89),
        ],
        event_id="evt_e2e",
    )
    ob_yes = _yes_book_h2_zone()
    ob_no = _no_book()

    async def fake_orderbook(tid: str) -> Orderbook:
        if tid == "yes_tok_e2e":
            return ob_yes
        if tid == "no_tok_e2e":
            return ob_no
        raise AssertionError(f"unexpected token {tid}")

    async def _cycle():
        with patch("runner.orchestrator.PolymarketClient") as MC:
            inst = MC.return_value
            inst.get_markets = AsyncMock(return_value=[mkt])
            inst.get_orderbook = AsyncMock(side_effect=fake_orderbook)

            orch = Orchestrator(settings)
            await orch._run_cycle()

    asyncio.run(_cycle())

    from db.session import get_session
    from db.models import OrderRow, PositionRow

    sess = get_session(db_url)
    try:
        orders = sess.query(OrderRow).filter(OrderRow.side == "YES").all()
        positions = sess.query(PositionRow).filter(PositionRow.status == "open").all()
        assert len(orders) >= 1
        assert len(positions) >= 1
        assert positions[0].token_id == "yes_tok_e2e"
    finally:
        sess.close()
