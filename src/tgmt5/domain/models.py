"""Core domain types shared by every layer.

These are plain, immutable-where-possible dataclasses and enums.  They carry no
I/O and no framework dependencies so that the parser, risk engine and execution
engine can be unit-tested in isolation.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class Direction(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Direction.BUY else -1


class EntryType(str, enum.Enum):
    MARKET = "MARKET"          # "NOW", or explicit price close to market
    PRICE = "PRICE"            # single price "@ 1.0850" without explicit order type
    ZONE = "ZONE"              # entry range "2700-2705"
    LIMIT = "LIMIT"            # explicit "BUY LIMIT"
    STOP = "STOP"              # explicit "BUY STOP"


class MessageKind(str, enum.Enum):
    NEW_SIGNAL = "NEW_SIGNAL"
    MANAGEMENT = "MANAGEMENT"
    INFO = "INFO"              # e.g. "TP1 HIT" - recognised but not actionable by default
    NOISE = "NOISE"            # general chat / promo
    AMBIGUOUS = "AMBIGUOUS"    # looks trade-related but cannot be interpreted reliably


class ManagementType(str, enum.Enum):
    MOVE_SL_BE = "MOVE_SL_BE"
    MODIFY_SL = "MODIFY_SL"
    MODIFY_TP = "MODIFY_TP"
    CLOSE_ALL = "CLOSE_ALL"
    CLOSE_PARTIAL = "CLOSE_PARTIAL"
    CANCEL_PENDING = "CANCEL_PENDING"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"


class SignalStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    PARSED = "PARSED"
    VALIDATED = "VALIDATED"
    QUEUED = "QUEUED"
    WAITING = "WAITING"            # waiting for price to enter zone
    SUBMITTING = "SUBMITTING"      # order request persisted, about to be / being sent
    PENDING_PLACED = "PENDING_PLACED"
    EXECUTED = "EXECUTED"
    PARTIAL = "PARTIAL"            # some legs filled, others failed
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNCERTAIN = "UNCERTAIN"        # outcome unknown - must be reconciled before anything else
    MONITORED = "MONITORED"        # monitor-only / not executed by design
    SIMULATED = "SIMULATED"        # paper-traded

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES

    @property
    def is_active_trade(self) -> bool:
        return self in (SignalStatus.EXECUTED, SignalStatus.PARTIAL,
                        SignalStatus.PENDING_PLACED, SignalStatus.SIMULATED)


TERMINAL_STATUSES = frozenset({
    SignalStatus.CLOSED, SignalStatus.REJECTED, SignalStatus.EXPIRED,
    SignalStatus.FAILED, SignalStatus.CANCELLED, SignalStatus.MONITORED,
})


class ExecutionMode(str, enum.Enum):
    MONITOR = "MONITOR"    # parse + validate only, never send orders (safe default)
    PAPER = "PAPER"        # simulate fills locally with real prices
    DEMO = "DEMO"          # send orders to a verified MT5 DEMO account
    LIVE = "LIVE"          # "REAL ACCOUNT": send orders to a real-money account (explicit opt-in + typed confirmation)


class MessageEventType(str, enum.Enum):
    NEW = "NEW"
    EDIT = "EDIT"
    DELETE = "DELETE"


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    """Normalised Telegram event as produced by any Telegram adapter."""
    source_id: int
    message_id: int
    text: str
    date: datetime                      # message timestamp (UTC, tz-aware)
    event: MessageEventType = MessageEventType.NEW
    reply_to_message_id: int | None = None
    edit_date: datetime | None = None
    source_title: str = ""
    received_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ParsedSignal:
    direction: Direction
    symbol: str                         # canonical symbol, e.g. XAUUSD
    entry_type: EntryType
    stop_loss: float
    take_profits: tuple[float, ...]
    entry_price: float | None = None    # single price
    entry_low: float | None = None      # zone
    entry_high: float | None = None
    raw_symbol: str = ""

    @property
    def reference_entry(self) -> float | None:
        if self.entry_price is not None:
            return self.entry_price
        if self.entry_low is not None and self.entry_high is not None:
            return (self.entry_low + self.entry_high) / 2
        return None


@dataclass(frozen=True, slots=True)
class ManagementInstruction:
    type: ManagementType
    symbol: str | None = None
    fraction: float | None = None       # CLOSE_PARTIAL: 0 < fraction < 1
    price: float | None = None          # MODIFY_SL / MODIFY_TP
    tp_index: int | None = None         # TP_HIT n


@dataclass(frozen=True, slots=True)
class ParseResult:
    kind: MessageKind
    signal: ParsedSignal | None = None
    management: ManagementInstruction | None = None
    reasons: tuple[str, ...] = ()       # why ambiguous / noise; diagnostics
    confidence: float = 1.0

    @property
    def needs_review(self) -> bool:
        return self.kind is MessageKind.AMBIGUOUS


@dataclass(slots=True)
class Decision:
    """Outcome of a risk/validation evaluation, fully auditable."""
    approved: bool
    reason: str = ""
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, ok, detail))
        if not ok and self.approved:
            self.approved = False
            self.reason = f"{name}: {detail}" if detail else name
        return ok
