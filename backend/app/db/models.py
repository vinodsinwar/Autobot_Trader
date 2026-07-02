"""SQLAlchemy ORM models.

Design notes:
- Every inbound message, parse attempt, risk decision, order and fill is
  persisted; the `events` table is the append-only audit log behind reporting.
- Enums are stored as strings for painless migrations and readable SQL.
"""
import enum
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON}


# ---------------------------------------------------------------------------
# enums (stored as plain strings)
# ---------------------------------------------------------------------------

class SignalSource(enum.StrEnum):
    TELEGRAM = "telegram"
    YOUTUBE = "youtube"
    MANUAL = "manual"


class SignalState(enum.StrEnum):
    RECEIVED = "received"
    PARSED = "parsed"
    PARSE_FAILED = "parse_failed"
    RISK_APPROVED = "risk_approved"
    RISK_REJECTED = "risk_rejected"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVAL_REJECTED = "approval_rejected"
    EXECUTING = "executing"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    ERROR = "error"


class Broker(enum.StrEnum):
    DHAN = "dhan"
    DELTA = "delta"
    PAPER = "paper"


class OrderLeg(enum.StrEnum):
    ENTRY = "entry"
    TARGET = "target"
    STOP_LOSS = "stop_loss"


class OrderStatus(enum.StrEnum):
    PENDING = "pending"
    PLACED = "placed"
    PART_FILLED = "part_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    ERROR = "error"


