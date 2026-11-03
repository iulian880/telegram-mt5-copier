"""In-memory broker simulator.

Used for two purposes:

* :class:`FakeMT5Gateway` - deterministic broker for automated tests, with
  fault injection (rejections, timeouts, "executed but response lost").
* :class:`PaperGateway`  - paper-trading mode: real prices/specs from a market
  data source (the connected MT5 terminal), simulated fills with configurable
  extra spread, slippage and commission.  Paper results are *not* evidence of
  realistic execution and are stored/flagged separately.

The simulator implements the same :class:`MT5Gateway` protocol as the real
adapter and enforces the same broker constraints (volume min/max/step, stops
level, side of SL/TP, filling mode, netting vs hedging).
"""
from __future__ import annotations

import itertools
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace

from tgmt5.mt5.interface import (
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING, ACCOUNT_TRADE_MODE_DEMO, ORDER_FILLING_FOK,
    ORDER_FILLING_IOC, ORDER_FILLING_RETURN, SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_MODE_DISABLED, TRADE_ACTION_DEAL, TRADE_ACTION_MODIFY, TRADE_ACTION_PENDING,
    TRADE_ACTION_REMOVE, TRADE_ACTION_SLTP, AccountInfo, ConnectParams, Deal, GatewayError,
    MT5Gateway, OrderType, PendingOrder, Position, Retcode, SymbolSpec, TerminalInfo, Tick,
    TradeRequest, TradeResult,
)


def fx_spec(name: str, digits: int = 5, contract: float = 100_000, profit_ccy: str = "USD",
            margin_ccy: str | None = None, stops_level: int = 0) -> SymbolSpec:
    point = 10 ** -digits
    return SymbolSpec(
        name=name, digits=digits, point=point, trade_tick_size=point,
        trade_tick_value=contract * point, trade_tick_value_loss=contract * point,
        trade_contract_size=contract, volume_min=0.01, volume_max=100.0, volume_step=0.01,
        trade_stops_level=stops_level, trade_freeze_level=0,
        filling_mode=SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC,
        currency_profit=profit_ccy, currency_margin=margin_ccy or name[:3])


def gold_spec(name: str = "XAUUSD", contract: float = 100, digits: int = 2) -> SymbolSpec:
    point = 10 ** -digits
    return SymbolSpec(
        name=name, digits=digits, point=point, trade_tick_size=point,
        trade_tick_value=contract * point, trade_tick_value_loss=contract * point,
        trade_contract_size=contract, volume_min=0.01, volume_max=100.0, volume_step=0.01,
        trade_stops_level=0, trade_freeze_level=0,
        filling_mode=SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC,
        currency_profit="USD", currency_margin="XAU")


@dataclass
class _Pos:
    ticket: int
    symbol: str
    type: int
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    comment: str
    time: float


@dataclass
class _Ord:
    ticket: int
    symbol: str
    type: int
    volume: float
    price_open: float
    sl: float
    tp: float
    magic: int
    comment: str
    time_setup: float
    expiration: float


