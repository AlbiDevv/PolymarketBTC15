from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_data.orderbook_manager import OrderbookManager
from market_data.ws_client import PolymarketWebSocket


def test_process_current_market_message_updates_snapshot():
    mgr = OrderbookManager()
    ws = PolymarketWebSocket("wss://example.test/ws", mgr)

    asyncio.run(ws._process_message(
        '{"event_type":"book","asset_id":"tok_yes","bids":[{"price":"0.41","size":"120"}],"asks":[{"price":"0.43","size":"95"}]}'
    ))

    ob = mgr.get_orderbook("tok_yes")
    assert ob is not None
    assert ob.best_bid == 0.41
    assert ob.best_ask == 0.43


def test_process_legacy_book_delta_message_still_supported():
    mgr = OrderbookManager()
    ws = PolymarketWebSocket("wss://example.test/ws", mgr)

    asyncio.run(ws._process_message(
        '{"channel":"book","data":{"market":"tok_yes","type":"snapshot","bids":[{"price":"0.50","size":"10"}],"asks":[{"price":"0.52","size":"8"}]}}'
    ))
    asyncio.run(ws._process_message(
        '{"channel":"book","data":{"market":"tok_yes","bids":[{"price":"0.51","size":"7"}],"asks":[{"price":"0.52","size":"0"}]}}'
    ))

    ob = mgr.get_orderbook("tok_yes")
    assert ob is not None
    assert ob.best_bid == 0.51
    assert ob.best_ask > 0.52
