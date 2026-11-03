"""Typed broker gateway interface.

Every implementation (real MetaTrader5, the in-memory fake used by tests, and
the paper-trading simulator) exposes exactly this API.  Values mirror the
official ``MetaTrader5`` Python package so that the real adapter is a thin
translation layer.  All methods are synchronous and must be called from the
single MT5 worker thread (the official package is not thread-safe).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# --- constants mirroring the MetaTrader5 package --------------------------
ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2

ACCOUNT_MARGIN_MODE_RETAIL_NETTING = 0
ACCOUNT_MARGIN_MODE_EXCHANGE = 1
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

ORDER_TIME_GTC = 0
ORDER_TIME_SPECIFIED = 2

TRADE_ACTION_DEAL = 1
TRADE_ACTION_PENDING = 5
TRADE_ACTION_SLTP = 6
TRADE_ACTION_MODIFY = 7
TRADE_ACTION_REMOVE = 8

SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_FULL = 4


class OrderType(enum.IntEnum):
    BUY = 0
    SELL = 1
    BUY_LIMIT = 2
    SELL_LIMIT = 3
    BUY_STOP = 4
    SELL_STOP = 5


class Retcode(enum.IntEnum):
    REQUOTE = 10004
    REJECT = 10006
    CANCEL = 10007
    PLACED = 10008
    DONE = 10009
    DONE_PARTIAL = 10010
    ERROR = 10011
    TIMEOUT = 10012
    INVALID = 10013
    INVALID_VOLUME = 10014
    INVALID_PRICE = 10015
    INVALID_STOPS = 10016
    TRADE_DISABLED = 10017
    MARKET_CLOSED = 10018
    NO_MONEY = 10019
    PRICE_CHANGED = 10020
    PRICE_OFF = 10021
    INVALID_EXPIRATION = 10022
    ORDER_CHANGED = 10023
    TOO_MANY_REQUESTS = 10024
    NO_CHANGES = 10025
    SERVER_DISABLES_AT = 10026
    CLIENT_DISABLES_AT = 10027
    LOCKED = 10028
    FROZEN = 10029
    INVALID_FILL = 10030
    CONNECTION = 10031
    ONLY_REAL = 10032
    LIMIT_ORDERS = 10033
    LIMIT_VOLUME = 10034
    INVALID_ORDER = 10035
    POSITION_CLOSED = 10036
    CLOSE_ORDER_EXIST = 10039
    LIMIT_POSITIONS = 10040


RETCODE_TEXT: dict[int, str] = {
    10004: "Requote", 10006: "Request rejected by broker", 10007: "Request cancelled",
    10008: "Order placed", 10009: "Done", 10010: "Partially filled", 10011: "Request processing error",
    10012: "Request timed out", 10013: "Invalid request", 10014: "Invalid volume",
    10015: "Invalid price", 10016: "Invalid stops (SL/TP too close or wrong side)",
    10017: "Trading disabled for symbol", 10018: "Market closed", 10019: "Not enough money",
    10020: "Price changed", 10021: "No quotes to process request", 10022: "Invalid expiration",
    10023: "Order state changed", 10024: "Too many requests", 10025: "No changes in request",
    10026: "Autotrading disabled by server", 10027: "Algo Trading disabled in the terminal",
    10028: "Request locked", 10029: "Order or position frozen", 10030: "Unsupported filling mode",
    10031: "No connection to trade server", 10032: "Operation allowed only for live accounts",
    10033: "Pending order limit reached", 10034: "Volume limit reached", 10035: "Invalid order type",
    10036: "Position already closed", 10039: "Close order already exists",
    10040: "Position limit reached",
}

# Codes after which the outcome of the request is not known and MUST be reconciled.
UNCERTAIN_RETCODES = frozenset({Retcode.TIMEOUT, Retcode.CONNECTION, Retcode.ERROR})


@dataclass(frozen=True, slots=True)
class AccountInfo:
    login: int
    server: str
    company: str
    currency: str
    balance: float
    equity: float
    margin: float
    margin_free: float
    leverage: int
    trade_mode: int
    margin_mode: int
    trade_allowed: bool = True
    name: str = ""

    @property
    def is_demo(self) -> bool:
        return self.trade_mode == ACCOUNT_TRADE_MODE_DEMO

    @property
    def is_hedging(self) -> bool:
        return self.margin_mode == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING


@dataclass(frozen=True, slots=True)
class TerminalInfo:
    connected: bool
    trade_allowed: bool
    path: str = ""
    name: str = ""
    ping_last_us: int = 0


@dataclass(frozen=True, slots=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value: float
    trade_tick_value_loss: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    trade_stops_level: int
    trade_freeze_level: int
    filling_mode: int
    currency_profit: str
    currency_margin: str
    trade_mode: int = SYMBOL_TRADE_MODE_FULL
    visible: bool = True


@dataclass(frozen=True, slots=True)
class Tick:
    bid: float
    ask: float
    time: float  # epoch seconds

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class Position:
    ticket: int
    symbol: str
    type: int           # 0 buy, 1 sell
    volume: float
    price_open: float
    price_current: float
    sl: float
    tp: float
    profit: float
    magic: int
    comment: str
    time: float
    identifier: int = 0
    swap: float = 0.0


@dataclass(frozen=True, slots=True)
class PendingOrder:
    ticket: int
    symbol: str
    type: int
    volume_initial: float
    volume_current: float
    price_open: float
    sl: float
    tp: float
    magic: int
    comment: str
    time_setup: float
    time_expiration: float = 0.0


@dataclass(frozen=True, slots=True)
class Deal:
    ticket: int
    order: int
    position_id: int
    symbol: str
    type: int
    entry: int          # 0 in, 1 out, 2 inout, 3 out_by
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    magic: int
    comment: str
    time: float


@dataclass(frozen=True, slots=True)
class TradeRequest:
    action: int
    symbol: str = ""
    volume: float = 0.0
    type: int = 0
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    deviation: int = 0
    magic: int = 0
    comment: str = ""
    type_filling: int = ORDER_FILLING_FOK
    type_time: int = ORDER_TIME_GTC
    expiration: int = 0
    position: int = 0
    order: int = 0

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__slots__}
        keep = ("action", "type", "type_filling", "type_time")
        return {k: v for k, v in d.items() if v not in (0, 0.0, "") or k in keep}


@dataclass(frozen=True, slots=True)
class TradeResult:
    retcode: int
    order: int = 0
    deal: int = 0
    volume: float = 0.0
    price: float = 0.0
    comment: str = ""
    request_id: int = 0

    @property
    def ok(self) -> bool:
        return self.retcode in (Retcode.DONE, Retcode.PLACED, Retcode.DONE_PARTIAL)

    @property
    def uncertain(self) -> bool:
        return self.retcode in UNCERTAIN_RETCODES

    @property
    def text(self) -> str:
        return RETCODE_TEXT.get(self.retcode, f"retcode {self.retcode}") + (
            f" ({self.comment})" if self.comment else "")


class GatewayError(RuntimeError):
    """Raised for adapter-level failures (not broker rejections)."""


class LiveAccountBlocked(GatewayError):
    """Raised whenever a non-DEMO account is detected."""


@dataclass
class ConnectParams:
    terminal_path: str = ""
    login: int | None = None
    password: str | None = field(default=None, repr=False)
    server: str = ""
    timeout_ms: int = 60_000


@runtime_checkable
class MT5Gateway(Protocol):
    """Broker gateway; see module docstring."""

    def connect(self, params: ConnectParams) -> AccountInfo: ...
    def disconnect(self) -> None: ...
    def is_connected(self) -> bool: ...
    def account_info(self) -> AccountInfo | None: ...
    def terminal_info(self) -> TerminalInfo | None: ...
    def symbols(self) -> list[str]: ...
    def symbol_spec(self, symbol: str) -> SymbolSpec | None: ...
    def symbol_select(self, symbol: str) -> bool: ...
    def tick(self, symbol: str) -> Tick | None: ...
    def calc_profit(self, order_type: int, symbol: str, volume: float,
                    price_open: float, price_close: float) -> float | None: ...
    def calc_margin(self, order_type: int, symbol: str, volume: float, price: float) -> float | None: ...
    def positions(self, symbol: str | None = None) -> list[Position]: ...
    def orders(self, symbol: str | None = None) -> list[PendingOrder]: ...
    def history_deals(self, date_from: float, date_to: float) -> list[Deal]: ...
    def send(self, request: TradeRequest) -> TradeResult: ...
