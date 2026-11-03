"""Adapter for the official ``MetaTrader5`` Python package (Windows only).

This is a thin translation layer: it converts the package's named tuples into
our typed dataclasses and never adds trading logic.  The package is imported
lazily so that the rest of the application (and the test-suite) runs on any
platform.  All calls must happen on the single MT5 worker thread.
"""
from __future__ import annotations

import glob
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from tgmt5.mt5.interface import (
    AccountInfo, ConnectParams, Deal, GatewayError, PendingOrder, Position, Retcode, SymbolSpec,
    TerminalInfo, Tick, TradeRequest, TradeResult,
)

log = logging.getLogger(__name__)


def detect_terminals() -> list[str]:
    """Find installed MT5 terminals (terminal64.exe) on Windows."""
    found: list[str] = []
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
    for root in roots:
        if root:
            found += glob.glob(os.path.join(root, "*", "terminal64.exe"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        for origin in glob.glob(os.path.join(appdata, "MetaQuotes", "Terminal", "*", "origin.txt")):
            try:
                p = Path(origin).read_text(encoding="utf-16").strip()
                exe = os.path.join(p, "terminal64.exe")
                if os.path.exists(exe):
                    found.append(exe)
            except (OSError, UnicodeError):
                continue
    seen: set[str] = set()
    return [f for f in found if not (f.lower() in seen or seen.add(f.lower()))]


class RealMT5Gateway:
    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - platform dependent
            raise GatewayError(
                "The MetaTrader5 package is not available. It requires Windows and "
                "`pip install MetaTrader5`.") from exc
        self.mt5 = mt5
        self._connected = False

    def _err(self, what: str) -> GatewayError:
        code, desc = self.mt5.last_error()
        return GatewayError(f"{what} failed: [{code}] {desc}")

    # ------------------------------------------------------------ session
    def connect(self, params: ConnectParams) -> AccountInfo:
        kwargs: dict = {"timeout": params.timeout_ms}
        if params.terminal_path:
            kwargs["path"] = params.terminal_path
        if params.login:
            kwargs["login"] = int(params.login)
        if params.password:
            kwargs["password"] = params.password
        if params.server:
            kwargs["server"] = params.server
        if not self.mt5.initialize(**kwargs):
            raise self._err("MT5 initialize")
        self._connected = True
        acct = self.account_info()
        if acct is None:
            raise self._err("account_info")
        return acct

    def disconnect(self) -> None:
        if self._connected:
            self.mt5.shutdown()
        self._connected = False

    def is_connected(self) -> bool:
        if not self._connected:
            return False
        ti = self.mt5.terminal_info()
        return bool(ti and ti.connected)

    def account_info(self) -> AccountInfo | None:
        a = self.mt5.account_info()
        if a is None:
            return None
        return AccountInfo(login=a.login, server=a.server, company=a.company, currency=a.currency,
                           balance=a.balance, equity=a.equity, margin=a.margin,
                           margin_free=a.margin_free, leverage=a.leverage, trade_mode=a.trade_mode,
                           margin_mode=a.margin_mode, trade_allowed=bool(a.trade_allowed),
                           name=a.name)

    def terminal_info(self) -> TerminalInfo | None:
        t = self.mt5.terminal_info()
        if t is None:
            return None
        return TerminalInfo(connected=bool(t.connected), trade_allowed=bool(t.trade_allowed),
                            path=t.path, name=t.name, ping_last_us=int(getattr(t, "ping_last", 0)))

    # ------------------------------------------------------------ symbols
    def symbols(self) -> list[str]:
        syms = self.mt5.symbols_get()
        return [s.name for s in syms] if syms else []

    def symbol_select(self, symbol: str) -> bool:
        return bool(self.mt5.symbol_select(symbol, True))

    def symbol_spec(self, symbol: str) -> SymbolSpec | None:
        s = self.mt5.symbol_info(symbol)
        if s is None:
            return None
        return SymbolSpec(
            name=s.name, digits=s.digits, point=s.point, trade_tick_size=s.trade_tick_size,
            trade_tick_value=s.trade_tick_value, trade_tick_value_loss=s.trade_tick_value_loss,
            trade_contract_size=s.trade_contract_size, volume_min=s.volume_min,
            volume_max=s.volume_max, volume_step=s.volume_step,
            trade_stops_level=s.trade_stops_level, trade_freeze_level=s.trade_freeze_level,
            filling_mode=s.filling_mode, currency_profit=s.currency_profit,
            currency_margin=s.currency_margin, trade_mode=s.trade_mode, visible=s.visible)

    def tick(self, symbol: str) -> Tick | None:
        t = self.mt5.symbol_info_tick(symbol)
        if t is None or (t.bid == 0 and t.ask == 0):
            return None
        return Tick(bid=t.bid, ask=t.ask, time=float(t.time))

    def calc_profit(self, order_type: int, symbol: str, volume: float, price_open: float,
                    price_close: float) -> float | None:
        v = self.mt5.order_calc_profit(int(order_type), symbol, float(volume), float(price_open),
                                       float(price_close))
        return None if v is None else float(v)

    def calc_margin(self, order_type: int, symbol: str, volume: float, price: float) -> float | None:
        v = self.mt5.order_calc_margin(int(order_type), symbol, float(volume), float(price))
        return None if v is None else float(v)

    # ------------------------------------------------------------ trading state
    def positions(self, symbol: str | None = None) -> list[Position]:
        res = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        if res is None:
            raise self._err("positions_get")
        return [Position(ticket=p.ticket, symbol=p.symbol, type=p.type, volume=p.volume,
                         price_open=p.price_open, price_current=p.price_current, sl=p.sl, tp=p.tp,
                         profit=p.profit, magic=p.magic, comment=p.comment, time=float(p.time),
                         identifier=p.identifier, swap=p.swap) for p in res]

    def orders(self, symbol: str | None = None) -> list[PendingOrder]:
        res = self.mt5.orders_get(symbol=symbol) if symbol else self.mt5.orders_get()
        if res is None:
            raise self._err("orders_get")
        return [PendingOrder(ticket=o.ticket, symbol=o.symbol, type=o.type,
                             volume_initial=o.volume_initial, volume_current=o.volume_current,
                             price_open=o.price_open, sl=o.sl, tp=o.tp, magic=o.magic,
                             comment=o.comment, time_setup=float(o.time_setup),
                             time_expiration=float(o.time_expiration)) for o in res]

    def history_deals(self, date_from: float, date_to: float) -> list[Deal]:
        res = self.mt5.history_deals_get(datetime.fromtimestamp(date_from, timezone.utc),
                                         datetime.fromtimestamp(date_to, timezone.utc))
        if res is None:
            raise self._err("history_deals_get")
        return [Deal(ticket=d.ticket, order=d.order, position_id=d.position_id, symbol=d.symbol,
                     type=d.type, entry=d.entry, volume=d.volume, price=d.price, profit=d.profit,
                     commission=d.commission, swap=d.swap, magic=d.magic, comment=d.comment,
                     time=float(d.time)) for d in res]

    def send(self, request: TradeRequest) -> TradeResult:
        r = self.mt5.order_send(request.as_dict())
        if r is None:
            code, desc = self.mt5.last_error()
            # No result object: we cannot know whether the server received it -> uncertain.
            return TradeResult(retcode=Retcode.ERROR, comment=f"order_send returned None [{code}] {desc}")
        return TradeResult(retcode=r.retcode, order=r.order, deal=r.deal, volume=r.volume,
                           price=r.price, comment=r.comment, request_id=r.request_id)
