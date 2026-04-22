import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.session import init_db, get_session
from db.models import MarketRow, PositionRow, PnlLogRow
from monitor.stats_service import StatsService


def test_bankroll_and_pnl_breakdown_memory_sqlite():
    url = "sqlite:///:memory:"
    init_db(url)
    session = get_session(url)
    now = datetime.now(timezone.utc)
    try:
        m = MarketRow(
            polymarket_id="x1", question="q", category="c",
            outcome="YES",
        )
        session.add(m)
        session.flush()
        session.add(
            PositionRow(
                market_id=m.id, token_id="t", side="YES",
                entry_price=0.5, current_price=0.5, size=10, status="closed",
                pnl=5.0, closed_at=now, exit_reason="settlement",
            )
        )
        session.add(PnlLogRow(
            date=now, realized_pnl=5, unrealized_pnl=0, bankroll=505,
            trades_count=1, hit_rate=1.0,
        ))
        session.commit()
    finally:
        session.close()

    st = StatsService(url, 500.0, "paper")
    assert st.bankroll_latest(get_session(url)) == 505.0
    p = st.pnl_breakdown()
    assert p["trades_total"] == 1
    assert p["all_time_realized"] == 5.0
    rt = st.recent_trades(5)
    assert rt[0]["exit_reason"] == "settlement"
    assert st.exit_reason_counts_today().get("settlement") == 1
