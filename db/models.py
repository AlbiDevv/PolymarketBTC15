from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Integer,
    Float,
    String,
    Boolean,
    DateTime,
    Text,
    JSON,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class MarketRow(Base):
    __tablename__ = "markets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    polymarket_id = Column(String(256), unique=True, nullable=False, index=True)
    event_id = Column(String(256), index=True)
    question = Column(Text, nullable=False)
    category = Column(String(128), default="")
    end_date = Column(DateTime, nullable=True)
    resolution_source = Column(String(256), default="")
    outcome = Column(String(16), nullable=True)  # YES / NO / null
    settled_at = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True)
    volume_24h = Column(Float, default=0)
    tags = Column(JSON, default=list)
    yes_token_id = Column(String(256), default="")
    no_token_id = Column(String(256), default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    price_history = relationship("PriceHistoryRow", back_populates="market")
    positions = relationship("PositionRow", back_populates="market")
    signals = relationship("SignalRow", back_populates="market")


class PriceHistoryRow(Base):
    __tablename__ = "price_history"
    __table_args__ = (
        Index("ix_price_history_market_ts", "market_id", "timestamp"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    bid = Column(Float)
    ask = Column(Float)
    mid = Column(Float)
    spread = Column(Float)
    volume_24h = Column(Float)
    depth_bid = Column(Float)
    depth_ask = Column(Float)
    source = Column(String(32), default="live")  # live | historical
    no_mid = Column(Float, nullable=True)  # native NO token mid when available

    market = relationship("MarketRow", back_populates="price_history")


class TradeRow(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False)
    side = Column(String(4), nullable=False)  # YES / NO
    price = Column(Float, nullable=False)
    size = Column(Float, nullable=False)
    order_id = Column(String(256), nullable=True)


class PositionRow(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    token_id = Column(String(256), default="")
    event_id = Column(String(256), nullable=True, index=True)
    side = Column(String(4), nullable=False)
    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    size = Column(Float, nullable=False)          # in contracts (shares), not dollars
    opened_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime, nullable=True)
    pnl = Column(Float, nullable=True)
    status = Column(String(16), default="open")  # open / closed / disputed
    exit_reason = Column(String(32), nullable=True)
    # stop_loss | take_profit | time_exit | settlement | settlement_cancelled | disputed | unknown

    market = relationship("MarketRow", back_populates="positions")


class OrderRow(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    exchange_order_id = Column(String(256), nullable=True)
    token_id = Column(String(256), default="")
    side = Column(String(4), nullable=False)  # BUY / SELL
    price = Column(Float, nullable=False)
    size = Column(Float, nullable=False)
    filled_size = Column(Float, default=0)
    status = Column(String(16), default="pending")  # pending/filled/partial/cancelled
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    filled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)


class PnlLogRow(Base):
    __tablename__ = "pnl_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, nullable=False, index=True)
    realized_pnl = Column(Float, default=0)
    unrealized_pnl = Column(Float, default=0)
    bankroll = Column(Float, nullable=False)
    trades_count = Column(Integer, default=0)
    hit_rate = Column(Float, nullable=True)


class SignalRow(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    hypothesis = Column(String(8), nullable=False)  # H1 / H2 / H3 / H4 / H5
    model_probability = Column(Float, nullable=False)
    market_probability = Column(Float, nullable=False)
    edge = Column(Float, nullable=False)
    action_taken = Column(Boolean, default=False)

    market = relationship("MarketRow", back_populates="signals")


class SettlementRow(Base):
    """v3.0: Separate table for settlement details (resolved/disputed/cancelled)."""
    __tablename__ = "settlements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False, index=True)
    status = Column(String(16), nullable=False)  # resolved / disputed / cancelled
    outcome = Column(String(16), nullable=True)  # YES / NO / null for cancelled
    resolved_at = Column(DateTime, nullable=True)
    dispute_reason = Column(Text, nullable=True)
    payout_details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class OrderbookRawRow(Base):
    """v3.0: Raw orderbook snapshots for execution analysis."""
    __tablename__ = "orderbooks_raw"

    id = Column(Integer, primary_key=True, autoincrement=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)
    bids_json = Column(JSON, nullable=False)   # [{price, size}, ...]
    asks_json = Column(JSON, nullable=False)
    mid = Column(Float)
    spread = Column(Float)


class AuditRow(Base):
    """v3.0: Audit trail for every significant state change."""
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    event_type = Column(String(64), nullable=False)  # reconciliation, trade_opened, settlement_*, shutdown
    details = Column(Text, nullable=True)  # JSON blob


class GateStateRow(Base):
    """
    Single-row metrics for dry_run → paper → live gates (no log parsing).
    id=1 is the canonical row.
    """
    __tablename__ = "gate_state"

    id = Column(Integer, primary_key=True, autoincrement=True)
    paper_started_at = Column(DateTime, nullable=True)
    paper_days_completed = Column(Integer, default=0)
    paper_trades_count = Column(Integer, default=0)
    paper_realized_pnl = Column(Float, default=0.0)
    paper_errors_count = Column(Integer, default=0)
    gate_status = Column(String(32), default="dry_run")
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class LabPortfolioRow(Base):
    __tablename__ = "lab_portfolios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String(64), unique=True, nullable=False, index=True)
    mode = Column(String(32), default="shadow_maker")
    settings_json = Column(JSON, nullable=False, default=dict)
    initial_bankroll = Column(Float, nullable=False, default=0.0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))


class LabOrderRow(Base):
    __tablename__ = "lab_orders"
    __table_args__ = (
        Index("ix_lab_orders_portfolio_status", "portfolio_id", "status"),
        Index("ix_lab_orders_token_status", "token_id", "status"),
        Index("ix_lab_orders_portfolio_created", "portfolio_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("lab_portfolios.id"), nullable=False)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    token_id = Column(String(256), nullable=False, index=True)
    event_id = Column(String(256), nullable=True, index=True)
    side = Column(String(4), nullable=False)  # YES / NO
    action = Column(String(8), nullable=False, default="BUY")
    price = Column(Float, nullable=False)
    size_total = Column(Float, nullable=False)
    size_remaining = Column(Float, nullable=False)
    filled_size = Column(Float, default=0.0)
    status = Column(String(24), default="working")  # working/filled/partial/cancelled/expired/rejected
    order_kind = Column(String(24), default="maker")  # maker/forced_taker
    queue_ahead = Column(Float, default=0.0)
    visible_size_same_side = Column(Float, default=0.0)
    reprices = Column(Integer, default=0)
    ttl_sec = Column(Integer, default=30)
    hypothesis = Column(String(16), default="")
    edge = Column(Float, default=0.0)
    forced_exit = Column(Boolean, default=False)
    close_reason = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_repriced_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)


class LabFillRow(Base):
    __tablename__ = "lab_fills"
    __table_args__ = (
        Index("ix_lab_fills_portfolio_ts", "portfolio_id", "timestamp"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("lab_portfolios.id"), nullable=False)
    order_id = Column(Integer, ForeignKey("lab_orders.id"), nullable=False)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    token_id = Column(String(256), nullable=False, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    side = Column(String(4), nullable=False)
    price = Column(Float, nullable=False)
    size = Column(Float, nullable=False)
    notional = Column(Float, nullable=False)
    fill_type = Column(String(24), default="partial")  # partial/full/forced_taker_exit


class LabPositionRow(Base):
    __tablename__ = "lab_positions"
    __table_args__ = (
        Index("ix_lab_positions_portfolio_status", "portfolio_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("lab_portfolios.id"), nullable=False)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    token_id = Column(String(256), nullable=False, index=True)
    event_id = Column(String(256), nullable=True, index=True)
    side = Column(String(4), nullable=False)
    strategy_key = Column(String(64), nullable=False, index=True)
    hypothesis = Column(String(16), default="")
    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    size = Column(Float, nullable=False)
    opened_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime, nullable=True)
    pnl = Column(Float, nullable=True)
    realized_pnl = Column(Float, default=0.0)
    status = Column(String(16), default="open")  # open / closed
    exit_reason = Column(String(32), nullable=True)
    forced_exit = Column(Boolean, default=False)


class LabEquityPointRow(Base):
    __tablename__ = "lab_equity_points"
    __table_args__ = (
        Index("ix_lab_equity_points_portfolio_ts", "portfolio_id", "timestamp"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("lab_portfolios.id"), nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True)
    bankroll = Column(Float, nullable=False)
    realized_pnl = Column(Float, nullable=False, default=0.0)
    unrealized_pnl = Column(Float, nullable=False, default=0.0)
    equity = Column(Float, nullable=False, default=0.0)
    drawdown_pct = Column(Float, nullable=False, default=0.0)


class LabDecisionAuditRow(Base):
    __tablename__ = "lab_decision_audit"
    __table_args__ = (
        Index("ix_lab_decision_audit_ts", "timestamp"),
        Index("ix_lab_decision_audit_portfolio_market", "portfolio_id", "market_id"),
        Index("ix_lab_decision_audit_decision", "decision"),
        Index("ix_lab_decision_audit_portfolio_ts", "portfolio_key", "timestamp"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    portfolio_id = Column(Integer, ForeignKey("lab_portfolios.id"), nullable=True)
    market_id = Column(Integer, ForeignKey("markets.id"), nullable=False)
    token_id = Column(String(256), nullable=True, index=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    decision = Column(String(24), nullable=False)  # rejected/candidate/accepted/entered
    track = Column(String(32), default="control")
    portfolio_key = Column(String(64), nullable=True, index=True)
    hypothesis = Column(String(16), default="")
    side = Column(String(4), nullable=True)
    edge = Column(Float, default=0.0)
    quality_score = Column(Float, default=0.0)
    expected_net_edge = Column(Float, default=0.0)
    fee_rate = Column(Float, default=0.0)
    estimated_slippage = Column(Float, default=0.0)
    spread = Column(Float, default=0.0)
    bid_depth = Column(Float, default=0.0)
    ask_depth = Column(Float, default=0.0)
    time_to_resolution_hours = Column(Float, nullable=True)
    question_snapshot = Column(Text, default="")
    category = Column(String(128), default="")
    reasons_json = Column(JSON, default=list)
    meta_json = Column(JSON, default=dict)


class LabRuntimeStatusRow(Base):
    __tablename__ = "lab_runtime_status"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String(32), nullable=False, default="shadow_maker")
    started_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    last_cycle_ts = Column(DateTime, nullable=True)
    last_market_refresh_ts = Column(DateTime, nullable=True)
    last_signal_tick_ts = Column(DateTime, nullable=True)
    last_cycle_ok = Column(Boolean, default=True)
    last_cycle_error = Column(Text, nullable=True)
    cycle_failures_in_row = Column(Integer, default=0)
    ws_connected = Column(Boolean, default=False)
    markets_fetched_last = Column(Integer, default=0)
    eligible_markets_last = Column(Integer, default=0)
    subscribed_tokens_last = Column(Integer, default=0)


class LabWsMetricRow(Base):
    __tablename__ = "lab_ws_metrics"
    __table_args__ = (
        Index("ix_lab_ws_metrics_ts", "timestamp"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, index=True)
    connected = Column(Boolean, default=False)
    reconnect_count = Column(Integer, default=0)
    disconnect_count = Column(Integer, default=0)
    error_count = Column(Integer, default=0)
    message_count = Column(Integer, default=0)
    messages_per_minute = Column(Float, default=0.0)
    last_message_age_sec = Column(Float, default=0.0)
    gap_count = Column(Integer, default=0)
    max_gap_sec = Column(Float, default=0.0)
    total_gaps_sec = Column(Float, default=0.0)
    health_score = Column(Float, default=0.0)
    is_stale = Column(Boolean, default=False)
    subscribed_tokens = Column(Integer, default=0)
    heartbeat_interval = Column(Float, default=0.0)
    entries_frozen = Column(Boolean, default=False)
    forced_taker_exit_count = Column(Integer, default=0)
    exit_fill_count = Column(Integer, default=0)
    forced_taker_exit_ratio = Column(Float, default=0.0)
    maker_fill_ratio = Column(Float, default=0.0)
    avg_quote_age_sec = Column(Float, default=0.0)
    avg_reprice_count = Column(Float, default=0.0)
    open_working_orders = Column(Integer, default=0)
    extra_json = Column(JSON, default=dict)


class ResearchModelArtifactRow(Base):
    __tablename__ = "research_model_artifacts"
    __table_args__ = (
        Index("ix_research_model_artifacts_created", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    artifact_key = Column(String(128), unique=True, nullable=False, index=True)
    model_type = Column(String(64), nullable=False, default="logistic_regression")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    artifact_path = Column(String(512), nullable=False)
    manifest_path = Column(String(512), nullable=False)
    metrics_json = Column(JSON, default=dict)
    holdout_summary_json = Column(JSON, default=list)
    accepted = Column(Boolean, default=False)
    enabled = Column(Boolean, default=False)
    training_fresh_until = Column(DateTime, nullable=True)
    high_conf_accuracy = Column(Float, default=0.0)
    high_conf_net_ev = Column(Float, default=0.0)
    calibration_error = Column(Float, default=0.0)


class ResearchMotifRow(Base):
    __tablename__ = "research_motifs"
    __table_args__ = (
        Index("ix_research_motifs_artifact", "artifact_key"),
        Index("ix_research_motifs_confidence", "confidence_score"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    artifact_key = Column(String(128), nullable=False, index=True)
    motif_key = Column(String(128), nullable=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    feature_signature = Column(JSON, default=dict)
    pre_event_window = Column(JSON, default=dict)
    time_lag_sec = Column(Integer, default=0)
    sample_size = Column(Integer, default=0)
    hit_rate = Column(Float, default=0.0)
    expected_value = Column(Float, default=0.0)
    confidence_score = Column(Float, default=0.0)
    holdout_metrics = Column(JSON, default=dict)
