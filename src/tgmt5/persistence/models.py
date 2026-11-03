"""SQLAlchemy ORM schema.  Migrations live in ``persistence/migrations``."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)


class TelegramMessageRow(Base):
    __tablename__ = "telegram_messages"
    __table_args__ = (UniqueConstraint("source_id", "message_id", name="uq_source_message"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)
    source_title: Mapped[str] = mapped_column(String(255), default="")
    message_id: Mapped[int] = mapped_column(Integer)
    reply_to_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    edit_count: Mapped[int] = mapped_column(Integer, default=0)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class SignalRow(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_source_status", "source_id", "status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_row_id: Mapped[int] = mapped_column(ForeignKey("telegram_messages.id"), index=True)
    source_id: Mapped[int] = mapped_column(Integer)
    message_id: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))          # MessageKind
    status: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    broker_symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    direction: Mapped[str | None] = mapped_column(String(4), nullable=True)
    entry_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profits: Mapped[list] = mapped_column(JSON, default=list)
    management: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    parent_signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    lot_total: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_money: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                                 onupdate=utcnow)

    message: Mapped[TelegramMessageRow] = relationship()
    executions: Mapped[list["ExecutionRow"]] = relationship(back_populates="signal",
                                                            order_by="ExecutionRow.leg_index")
    events: Mapped[list["SignalEventRow"]] = relationship(back_populates="signal",
                                                          order_by="SignalEventRow.id")


class SignalEventRow(Base):
    """Audit trail: every state transition / decision of a signal."""
    __tablename__ = "signal_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status_from: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status_to: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    signal: Mapped[SignalRow] = relationship(back_populates="events")


class ExecutionRow(Base):
    """One broker order (a 'leg') belonging to a signal."""
    __tablename__ = "executions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    leg_index: Mapped[int] = mapped_column(Integer, default=0)
    client_tag: Mapped[str] = mapped_column(String(31), unique=True)   # MT5 comment, <=31 chars
    state: Mapped[str] = mapped_column(String(16), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    direction: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[int] = mapped_column(Integer)
    volume: Mapped[float] = mapped_column(Float)
    requested_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_money: Mapped[float | None] = mapped_column(Float, nullable=True)
    retcode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    broker_comment: Mapped[str] = mapped_column(Text, default="")
    order_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    position_ticket: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    account_login: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    spread_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage_points: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_volume: Mapped[float] = mapped_column(Float, default=0.0)
    close_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    be_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    signal: Mapped[SignalRow] = relationship(back_populates="executions")


class AppEventRow(Base):
    """Searchable activity / error log (never contains credentials)."""
    __tablename__ = "app_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), index=True)
    category: Mapped[str] = mapped_column(String(24), index=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    balance: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=False)