class PositionStatus(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ExecutionMode(enum.StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# core pipeline tables
# ---------------------------------------------------------------------------

class Channel(Base):
    """A watched Telegram channel/group."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_chat_id: Mapped[int] = mapped_column(unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # which broker/market signals from this channel target: dhan | delta | paper
    broker: Mapped[str] = mapped_column(String(16), default=Broker.PAPER.value)
    execution_mode: Mapped[str] = mapped_column(String(16), default=ExecutionMode.MANUAL.value)
    # name of the rule pack used by the deterministic parser
    rule_pack: Mapped[str] = mapped_column(String(64), default="generic")
    llm_fallback: Mapped[bool] = mapped_column(Boolean, default=True)
    # sizing config e.g. {"mode": "lots", "value": 1} or {"mode": "capital", "value": 10000}
    sizing: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    raw_messages: Mapped[list["RawMessage"]] = relationship(back_populates="channel")


class RawMessage(Base):
    """Every Telegram message (and every edit of it) verbatim."""

    __tablename__ = "raw_messages"
    __table_args__ = (
        UniqueConstraint("channel_id", "tg_message_id", "edit_version", name="uq_msg_edit"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    tg_message_id: Mapped[int] = mapped_column(Integer)
    edit_version: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    channel: Mapped["Channel"] = relationship(back_populates="raw_messages")
    signals: Mapped[list["Signal"]] = relationship(back_populates="raw_message")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String(32), default=SignalState.RECEIVED.value, index=True)
    raw_message_id: Mapped[int | None] = mapped_column(ForeignKey("raw_messages.id"), nullable=True)
    yt_extracted_signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("yt_extracted_signals.id"), nullable=True
    )
    # validated ParsedSignal dump (see app.parsing.schema)
    parsed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    parser: Mapped[str] = mapped_column(String(16), default="")  # rules | llm | relay | manual
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    broker: Mapped[str] = mapped_column(String(16), default=Broker.PAPER.value)
    error: Mapped[str] = mapped_column(Text, default="")
    # content hash for duplicate suppression
    dedup_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    raw_message: Mapped["RawMessage | None"] = relationship(back_populates="signals")
    orders: Mapped[list["Order"]] = relationship(back_populates="signal")
    position: Mapped["Position | None"] = relationship(back_populates="signal", uselist=False)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    broker: Mapped[str] = mapped_column(String(16))
    correlation_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    broker_order_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    leg: Mapped[str] = mapped_column(String(16), default=OrderLeg.ENTRY.value)
    symbol: Mapped[str] = mapped_column(String(64), default="")
    security_id: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8), default="BUY")
    qty: Mapped[float] = mapped_column(Float, default=0)
    filled_qty: Mapped[float] = mapped_column(Float, default=0)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trigger_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_type: Mapped[str] = mapped_column(String(24), default="LIMIT")
    status: Mapped[str] = mapped_column(String(24), default=OrderStatus.PENDING.value, index=True)
    status_reason: Mapped[str] = mapped_column(Text, default="")
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    signal: Mapped["Signal | None"] = relationship(back_populates="orders")
    trades: Mapped[list["Trade"]] = relationship(back_populates="order")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    broker_trade_id: Mapped[str] = mapped_column(String(64), default="")
    qty: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    order: Mapped["Order"] = relationship(back_populates="trades")


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), index=True)
    broker: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(64))
    security_id: Mapped[str] = mapped_column(String(64), default="")
    side: Mapped[str] = mapped_column(String(8), default="BUY")
    qty: Mapped[float] = mapped_column(Float, default=0)
    avg_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default=PositionStatus.OPEN.value, index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    signal: Mapped["Signal | None"] = relationship(back_populates="position")


class EventLog(Base):
    """Append-only audit trail of every state transition and decision."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    category: Mapped[str] = mapped_column(String(32), index=True)  # signal|order|risk|system|youtube
    name: Mapped[str] = mapped_column(String(64), index=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Setting(Base):
    """Key/value store for dashboard-editable configuration.

    Secret values are Fernet-encrypted (encrypted=True) before storage.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Instrument(Base):
    """Instrument master (Dhan scrip master rows + Delta products)."""

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("broker", "security_id", name="uq_instrument"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    broker: Mapped[str] = mapped_column(String(16), index=True)
    security_id: Mapped[str] = mapped_column(String(64), index=True)
    exchange_segment: Mapped[str] = mapped_column(String(32), default="")
    symbol: Mapped[str] = mapped_column(String(128), index=True)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    instrument_type: Mapped[str] = mapped_column(String(32), default="")  # EQ|FUT|CE|PE|PERP
    underlying: Mapped[str] = mapped_column(String(64), default="", index=True)
    expiry: Mapped[str] = mapped_column(String(32), default="")
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    lot_size: Mapped[float] = mapped_column(Float, default=1)
    tick_size: Mapped[float] = mapped_column(Float, default=0.05)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DailyStat(Base):
    __tablename__ = "daily_stats"
    __table_args__ = (UniqueConstraint("date", "source", "channel_key", name="uq_daily_stat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    source: Mapped[str] = mapped_column(String(16), default="all")
    channel_key: Mapped[str] = mapped_column(String(64), default="all")
    signals: Mapped[int] = mapped_column(Integer, default=0)
    executed: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    pnl: Mapped[float] = mapped_column(Float, default=0.0)


# ---------------------------------------------------------------------------
# YouTube source tables
# ---------------------------------------------------------------------------

class YtChannel(Base):
    __tablename__ = "yt_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String(255))
    channel_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    handle: Mapped[str] = mapped_column(String(128), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    execution_mode: Mapped[str] = mapped_column(String(16), default=ExecutionMode.MANUAL.value)
    broker: Mapped[str] = mapped_column(String(16), default=Broker.PAPER.value)
    poll_interval_s: Mapped[int] = mapped_column(Integer, default=60)
    sizing: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # compiled calibration profile: few-shot examples, lexicon patterns, metrics
    calibration: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    streams: Mapped[list["YtStream"]] = relationship(back_populates="yt_channel")


class YtStream(Base):
    """A live capture session or a processed VOD."""

    __tablename__ = "yt_streams"

    id: Mapped[int] = mapped_column(primary_key=True)
    yt_channel_id: Mapped[int] = mapped_column(ForeignKey("yt_channels.id"), index=True)
    video_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(8), default="live")  # live | vod
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    # pending | capturing | transcribing | extracting | review | done | error
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    yt_channel: Mapped["YtChannel"] = relationship(back_populates="streams")
    segments: Mapped[list["YtTranscriptSegment"]] = relationship(back_populates="stream")
    extracted: Mapped[list["YtExtractedSignal"]] = relationship(back_populates="stream")


class YtTranscriptSegment(Base):
    __tablename__ = "yt_transcript_segments"

    id: Mapped[int] = mapped_column(primary_key=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("yt_streams.id"), index=True)
    t_start: Mapped[float] = mapped_column(Float)  # seconds into the stream
    t_end: Mapped[float] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    stream: Mapped["YtStream"] = relationship(back_populates="segments")


class YtExtractedSignal(Base):
    __tablename__ = "yt_extracted_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("yt_streams.id"), index=True)
    transcript_excerpt: Mapped[str] = mapped_column(Text, default="")
    t_in_stream: Mapped[float] = mapped_column(Float, default=0.0)
    parsed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    dedup_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    relay_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # per-stage latency instrumentation (spoken_at, transcribed_at, extracted_at, relayed_at)
    latency: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    stream: Mapped["YtStream"] = relationship(back_populates="extracted")
    feedback: Mapped[list["YtFeedback"]] = relationship(back_populates="extracted_signal")


class YtFeedback(Base):
    """User labels from the calibration studio: correct / wrong / missed."""

    __tablename__ = "yt_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    stream_id: Mapped[int] = mapped_column(ForeignKey("yt_streams.id"), index=True)
    yt_extracted_signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("yt_extracted_signals.id"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(16))  # correct | wrong | missed
    excerpt: Mapped[str] = mapped_column(Text, default="")
    corrected: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    extracted_signal: Mapped["YtExtractedSignal | None"] = relationship(back_populates="feedback")