class SimBroker(MT5Gateway):
    def __init__(self, *, balance: float = 10_000.0, currency: str = "USD", leverage: int = 500,
                 hedging: bool = True, trade_mode: int = ACCOUNT_TRADE_MODE_DEMO,
                 login: int = 50001234, server: str = "ICMarketsSC-Demo",
                 company: str = "Raw Trading Ltd", market: MT5Gateway | None = None,
                 extra_spread_points: float = 0.0, slippage_points: float = 0.0,
                 commission_per_lot: float = 0.0) -> None:
        self._lock = threading.RLock()
        self.market = market
        self.balance = balance
        self.currency = currency
        self.leverage = leverage
        self.hedging = hedging
        self.trade_mode = trade_mode
        self.login = login
        self.server = server
        self.company = company
        self.extra_spread_points = extra_spread_points
        self.slippage_points = slippage_points
        self.commission_per_lot = commission_per_lot
        self.specs: dict[str, SymbolSpec] = {}
        self.ticks: dict[str, Tick] = {}
        self._positions: dict[int, _Pos] = {}
        self._orders: dict[int, _Ord] = {}
        self.deals: list[Deal] = []
        self._ids = itertools.count(1000)
        self.connected = False
        self.requests: list[TradeRequest] = []
        # fault injection
        self.scripted: deque[int | Exception | str] = deque()
        self.calc_profit_available = True

    # ---------------------------------------------------------------- setup
    def add_symbol(self, spec: SymbolSpec, bid: float, ask: float) -> None:
        self.specs[spec.name] = spec
        self.ticks[spec.name] = Tick(bid=bid, ask=ask, time=time.time())

    def set_price(self, symbol: str, bid: float, ask: float | None = None) -> None:
        with self._lock:
            spec = self._spec(symbol)
            if ask is None:
                old = self.ticks[symbol]
                ask = bid + (old.ask - old.bid)
            self.ticks[symbol] = Tick(bid=round(bid, spec.digits), ask=round(ask, spec.digits),
                                      time=time.time())
            self._process_market(symbol)

    # ------------------------------------------------------------- protocol
    def connect(self, params: ConnectParams) -> AccountInfo:
        self.connected = True
        info = self.account_info()
        assert info is not None
        return info

    def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected and (self.market.is_connected() if self.market else True)

    def account_info(self) -> AccountInfo | None:
        if not self.connected:
            return None
        with self._lock:
            floating = sum(self._pos_profit(p) for p in self._positions.values())
            margin = sum(self.calc_margin(p.type, p.symbol, p.volume, p.price_open) or 0.0
                         for p in self._positions.values())
            equity = self.balance + floating
            return AccountInfo(
                login=self.login, server=self.server, company=self.company, currency=self.currency,
                balance=round(self.balance, 2), equity=round(equity, 2), margin=round(margin, 2),
                margin_free=round(equity - margin, 2), leverage=self.leverage,
                trade_mode=self.trade_mode,
                margin_mode=ACCOUNT_MARGIN_MODE_RETAIL_HEDGING if self.hedging else 0,
                name="Simulator")

    def terminal_info(self) -> TerminalInfo | None:
        return TerminalInfo(connected=self.connected, trade_allowed=True, name="Simulator")

    def symbols(self) -> list[str]:
        return self.market.symbols() if self.market else list(self.specs)

    def symbol_spec(self, symbol: str) -> SymbolSpec | None:
        return self.market.symbol_spec(symbol) if self.market else self.specs.get(symbol)

    def symbol_select(self, symbol: str) -> bool:
        return self.market.symbol_select(symbol) if self.market else symbol in self.specs

    def tick(self, symbol: str) -> Tick | None:
        t = self.market.tick(symbol) if self.market else self.ticks.get(symbol)
        if t is None:
            return None
        if self.extra_spread_points:
            spec = self._spec(symbol)
            half = self.extra_spread_points * spec.point / 2
            t = Tick(bid=t.bid - half, ask=t.ask + half, time=t.time)
        return t

    def calc_profit(self, order_type: int, symbol: str, volume: float,
                    price_open: float, price_close: float) -> float | None:
        if not self.calc_profit_available:
            return None
        if self.market:
            return self.market.calc_profit(order_type, symbol, volume, price_open, price_close)
        spec = self._spec(symbol)
        sign = 1 if order_type in (OrderType.BUY, OrderType.BUY_LIMIT, OrderType.BUY_STOP) else -1
        raw = sign * (price_close - price_open) * volume * spec.trade_contract_size
        return round(raw * self._rate(spec.currency_profit, self.currency), 2)

    def calc_margin(self, order_type: int, symbol: str, volume: float, price: float) -> float | None:
        if self.market:
            return self.market.calc_margin(order_type, symbol, volume, price)
        spec = self._spec(symbol)
        notional_profit_ccy = volume * spec.trade_contract_size * price
        if spec.currency_margin == self.currency:
            notional = volume * spec.trade_contract_size
        else:
            notional = notional_profit_ccy * self._rate(spec.currency_profit, self.currency)
        return round(notional / self.leverage, 2)

    def positions(self, symbol: str | None = None) -> list[Position]:
        with self._lock:
            out = []
            for p in self._positions.values():
                if symbol and p.symbol != symbol:
                    continue
                t = self.tick(p.symbol)
                cur = (t.bid if p.type == 0 else t.ask) if t else p.price_open
                out.append(Position(ticket=p.ticket, symbol=p.symbol, type=p.type, volume=p.volume,
                                    price_open=p.price_open, price_current=cur, sl=p.sl, tp=p.tp,
                                    profit=round(self._pos_profit(p), 2), magic=p.magic,
                                    comment=p.comment, time=p.time, identifier=p.ticket))
            return out

    def orders(self, symbol: str | None = None) -> list[PendingOrder]:
        with self._lock:
            return [PendingOrder(ticket=o.ticket, symbol=o.symbol, type=o.type,
                                 volume_initial=o.volume, volume_current=o.volume,
                                 price_open=o.price_open, sl=o.sl, tp=o.tp, magic=o.magic,
                                 comment=o.comment, time_setup=o.time_setup,
                                 time_expiration=o.expiration)
                    for o in self._orders.values() if not symbol or o.symbol == symbol]

    def history_deals(self, date_from: float, date_to: float) -> list[Deal]:
        with self._lock:
            return [d for d in self.deals if date_from <= d.time <= date_to]

    # ------------------------------------------------------------ trading
    def send(self, request: TradeRequest) -> TradeResult:
        with self._lock:
            self.requests.append(request)
            lost_response = False
            if self.scripted:
                s = self.scripted.popleft()
                if isinstance(s, Exception):
                    raise s
                if s == "EXECUTE_THEN_TIMEOUT":
                    lost_response = True
                else:
                    return TradeResult(retcode=int(s), comment="scripted")
            if not self.is_connected():
                return TradeResult(retcode=Retcode.CONNECTION)
            result = self._dispatch(request)
            if lost_response:
                return TradeResult(retcode=Retcode.TIMEOUT, comment="response lost")
            return result

    def _dispatch(self, r: TradeRequest) -> TradeResult:
        if r.action == TRADE_ACTION_DEAL:
            return self._deal(r)
        if r.action == TRADE_ACTION_PENDING:
            return self._pending(r)
        if r.action == TRADE_ACTION_SLTP:
            return self._sltp(r)
        if r.action == TRADE_ACTION_MODIFY:
            return self._modify(r)
        if r.action == TRADE_ACTION_REMOVE:
            if r.order not in self._orders:
                return TradeResult(retcode=Retcode.INVALID_ORDER)
            del self._orders[r.order]
            return TradeResult(retcode=Retcode.DONE, order=r.order)
        return TradeResult(retcode=Retcode.INVALID)

    def _check_volume(self, spec: SymbolSpec, v: float) -> bool:
        if v < spec.volume_min - 1e-9 or v > spec.volume_max + 1e-9:
            return False
        steps = v / spec.volume_step
        return abs(steps - round(steps)) < 1e-6

    def _check_stops(self, spec: SymbolSpec, side: int, price: float, sl: float, tp: float,
                     tick: Tick) -> bool:
        min_dist = spec.trade_stops_level * spec.point
        # SL/TP of a buy are compared with bid, of a sell with ask
        ref = tick.bid if side == 0 else tick.ask
        if sl:
            if side == 0 and not (sl < ref - min_dist + 1e-12):
                return False
            if side == 1 and not (sl > ref + min_dist - 1e-12):
                return False
        if tp:
            if side == 0 and not (tp > ref + min_dist - 1e-12):
                return False
            if side == 1 and not (tp < ref - min_dist + 1e-12):
                return False
        return True

    def _filling_ok(self, spec: SymbolSpec, filling: int) -> bool:
        if filling == ORDER_FILLING_FOK:
            return bool(spec.filling_mode & SYMBOL_FILLING_FOK)
        if filling == ORDER_FILLING_IOC:
            return bool(spec.filling_mode & SYMBOL_FILLING_IOC)
        return filling == ORDER_FILLING_RETURN

    def _deal(self, r: TradeRequest) -> TradeResult:
        spec = self.symbol_spec(r.symbol)
        tick = self.tick(r.symbol)
        if spec is None or tick is None:
            return TradeResult(retcode=Retcode.INVALID, comment="unknown symbol")
        if spec.trade_mode == SYMBOL_TRADE_MODE_DISABLED:
            return TradeResult(retcode=Retcode.TRADE_DISABLED)
        if not self._check_volume(spec, r.volume):
            return TradeResult(retcode=Retcode.INVALID_VOLUME)
        if not self._filling_ok(spec, r.type_filling):
            return TradeResult(retcode=Retcode.INVALID_FILL)
        side = 0 if r.type == OrderType.BUY else 1
        market = tick.ask if side == 0 else tick.bid
        slip = self.slippage_points * spec.point * (1 if side == 0 else -1)
        fill = round(market + slip, spec.digits)
        if r.price and abs(fill - r.price) > r.deviation * spec.point + 1e-12:
            return TradeResult(retcode=Retcode.REQUOTE, price=fill)

        if r.position:   # closing (part of) an existing position
            p = self._positions.get(r.position)
            if p is None:
                return TradeResult(retcode=Retcode.POSITION_CLOSED)
            if p.type == side or r.volume > p.volume + 1e-9:
                return TradeResult(retcode=Retcode.INVALID)
            self._close(p, r.volume, fill, r.comment)
            return TradeResult(retcode=Retcode.DONE, order=next(self._ids), deal=self.deals[-1].ticket,
                               volume=r.volume, price=fill)

        if not self._check_stops(spec, side, fill, r.sl, r.tp, tick):
            return TradeResult(retcode=Retcode.INVALID_STOPS)
        margin = self.calc_margin(r.type, r.symbol, r.volume, fill) or 0.0
        acct = self.account_info()
        if acct and margin > acct.margin_free:
            return TradeResult(retcode=Retcode.NO_MONEY)
        return self._open(r.symbol, side, r.volume, fill, r.sl, r.tp, r.magic, r.comment)

    def _open(self, symbol: str, side: int, volume: float, price: float, sl: float, tp: float,
              magic: int, comment: str) -> TradeResult:
        order_id = next(self._ids)
        commission = -self.commission_per_lot * volume / 2
        if not self.hedging:
            existing = next((p for p in self._positions.values() if p.symbol == symbol), None)
            if existing is not None:
                if existing.type == side:
                    tot = existing.volume + volume
                    existing.price_open = (existing.price_open * existing.volume + price * volume) / tot
                    existing.volume = round(tot, 8)
                    existing.sl, existing.tp = sl or existing.sl, tp or existing.tp
                    self._record_deal(order_id, existing.ticket, symbol, side, 0, volume, price, 0.0,
                                      commission, magic, comment)
                    return TradeResult(retcode=Retcode.DONE, order=order_id,
                                       deal=self.deals[-1].ticket, volume=volume, price=price)
                closing = min(volume, existing.volume)
                self._close(existing, closing, price, comment)
                volume = round(volume - closing, 8)
                if volume <= 0:
                    return TradeResult(retcode=Retcode.DONE, order=order_id,
                                       deal=self.deals[-1].ticket, volume=closing, price=price)
        ticket = order_id
        self._positions[ticket] = _Pos(ticket, symbol, side, volume, price, sl, tp, magic, comment,
                                       time.time())
        self._record_deal(order_id, ticket, symbol, side, 0, volume, price, 0.0, commission, magic,
                          comment)
        self.balance += commission
        return TradeResult(retcode=Retcode.DONE, order=order_id, deal=self.deals[-1].ticket,
                           volume=volume, price=price)

    def _close(self, p: _Pos, volume: float, price: float, comment: str = "") -> None:
        sign = 1 if p.type == 0 else -1
        spec = self._spec(p.symbol)
        profit = self.calc_profit(p.type, p.symbol, volume, p.price_open, price)
        if profit is None:
            profit = sign * (price - p.price_open) * volume * spec.trade_contract_size
        commission = -self.commission_per_lot * volume / 2
        self.balance += profit + commission
        self._record_deal(next(self._ids), p.ticket, p.symbol, 1 - p.type, 1, volume, price,
                          profit, commission, p.magic, comment or p.comment)
        p.volume = round(p.volume - volume, 8)
        if p.volume <= 1e-9:
            del self._positions[p.ticket]

    def _record_deal(self, order: int, pos_id: int, symbol: str, typ: int, entry: int, volume: float,
                     price: float, profit: float, commission: float, magic: int, comment: str) -> None:
        self.deals.append(Deal(ticket=next(self._ids), order=order, position_id=pos_id, symbol=symbol,
                               type=typ, entry=entry, volume=volume, price=price,
                               profit=round(profit, 2), commission=round(commission, 2), swap=0.0,
                               magic=magic, comment=comment, time=time.time()))

    def _pending(self, r: TradeRequest) -> TradeResult:
        spec = self.symbol_spec(r.symbol)
        tick = self.tick(r.symbol)
        if spec is None or tick is None:
            return TradeResult(retcode=Retcode.INVALID)
        if not self._check_volume(spec, r.volume):
            return TradeResult(retcode=Retcode.INVALID_VOLUME)
        t = OrderType(r.type)
        valid = {
            OrderType.BUY_LIMIT: r.price < tick.ask, OrderType.SELL_LIMIT: r.price > tick.bid,
            OrderType.BUY_STOP: r.price > tick.ask, OrderType.SELL_STOP: r.price < tick.bid,
        }.get(t)
        if not valid:
            return TradeResult(retcode=Retcode.INVALID_PRICE)
        side = 0 if t in (OrderType.BUY_LIMIT, OrderType.BUY_STOP) else 1
        if r.sl and ((side == 0 and r.sl >= r.price) or (side == 1 and r.sl <= r.price)):
            return TradeResult(retcode=Retcode.INVALID_STOPS)
        if r.tp and ((side == 0 and r.tp <= r.price) or (side == 1 and r.tp >= r.price)):
            return TradeResult(retcode=Retcode.INVALID_STOPS)
        ticket = next(self._ids)
        self._orders[ticket] = _Ord(ticket, r.symbol, r.type, r.volume, r.price, r.sl, r.tp, r.magic,
                                    r.comment, time.time(), float(r.expiration or 0))
        return TradeResult(retcode=Retcode.PLACED, order=ticket, volume=r.volume, price=r.price)

    def _sltp(self, r: TradeRequest) -> TradeResult:
        p = self._positions.get(r.position)
        if p is None:
            return TradeResult(retcode=Retcode.POSITION_CLOSED)
        spec = self._spec(p.symbol)
        tick = self.tick(p.symbol)
        assert tick is not None
        if not self._check_stops(spec, p.type, p.price_open, r.sl, r.tp, tick):
            return TradeResult(retcode=Retcode.INVALID_STOPS)
        if math.isclose(p.sl, r.sl) and math.isclose(p.tp, r.tp):
            return TradeResult(retcode=Retcode.NO_CHANGES)
        p.sl, p.tp = r.sl, r.tp
        return TradeResult(retcode=Retcode.DONE)

    def _modify(self, r: TradeRequest) -> TradeResult:
        o = self._orders.get(r.order)
        if o is None:
            return TradeResult(retcode=Retcode.INVALID_ORDER)
        o.price_open = r.price or o.price_open
        o.sl, o.tp = r.sl, r.tp
        return TradeResult(retcode=Retcode.DONE, order=o.ticket)

    # --------------------------------------------------------- market sim
    def _process_market(self, symbol: str) -> None:
        tick = self.tick(symbol)
        if tick is None:
            return
        now = time.time()
        for o in list(self._orders.values()):
            if o.symbol != symbol:
                continue
            if o.expiration and now > o.expiration:
                del self._orders[o.ticket]
                continue
            t = OrderType(o.type)
            hit = {OrderType.BUY_LIMIT: tick.ask <= o.price_open,
                   OrderType.SELL_LIMIT: tick.bid >= o.price_open,
                   OrderType.BUY_STOP: tick.ask >= o.price_open,
                   OrderType.SELL_STOP: tick.bid <= o.price_open}[t]
            if hit:
                del self._orders[o.ticket]
                side = 0 if t in (OrderType.BUY_LIMIT, OrderType.BUY_STOP) else 1
                self._open(symbol, side, o.volume, tick.ask if side == 0 else tick.bid, o.sl, o.tp,
                           o.magic, o.comment)
        for p in list(self._positions.values()):
            if p.symbol != symbol:
                continue
            px = tick.bid if p.type == 0 else tick.ask
            if p.type == 0:
                if p.sl and px <= p.sl:
                    self._close(p, p.volume, px, "[sl]")
                elif p.tp and px >= p.tp:
                    self._close(p, p.volume, px, "[tp]")
            else:
                if p.sl and px >= p.sl:
                    self._close(p, p.volume, px, "[sl]")
                elif p.tp and px <= p.tp:
                    self._close(p, p.volume, px, "[tp]")

    def refresh(self) -> None:
        """Re-evaluate pending orders and SL/TP against current prices (paper mode)."""
        with self._lock:
            for sym in {p.symbol for p in self._positions.values()} | \
                       {o.symbol for o in self._orders.values()}:
                self._process_market(sym)

    # --------------------------------------------------------------- utils
    def _spec(self, symbol: str) -> SymbolSpec:
        spec = self.symbol_spec(symbol)
        if spec is None:
            raise GatewayError(f"unknown symbol {symbol}")
        return spec

    def _pos_profit(self, p: _Pos) -> float:
        t = self.tick(p.symbol)
        if t is None:
            return 0.0
        close = t.bid if p.type == 0 else t.ask
        return self.calc_profit(p.type, p.symbol, p.volume, p.price_open, close) or 0.0

    def _rate(self, src: str, dst: str) -> float:
        if src == dst:
            return 1.0
        if src + dst in self.ticks:
            t = self.ticks[src + dst]
            return (t.bid + t.ask) / 2
        if dst + src in self.ticks:
            t = self.ticks[dst + src]
            return 2 / (t.bid + t.ask)
        raise GatewayError(f"no conversion rate {src}->{dst}")


class FakeMT5Gateway(SimBroker):
    """Deterministic broker for tests (no market-data source)."""


class PaperGateway(SimBroker):
    """Simulated execution on top of real market data."""

    def __init__(self, market: MT5Gateway, *, balance: float, extra_spread_points: float,
                 slippage_points: float, commission_per_lot: float, currency: str = "USD") -> None:
        acct = market.account_info()
        super().__init__(balance=balance, currency=acct.currency if acct else currency,
                         leverage=acct.leverage if acct else 500,
                         hedging=True, login=0, server="PAPER-SIMULATION", company="Simulation",
                         market=market, extra_spread_points=extra_spread_points,
                         slippage_points=slippage_points, commission_per_lot=commission_per_lot)
        self.connected = True

    def tick(self, symbol: str) -> Tick | None:  # noqa: D401 - also processes simulated fills
        return super().tick(symbol)


def with_spec(spec: SymbolSpec, **kw) -> SymbolSpec:
    return replace(spec, **kw)
