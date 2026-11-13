"""Execution engine: the single path from a Telegram message to a broker order.

Pipeline for a new message::

    RECEIVED -> PARSED -> VALIDATED -> QUEUED -> SUBMITTING -> EXECUTED / PENDING_PLACED
                    \\-> MANUAL_REVIEW   \\-> REJECTED / WAITING / MONITORED / SIMULATED
                                                          \\-> PARTIAL / FAILED / UNCERTAIN

Invariants
----------
* Every method runs on the single MT5 worker thread, so signals are processed
  strictly one at a time (no risk-check races between concurrent signals).
* (source_id, message_id) is unique in the DB: a repeated NEW event is a no-op.
* An order is *persisted with its client tag before it is sent*.  If the send
  outcome is unknown, the leg becomes UNCERTAIN and is reconciled against MT5
  positions/orders/deals by tag before any retry is considered.
* Nothing here sends an order unless mode is DEMO (or PAPER, against the
  simulator), the account was verified as DEMO, the user confirmed automated
  execution for that account, and every risk check passed.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from tgmt5.config import AppConfig, TpStrategy, ZonePolicy
from tgmt5.domain.models import (
    Decision, Direction, EntryType, ExecutionMode, ManagementInstruction, ManagementType,
    MessageEventType, MessageKind, ParsedSignal, SignalStatus, TelegramMessage,
)
from tgmt5.execution.legs import LegState
from tgmt5.mt5.interface import (
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN, SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC, SYMBOL_TRADE_MODE_DISABLED, TRADE_ACTION_DEAL, TRADE_ACTION_PENDING,
    TRADE_ACTION_REMOVE, TRADE_ACTION_SLTP, AccountInfo, GatewayError, LiveAccountBlocked,
    MT5Gateway, OrderType, Retcode, SymbolSpec, Tick, TradeRequest, TradeResult,
)
from tgmt5.mt5.symbols import SymbolResolver
from tgmt5.parsing.parser import ParserConfig, SignalParser
from tgmt5.persistence.db import Database
from tgmt5.persistence.models import ExecutionRow, SignalEventRow, SignalRow, TelegramMessageRow
from tgmt5.risk.engine import OpenRisk, RiskEngine, TradeProposal, position_open_risk
from tgmt5.risk.sizing import (
    SizingError, floor_to_step, loss_at_stop, plan_volumes, tp_layout,
)

log = logging.getLogger(__name__)

ACTIVE_LEG_STATES = (LegState.OPEN, LegState.PENDING, LegState.SUBMITTING, LegState.UNCERTAIN)
REPROCESSABLE_ON_EDIT = (SignalStatus.MANUAL_REVIEW, SignalStatus.WAITING)


@dataclass
class LegPlan:
    volume: float
    tp: float
    tp_index: int | None


@dataclass
class ExecutionPlan:
    broker_symbol: str
    spec: SymbolSpec
    order_type: OrderType
    price: float                  # expected fill (market) or pending price
    pending: bool
    legs: list[LegPlan]
    total_volume: float
    risk_money: float
    margin: float
    spread_points: float
    managed_tps: tuple[float, ...] = ()   # SINGLE_MANAGED partial targets


class ExecutionEngine:
    def __init__(self, db: Database, gateway: MT5Gateway | None, risk: RiskEngine,
                 config: Callable[[], AppConfig], *, mode: ExecutionMode = ExecutionMode.MONITOR,
                 clock: Callable[[], datetime] | None = None,
                 emit: Callable[[str, dict], None] | None = None,
                 symbol_suffix: str = "") -> None:
        self.db = db
        self.risk = risk
        self._config = config
        self.mode = mode
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._emit = emit or (lambda kind, data: None)
        self.gw: MT5Gateway | None = None
        self.resolver: SymbolResolver | None = None
        self.account: AccountInfo | None = None
        self.set_gateway(gateway, symbol_suffix)

    # ------------------------------------------------------------------ setup
    @property
    def cfg(self) -> AppConfig:
        return self._config()

    @property
    def simulated(self) -> bool:
        return self.mode is ExecutionMode.PAPER

    def set_gateway(self, gateway: MT5Gateway | None, suffix: str = "") -> None:
        self.gw = gateway
        self.resolver = SymbolResolver(gateway, suffix) if gateway else None
        self.account = gateway.account_info() if gateway else None

    def set_mode(self, mode: ExecutionMode, gateway: MT5Gateway | None = None) -> None:
        self.mode = mode
        if gateway is not None:
            self.set_gateway(gateway, self.cfg.mt5.symbol_suffix)
        self.audit_app("INFO", "mode", f"Execution mode set to {mode.value}")

    def emit(self, kind: str, **data: Any) -> None:
        try:
            self._emit(kind, data)
        except Exception:  # pragma: no cover - UI callbacks must never break the engine
            log.exception("emit callback failed")

    def audit_app(self, level: str, category: str, message: str, data: dict | None = None) -> None:
        log.log(getattr(logging, level, logging.INFO), "[%s] %s", category, message)
        self.db.log_event(level, category, message, data)
        self.emit("log", level=level, category=category, message=message)

    # --------------------------------------------------------------- helpers
    def _transition(self, s: Session, sig: SignalRow, status: SignalStatus, message: str = "",
                    data: dict | None = None) -> None:
        old = sig.status
        sig.status = status.value
        if status in (SignalStatus.REJECTED, SignalStatus.FAILED, SignalStatus.EXPIRED,
                      SignalStatus.MANUAL_REVIEW, SignalStatus.CANCELLED) and message:
            sig.reject_reason = message
        sig.updated_at = self.clock()
        s.add(SignalEventRow(signal=sig, status_from=old, status_to=status.value, message=message,
                             data=data, ts=self.clock()))
        s.flush()
        self.emit("signal", id=sig.id, status=status.value, message=message)

    def _parser_for(self, source_id: int) -> SignalParser:
        src = self.cfg.source(source_id)
        aliases = dict(src.symbol_aliases) if src else {}
        return SignalParser(ParserConfig(symbol_aliases=aliases,
                                         implicit_market=bool(src and src.template == "implicit_market")))

    def refresh_account(self) -> AccountInfo | None:
        if self.gw is None:
            return None
        self.account = self.gw.account_info()
        if self.account is not None and self.mode is not ExecutionMode.PAPER:
            self.risk.on_account_update(self.account)
        return self.account

    # ===================================================== message entrypoint
    def handle_message(self, msg: TelegramMessage) -> int | None:
        """Process one Telegram event. Returns the affected signal id (if any)."""
        if msg.event is MessageEventType.DELETE:
            return self._on_delete(msg)
        if msg.event is MessageEventType.EDIT:
            return self._on_edit(msg)
        return self._on_new(msg)

    def _authorized(self, source_id: int) -> bool:
        src = self.cfg.source(source_id)
        return bool(src and src.enabled)

    def _on_new(self, msg: TelegramMessage) -> int | None:
        with self.db.session() as s:
            existing = s.scalar(select(TelegramMessageRow).where(
                TelegramMessageRow.source_id == msg.source_id,
                TelegramMessageRow.message_id == msg.message_id))
            if existing is not None:
                log.info("duplicate NEW event %s/%s ignored", msg.source_id, msg.message_id)
                return None
            row = TelegramMessageRow(
                source_id=msg.source_id, source_title=msg.source_title, message_id=msg.message_id,
                reply_to_message_id=msg.reply_to_message_id, text=msg.text[:8000], date=msg.date,
                received_at=msg.received_at or self.clock())
            s.add(row)
            s.flush()
            row_id = row.id
        if not self._authorized(msg.source_id):
            self.audit_app("WARNING", "telegram",
                           f"Message from unauthorised/disabled source {msg.source_id} ignored")
            return None
        return self._interpret(row_id, msg)

    def _interpret(self, row_id: int, msg: TelegramMessage, existing: SignalRow | None = None) -> int | None:
        result = self._parser_for(msg.source_id).parse(msg.text)
        if result.kind is MessageKind.NOISE and existing is None:
            log.debug("noise from %s/%s: %s", msg.source_id, msg.message_id, result.reasons)
            return None
        latency = None
        if msg.received_at:
            latency = max(0.0, (msg.received_at - msg.date).total_seconds() * 1000)
        with self.db.session() as s:
            sig = s.get(SignalRow, existing.id) if existing else None
            if sig is None:
                sig = SignalRow(message_row_id=row_id, source_id=msg.source_id,
                                message_id=msg.message_id, kind=result.kind.value,
                                status=SignalStatus.RECEIVED.value, is_simulated=self.simulated,
                                latency_ms=latency, created_at=self.clock())
                s.add(sig)
                s.flush()
                self._transition(s, sig, SignalStatus.RECEIVED, "message received")
            sig.kind = result.kind.value
            sig.reasons = list(result.reasons)
            if result.kind is MessageKind.NOISE:
                self._transition(s, sig, SignalStatus.CANCELLED, "edited into non-signal text")
                return sig.id
            if result.kind is MessageKind.AMBIGUOUS:
                self._transition(s, sig, SignalStatus.MANUAL_REVIEW,
                                 "; ".join(result.reasons) or "ambiguous")
                return sig.id
            if result.signal is not None:
                p = result.signal
                sig.symbol, sig.direction = p.symbol, p.direction.value
                sig.entry_type, sig.entry_price = p.entry_type.value, p.entry_price
                sig.entry_low, sig.entry_high = p.entry_low, p.entry_high
                sig.stop_loss, sig.take_profits = p.stop_loss, list(p.take_profits)
                sig.fingerprint = _fingerprint(p)
                self._transition(s, sig, SignalStatus.PARSED, "parsed",
                                 {"signal": _signal_dict(p)})
                sig_id = sig.id
            else:
                assert result.management is not None
                m = result.management
                sig.symbol = m.symbol
                sig.management = {"type": m.type.value, "fraction": m.fraction, "price": m.price,
                                  "tp_index": m.tp_index}
                self._transition(s, sig, SignalStatus.PARSED, f"management: {m.type.value}")
                sig_id = sig.id
        if result.signal is not None:
            self.process_signal(sig_id, msg.date)
        else:
            assert result.management is not None
            self.process_management(sig_id, result.management, msg)
        return sig_id

    def _on_edit(self, msg: TelegramMessage) -> int | None:
        with self.db.session() as s:
            row = s.scalar(select(TelegramMessageRow).where(
                TelegramMessageRow.source_id == msg.source_id,
                TelegramMessageRow.message_id == msg.message_id))
            if row is None:
                # Edit of a message we never saw: treat as new; the original timestamp is kept
                # so signal-age validation rejects stale content.
                pass
            else:
                if row.text == msg.text[:8000]:
                    return None
                row.text = msg.text[:8000]
                row.edit_count += 1
                row_id = row.id
                sig = s.scalar(select(SignalRow).where(SignalRow.message_row_id == row.id,
                                                       SignalRow.parent_signal_id.is_(None))
                               .order_by(SignalRow.id))
        if row is None:
            return self._on_new(msg)
        if not self._authorized(msg.source_id):
            return None
        if sig is None:
            return self._interpret(row_id, msg)
        status = SignalStatus(sig.status)
        if status in REPROCESSABLE_ON_EDIT:
            with self.db.session() as s:
                srow = s.get(SignalRow, sig.id)
                self._transition(s, srow, SignalStatus.RECEIVED, "message edited - re-evaluating")
            return self._interpret(row_id, msg, existing=sig)
        if status.is_active_trade and sig.kind == MessageKind.NEW_SIGNAL.value:
            return self._edit_of_active_signal(sig, msg)
        with self.db.session() as s:
            s.add(SignalEventRow(signal_id=sig.id, status_from=sig.status, status_to=sig.status,
                                 message="message edited after final state - no action",
                                 data={"text": msg.text[:500]}, ts=self.clock()))
        return sig.id

    def _edit_of_active_signal(self, sig: SignalRow, msg: TelegramMessage) -> int:
        result = self._parser_for(msg.source_id).parse(msg.text)
        new = result.signal
        if (self.cfg.execution.apply_edits_to_open_trades and new is not None
                and new.symbol == sig.symbol and new.direction.value == sig.direction):
            instr: list[ManagementInstruction] = []
            if new.stop_loss != sig.stop_loss:
                instr.append(ManagementInstruction(ManagementType.MODIFY_SL, price=new.stop_loss))
            for ins in instr:
                self._apply_management(sig.id, ins, origin=f"edit of message {msg.message_id}")
            return sig.id
        with self.db.session() as s:
            s.add(SignalEventRow(signal_id=sig.id, status_from=sig.status, status_to=sig.status,
                                 message="message edited after execution - requires manual review; "
                                         "no automatic change applied",
                                 data={"text": msg.text[:1000]}, ts=self.clock()))
        self.audit_app("WARNING", "signal",
                       f"Signal #{sig.id} edited after execution; review manually", None)
        return sig.id

    def _on_delete(self, msg: TelegramMessage) -> int | None:
        with self.db.session() as s:
            row = s.scalar(select(TelegramMessageRow).where(
                TelegramMessageRow.message_id == msg.message_id,
                *( [TelegramMessageRow.source_id == msg.source_id] if msg.source_id else [])))
            if row is None:
                return None
            row.deleted = True
            sig = s.scalar(select(SignalRow).where(SignalRow.message_row_id == row.id))
            if sig is None:
                return None
            st = SignalStatus(sig.status)
            if st in (SignalStatus.WAITING, SignalStatus.MANUAL_REVIEW, SignalStatus.QUEUED,
                      SignalStatus.PARSED, SignalStatus.VALIDATED):
                self._transition(s, sig, SignalStatus.CANCELLED, "message deleted by provider")
            else:
                s.add(SignalEventRow(signal_id=sig.id, status_from=sig.status, status_to=sig.status,
                                     message="message deleted by provider - open trades unchanged",
                                     ts=self.clock()))
            return sig.id

    # ======================================================= new signals
    def process_signal(self, sig_id: int, message_date: datetime | None = None,
                       *, from_waiting: bool = False) -> SignalStatus:
        with self.db.session() as s:
            sig = s.get(SignalRow, sig_id)
            assert sig is not None
            parsed = _row_to_signal(sig)
            d = Decision(approved=True)
            try:
                plan = self._validate(s, sig, parsed, message_date, d, from_waiting)
            except (GatewayError, SizingError) as e:
                if from_waiting:
                    return SignalStatus(sig.status)
                self._transition(s, sig, SignalStatus.REJECTED, f"broker query failed: {e}")
                return SignalStatus.REJECTED
            if plan is None:
                return SignalStatus(sig.status)
            if not d.approved:
                self._transition(s, sig, SignalStatus.REJECTED, d.reason, {"checks": d.checks})
                self._notify_rejected(sig)
                return SignalStatus.REJECTED
            self._transition(s, sig, SignalStatus.VALIDATED, "all checks passed",
                             {"checks": d.checks, "lots": [l.volume for l in plan.legs],
                              "risk": plan.risk_money})
            if self.mode is ExecutionMode.MONITOR:
                self._transition(s, sig, SignalStatus.MONITORED,
                                 "monitor-only mode: no order sent")
                return SignalStatus.MONITORED
            if self.mode in (ExecutionMode.DEMO, ExecutionMode.LIVE):
                acct = self.account
                key = f"{acct.login}@{acct.server}" if acct else ""
                live = self.mode is ExecutionMode.LIVE
                confirmed = self.risk.state.confirmed_real_account if live else self.risk.state.confirmed_account
                if not acct or confirmed != key:
                    self._transition(s, sig, SignalStatus.REJECTED,
                                     f"automated execution not confirmed for this "
                                     f"{'REAL' if live else 'DEMO'} account")
                    return SignalStatus.REJECTED
            self._transition(s, sig, SignalStatus.QUEUED, "queued for execution")
        return self._execute(sig_id, plan)

    def _validate(self, s: Session, sig: SignalRow, p: ParsedSignal,
                  message_date: datetime | None, d: Decision,
                  from_waiting: bool) -> ExecutionPlan | None:
        cfg = self.cfg
        ex = cfg.execution
        now = self.clock()
        # ---- static checks (no market data needed)
        if ex.require_take_profit and not p.take_profits:
            self._transition(s, sig, SignalStatus.MANUAL_REVIEW, "no take profit in signal")
            return None
        if not from_waiting and message_date is not None:
            age = (now - message_date).total_seconds()
            if age > ex.max_signal_age_seconds:
                self._transition(s, sig, SignalStatus.EXPIRED,
                                 f"signal is {age:.0f}s old (max {ex.max_signal_age_seconds}s)")
                return None
        if not from_waiting and ex.cross_source_dedup_seconds and sig.fingerprint:
            since = now - timedelta(seconds=ex.cross_source_dedup_seconds)
            dup = s.scalar(select(SignalRow).where(
                SignalRow.fingerprint == sig.fingerprint, SignalRow.id != sig.id,
                SignalRow.created_at >= since, SignalRow.is_simulated == self.simulated,
                SignalRow.status.in_([x.value for x in (
                    SignalStatus.EXECUTED, SignalStatus.PARTIAL, SignalStatus.PENDING_PLACED,
                    SignalStatus.WAITING, SignalStatus.QUEUED, SignalStatus.SUBMITTING,
                    SignalStatus.SIMULATED, SignalStatus.UNCERTAIN, SignalStatus.MONITORED)])))
            if dup is not None:
                self._transition(s, sig, SignalStatus.REJECTED,
                                 f"duplicate of signal #{dup.id} (same symbol/direction/levels)")
                return None
        # ---- market-dependent checks
        if self.gw is None or not self.gw.is_connected():
            if not from_waiting:
                self._transition(s, sig, SignalStatus.REJECTED,
                                 "MT5 not connected - cannot validate against market")
            return None
        assert self.resolver is not None
        broker_sym = self.resolver.resolve(p.symbol)
        if broker_sym is None:
            self._transition(s, sig, SignalStatus.MANUAL_REVIEW,
                             f"symbol {p.symbol} not found (or ambiguous) at broker")
            return None
        sig.broker_symbol = broker_sym
        spec = self.gw.symbol_spec(broker_sym)
        tick = self.gw.tick(broker_sym)
        acct = self.refresh_account()
        if spec is None or tick is None or acct is None:
            self._transition(s, sig, SignalStatus.REJECTED, "no symbol specification / price / account")
            return None
        d.add("symbol_tradeable", spec.trade_mode != SYMBOL_TRADE_MODE_DISABLED,
              f"{broker_sym} trading disabled")
        d.add("tick_fresh", now.timestamp() - tick.time < 120 or self.simulated,
              "last price is stale (market closed?)")
        prof = cfg.risk.symbol_profiles.get(p.symbol)
        sl_dist = abs((p.reference_entry or (tick.ask if p.direction is Direction.BUY else tick.bid))
                      - p.stop_loss)
        spread_pts = tick.spread / spec.point
        max_spread = cfg.risk.max_spread_sl_fraction * sl_dist / spec.point
        if prof and prof.max_spread_points is not None:
            max_spread = min(max_spread, prof.max_spread_points)
        d.add("spread", spread_pts <= max_spread + 1e-9,
              f"spread {spread_pts:.0f} pts > max {max_spread:.0f} pts")

        decision = self._entry_decision(p, tick, spec, sl_dist, prof)
        if decision == "WAIT":
            if from_waiting:
                return None   # keep waiting silently
            if not d.approved:
                self._transition(s, sig, SignalStatus.REJECTED, d.reason, {"checks": d.checks})
                return None
            sig.expires_at = now + timedelta(minutes=ex.waiting_signal_expiry_minutes)
            self._transition(s, sig, SignalStatus.WAITING,
                             f"waiting for entry zone until {sig.expires_at:%H:%M} UTC")
            return None
        if decision.startswith("REJECT"):
            d.add("entry", False, decision.split(":", 1)[1].strip())
            return self._dummy_plan(spec, broker_sym)
        pending = decision.startswith("PENDING")
        if pending:
            order_type, price = _pending_order(p, decision)
        else:
            order_type = OrderType.BUY if p.direction is Direction.BUY else OrderType.SELL
            price = tick.ask if p.direction is Direction.BUY else tick.bid

        # ---- broker stop constraints relative to the actual entry price
        min_dist = (spec.trade_stops_level + 1) * spec.point if spec.trade_stops_level else 0
        side_ref_sl = (tick.bid if p.direction is Direction.BUY else tick.ask) if not pending else price
        ok_sl = p.direction.sign * (side_ref_sl - p.stop_loss) > min_dist
        d.add("sl_valid", ok_sl, "stop loss already breached or closer than broker stops level")
        for i, tp in enumerate(p.take_profits, 1):
            ok_tp = p.direction.sign * (tp - side_ref_sl) > min_dist
            d.add(f"tp{i}_valid", ok_tp, f"TP{i} {tp} already reached or too close to price")
        if not d.approved:
            return self._dummy_plan(spec, broker_sym)

        # ---- sizing (shared with the Risk page preview: tgmt5.risk.sizing.plan_volumes)
        src = cfg.source(sig.source_id)
        mult = (src.risk_multiplier if src else 1.0) * (prof.risk_multiplier if prof else 1.0)
        strategy, positions, covered = tp_layout(ex.tp_strategy, len(p.take_profits), acct.is_hedging)
        gw, direction, sl = self.gw, p.direction, p.stop_loss
        sz = plan_volumes(cfg.risk, positions=positions, tps_covered=covered, allocation=ex.tp_allocation,
                          spec=spec, loss_fn=lambda v: loss_at_stop(gw, spec, direction, v, price, sl)[0],
                          equity=acct.equity, balance=acct.balance, multiplier=mult,
                          max_total=min(cfg.risk.max_lot_per_order * max(1, len(p.take_profits)),
                                        cfg.risk.max_lots_per_symbol))
        if sz.error:
            d.add("sizing", False, sz.error)
            return self._dummy_plan(spec, broker_sym)
        for note in sz.notes:
            self.audit_app("INFO", "execution", f"{p.symbol}: {note}")

        legs, managed = self._legs_for(sz.volumes, p, strategy)
        total = round(sum(l.volume for l in legs), 8)
        risk_money = sz.risk_money
        d.add("risk_budget", sz.within_budget,
              f"risk {risk_money:.2f} exceeds budget {sz.budget or 0:.2f}")
        margin = self.gw.calc_margin(order_type if not pending else
                                     (OrderType.BUY if p.direction is Direction.BUY else OrderType.SELL),
                                     broker_sym, total, price)
        if margin is None:
            d.add("margin_calc", False, "broker could not compute margin")
            margin = float("inf")
        sig.lot_total, sig.risk_money = total, round(risk_money, 2)
        open_risk = self.open_risk()
        proposal = TradeProposal(symbol=p.symbol, broker_symbol=broker_sym, direction=p.direction,
                                 entry=price, stop_loss=p.stop_loss,
                                 volumes=tuple(l.volume for l in legs), risk_money=risk_money,
                                 margin_required=margin, take_profits=p.take_profits)
        self.risk.check(proposal, acct, open_risk, self.active_trade_count(), d)
        return ExecutionPlan(broker_symbol=broker_sym, spec=spec, order_type=order_type, price=price,
                             pending=pending, legs=legs, total_volume=total, risk_money=risk_money,
                             margin=margin, spread_points=spread_pts, managed_tps=managed)

    def _dummy_plan(self, spec: SymbolSpec, sym: str) -> ExecutionPlan:
        return ExecutionPlan(sym, spec, OrderType.BUY, 0.0, False, [], 0.0, 0.0, 0.0, 0.0)

    def _entry_decision(self, p: ParsedSignal, tick: Tick, spec: SymbolSpec, sl_dist: float,
                        prof) -> str:
        """Return MARKET, WAIT, PENDING:<price>:<kind>, or REJECT:<reason>."""
        ex = self.cfg.execution
        price = tick.ask if p.direction is Direction.BUY else tick.bid
        tol = self.cfg.risk.max_entry_deviation_sl_fraction * sl_dist
        if prof and prof.max_entry_deviation_points is not None:
            tol = min(tol, prof.max_entry_deviation_points * spec.point)
        sgn = p.direction.sign
        if p.entry_type is EntryType.MARKET and p.entry_price is None:
            return "MARKET"
        if p.entry_type in (EntryType.LIMIT, EntryType.STOP):
            assert p.entry_price is not None
            e = p.entry_price
            valid_side = (sgn * (price - e) > 0) if p.entry_type is EntryType.LIMIT else (sgn * (e - price) > 0)
            if valid_side:
                return f"PENDING:{e}:{p.entry_type.value}"
            if abs(price - e) <= tol:
                return "MARKET"
            return f"REJECT: {p.entry_type.value} price {e} no longer valid vs market {price}"
        if p.entry_type is EntryType.ZONE:
            assert p.entry_low is not None and p.entry_high is not None
            if p.entry_low - tol <= price <= p.entry_high + tol:
                return "MARKET"
            adverse = (price > p.entry_high) if sgn > 0 else (price < p.entry_low)
            if not adverse:
                return f"REJECT: price {price} beyond the zone toward the stop loss"
            edge = p.entry_high if sgn > 0 else p.entry_low
            return self._apply_zone_policy(ex.zone_policy, edge, f"price {price} outside zone")
        # single price (PRICE or MARKET with reference)
        assert p.entry_price is not None
        dev = price - p.entry_price
        if abs(dev) <= tol:
            return "MARKET"
        if sgn * dev > 0:   # market moved away in the trade's direction (worse entry)
            if p.entry_type is EntryType.MARKET:
                return f"REJECT: market moved {abs(dev):.{spec.digits}f} away from entry {p.entry_price}"
            return self._apply_zone_policy(ex.zone_policy, p.entry_price,
                                           f"price {price} too far from entry {p.entry_price}")
        return f"REJECT: price {price} moved toward the stop loss beyond tolerance"

    @staticmethod
    def _apply_zone_policy(policy: ZonePolicy, edge: float, why: str) -> str:
        if policy is ZonePolicy.WAIT:
            return "WAIT"
        if policy is ZonePolicy.PENDING:
            return f"PENDING:{edge}:LIMIT"
        return f"REJECT: {why} (zone policy REJECT)"

    @staticmethod
    def _legs_for(volumes: list[float], p: ParsedSignal,
                  strategy: TpStrategy) -> tuple[list[LegPlan], tuple[float, ...]]:
        """Turn planned volumes into order legs (one per TP when splitting)."""
        tps = list(p.take_profits)
        if not tps:
            return [LegPlan(volumes[0], 0.0, None)], ()
        if strategy is TpStrategy.SPLIT and len(tps) > 1 and len(volumes) > 1:
            return [LegPlan(v, tps[i], i + 1) for i, v in enumerate(volumes)], ()
        if strategy is TpStrategy.SINGLE_MANAGED and len(tps) > 1:
            return [LegPlan(volumes[0], tps[-1], len(tps))], tuple(tps[:-1])
        return [LegPlan(volumes[0], tps[0], 1)], ()

    # ------------------------------------------------------------- execution
    def _filling_modes(self, spec: SymbolSpec, pending: bool) -> list[int]:
        if pending:
            return [ORDER_FILLING_RETURN]
        modes = []
        if spec.filling_mode & SYMBOL_FILLING_FOK:
            modes.append(ORDER_FILLING_FOK)
        if spec.filling_mode & SYMBOL_FILLING_IOC:
            modes.append(ORDER_FILLING_IOC)
        modes.append(ORDER_FILLING_RETURN)
        return modes

    def _execute(self, sig_id: int, plan: ExecutionPlan) -> SignalStatus:
        assert self.gw is not None
        ex = self.cfg.execution
        with self.db.session() as s:
            sig = s.get(SignalRow, sig_id)
            self._transition(s, sig, SignalStatus.SUBMITTING,
                             f"sending {len(plan.legs)} order(s), total {plan.total_volume} lots")
            direction = sig.direction
            if plan.managed_tps:
                sig.management = {"managed_tps": list(plan.managed_tps), "done": []}
            rows = []
            for i, leg in enumerate(plan.legs):
                share = plan.risk_money * leg.volume / plan.total_volume if plan.total_volume else 0
                r = ExecutionRow(
                    signal_id=sig_id, leg_index=i, client_tag=_tag(sig_id, i, self.simulated),
                    state=LegState.SUBMITTING.value, symbol=plan.broker_symbol, direction=direction,
                    order_type=int(plan.order_type), volume=leg.volume, requested_price=plan.price,
                    sl=sig.stop_loss, tp=leg.tp or None, tp_index=leg.tp_index,
                    risk_money=round(share, 2), is_simulated=self.simulated,
                    account_login=self.account.login if self.account else None,
                    spread_points=plan.spread_points, created_at=self.clock())
                s.add(r)
                rows.append(r)
            s.flush()
            leg_ids = [r.id for r in rows]
        # rows are committed (SUBMITTING) before anything is sent
        for leg_id in leg_ids:
            self._send_leg(leg_id, plan)
        self.risk.record_trade()
        return self._finalize_signal(sig_id, plan.pending)

    def _build_request(self, leg: ExecutionRow, plan: ExecutionPlan, price: float,
                       filling: int) -> TradeRequest:
        ex = self.cfg.execution
        expiration = 0
        return TradeRequest(
            action=TRADE_ACTION_PENDING if plan.pending else TRADE_ACTION_DEAL,
            symbol=leg.symbol, volume=leg.volume, type=leg.order_type,
            price=round(price, plan.spec.digits), sl=leg.sl or 0.0, tp=leg.tp or 0.0,
            deviation=self.cfg.risk.max_slippage_points, magic=ex.magic_number,
            comment=leg.client_tag, type_filling=filling, expiration=expiration)

    def _send_leg(self, leg_id: int, plan: ExecutionPlan) -> None:
        assert self.gw is not None
        with self.db.session() as s:
            leg = s.get(ExecutionRow, leg_id)
            fillings = self._filling_modes(plan.spec, plan.pending)
            price = plan.price
            attempts = 0
            retried_uncertain = False
            while True:
                attempts += 1
                req = self._build_request(leg, plan, price, fillings[0])
                leg.request = req.as_dict()
                s.commit()      # persist the exact request before sending
                t0 = time.perf_counter()
                try:
                    res = self.gw.send(req)
                except LiveAccountBlocked as e:
                    leg.state, leg.broker_comment = LegState.REJECTED.value, str(e)
                    self.risk.emergency(f"live account detected: {e}")
                    self.audit_app("CRITICAL", "safety", str(e))
                    return
                except Exception as e:  # noqa: BLE001 - transport failure => outcome unknown
                    res = TradeResult(retcode=Retcode.CONNECTION, comment=f"exception: {e}")
                leg.latency_ms = (time.perf_counter() - t0) * 1000
                leg.retcode = res.retcode
                leg.broker_comment = res.text
                self.audit_app("INFO" if res.ok else "WARNING", "mt5",
                               f"{leg.client_tag} {req.symbol} {req.volume} -> {res.text}",
                               {"request": req.as_dict(), "retcode": res.retcode,
                                "order": res.order, "price": res.price})
                if res.ok:
                    self._mark_leg_success(s, leg, res, plan)
                    return
                if res.uncertain:
                    leg.state = LegState.UNCERTAIN.value
                    s.commit()
                    found = self._reconcile_leg(s, leg)
                    if found:
                        return
                    # Proven absent after reconciliation: optionally retry once with fresh price.
                    if self.cfg.execution.reconcile_retry_once and not retried_uncertain:
                        retried_uncertain = True
                        new_price = self._fresh_price_within_tolerance(leg, plan)
                        if new_price is not None:
                            price = new_price
                            continue
                    leg.state = LegState.FAILED.value
                    return
                if res.retcode == Retcode.INVALID_FILL and len(fillings) > 1:
                    fillings = fillings[1:]
                    continue
                if res.retcode in (Retcode.REQUOTE, Retcode.PRICE_CHANGED, Retcode.PRICE_OFF) \
                        and attempts < 3 and not plan.pending:
                    new_price = self._fresh_price_within_tolerance(leg, plan)
                    if new_price is not None:
                        price = new_price
                        continue
                leg.state = LegState.REJECTED.value
                if res.retcode == Retcode.CLIENT_DISABLES_AT:
                    self.audit_app("ERROR", "mt5", "Algo Trading is disabled in the MT5 terminal. "
                                   "Enable the 'Algo Trading' button in MT5.")
                return

    def _fresh_price_within_tolerance(self, leg: ExecutionRow, plan: ExecutionPlan) -> float | None:
        tick = self.gw.tick(leg.symbol) if self.gw else None
        if tick is None:
            return None
        price = tick.ask if leg.direction == "BUY" else tick.bid
        if abs(price - plan.price) <= self.cfg.risk.max_slippage_points * plan.spec.point:
            return price
        return None

    def _mark_leg_success(self, s: Session, leg: ExecutionRow, res: TradeResult,
                          plan: ExecutionPlan) -> None:
        leg.order_ticket = res.order or None
        if plan.pending and res.retcode == Retcode.PLACED:
            leg.state = LegState.PENDING.value
            return
        leg.state = LegState.OPEN.value
        leg.opened_at = self.clock()
        leg.fill_price = res.price or None
        if res.volume and res.volume + 1e-9 < leg.volume:
            leg.volume = res.volume     # partial fill: track what we really hold
            leg.broker_comment += " (partial fill)"
        if leg.fill_price and leg.requested_price:
            leg.slippage_points = round(
                (leg.fill_price - leg.requested_price) / plan.spec.point *
                (1 if leg.direction == "BUY" else -1), 1)
        pos = self._find_position_by_tag(leg.client_tag)
        leg.position_ticket = pos.ticket if pos else (res.order or None)
        if pos and not leg.fill_price:
            leg.fill_price = pos.price_open

    def _find_position_by_tag(self, tag: str):
        assert self.gw is not None
        magic = self.cfg.execution.magic_number
        for p in self.gw.positions():
            if p.comment == tag and p.magic == magic:
                return p
        return None

    def _reconcile_leg(self, s: Session, leg: ExecutionRow) -> bool:
        """Determine the true outcome of an UNCERTAIN leg from broker state. Never sends."""
        assert self.gw is not None
        magic = self.cfg.execution.magic_number
        try:
            pos = self._find_position_by_tag(leg.client_tag)
            if pos is not None:
                leg.state, leg.position_ticket = LegState.OPEN.value, pos.ticket
                leg.fill_price, leg.volume = pos.price_open, pos.volume
                leg.opened_at = leg.opened_at or self.clock()
                self.audit_app("WARNING", "reconcile",
                               f"{leg.client_tag}: uncertain order found OPEN (ticket {pos.ticket})")
                return True
            for o in self.gw.orders():
                if o.comment == leg.client_tag and o.magic == magic:
                    leg.state, leg.order_ticket = LegState.PENDING.value, o.ticket
                    self.audit_app("WARNING", "reconcile",
                                   f"{leg.client_tag}: uncertain order found PENDING ({o.ticket})")
                    return True
            now = self.clock().timestamp()
            deals = [d for d in self.gw.history_deals(now - 86400 * 7, now + 60)
                     if d.comment == leg.client_tag and d.magic == magic and d.entry == 0]
            if deals:
                d0 = deals[0]
                leg.state, leg.position_ticket = LegState.OPEN.value, d0.position_id
                leg.fill_price = d0.price
                s.flush()
                self.sync_leg(s, leg)   # it was opened and possibly already closed
                self.audit_app("WARNING", "reconcile",
                               f"{leg.client_tag}: uncertain order found in deal history")
                return True
        except GatewayError as e:
            self.audit_app("ERROR", "reconcile", f"{leg.client_tag}: reconciliation failed: {e}")
            leg.state = LegState.UNCERTAIN.value
            return True   # stay UNCERTAIN; do NOT retry while broker state is unknown
        return False

    def _finalize_signal(self, sig_id: int, pending: bool) -> SignalStatus:
        with self.db.session() as s:
            sig = s.get(SignalRow, sig_id)
            states = [LegState(e.state) for e in sig.executions]
            ok = [st for st in states if st in (LegState.OPEN, LegState.PENDING, LegState.CLOSED)]
            if any(st is LegState.UNCERTAIN for st in states):
                status = SignalStatus.UNCERTAIN
                msg = "order outcome unknown - reconciliation pending"
            elif ok and len(ok) == len(states):
                if self.simulated:
                    status = SignalStatus.SIMULATED
                else:
                    status = SignalStatus.PENDING_PLACED if all(
                        st is LegState.PENDING for st in states) else SignalStatus.EXECUTED
                msg = f"{len(ok)} order(s) accepted"
            elif ok:
                status = SignalStatus.PARTIAL
                msg = f"{len(ok)}/{len(states)} legs accepted"
            else:
                status = SignalStatus.FAILED
                msg = "; ".join({e.broker_comment for e in sig.executions}) or "all orders rejected"
            self._transition(s, sig, status, msg,
                             {"tickets": [e.position_ticket or e.order_ticket for e in sig.executions]})
            if status in (SignalStatus.EXECUTED, SignalStatus.PENDING_PLACED, SignalStatus.SIMULATED,
                          SignalStatus.PARTIAL):
                self.emit("notify", event="executed", title=f"{sig.direction} {sig.symbol}",
                          body=f"{sig.lot_total} lots, risk {sig.risk_money}")
            elif status is SignalStatus.FAILED:
                self._notify_rejected(sig)
            return status

    def _notify_rejected(self, sig: SignalRow) -> None:
        self.emit("notify", event="rejected", title=f"Signal #{sig.id} rejected",
                  body=f"{sig.symbol or ''} {sig.reject_reason}"[:200])

    # ============================================================ queries
    def open_risk(self) -> list[OpenRisk]:
        """Risk of all application-owned open positions and pending orders."""
        if self.gw is None or self.resolver is None:
            return []
        magic = self.cfg.execution.magic_number
        out = []
        for p in self.gw.positions():
            if p.magic != magic:
                continue
            out.append(OpenRisk(self.resolver.canonical_of(p.symbol),
                                position_open_risk(self.gw, p), p.volume))
        for o in self.gw.orders():
            if o.magic != magic or not o.sl:
                continue
            side = 0 if o.type in (OrderType.BUY_LIMIT, OrderType.BUY_STOP) else 1
            pr = self.gw.calc_profit(side, o.symbol, o.volume_current, o.price_open, o.sl)
            out.append(OpenRisk(self.resolver.canonical_of(o.symbol), max(0.0, -(pr or 0)),
                                o.volume_current))
        return out

    def active_trade_count(self) -> int:
        with self.db.session() as s:
            q = select(SignalRow.id).where(
                SignalRow.is_simulated == self.simulated,
                SignalRow.status.in_([SignalStatus.EXECUTED.value, SignalStatus.PARTIAL.value,
                                      SignalStatus.PENDING_PLACED.value, SignalStatus.SIMULATED.value,
                                      SignalStatus.UNCERTAIN.value]))
            return len(list(s.scalars(q)))

    # ====================================================== management
    def process_management(self, sig_id: int, instr: ManagementInstruction,
                           msg: TelegramMessage) -> SignalStatus:
        from tgmt5.execution.management import resolve_target
        with self.db.session() as s:
            row = s.get(SignalRow, sig_id)
            target, why = resolve_target(s, self, msg, instr)
            if target is None:
                status = SignalStatus.MANUAL_REVIEW if why.startswith("ambiguous") else SignalStatus.REJECTED
                self._transition(s, row, status, why)
                return status
            row.parent_signal_id = target
            if instr.type is ManagementType.SL_HIT or (
                    instr.type is ManagementType.TP_HIT and
                    self.cfg.execution.on_tp_hit_message.value == "IGNORE"):
                self._transition(s, row, SignalStatus.MONITORED,
                                 f"informational ({instr.type.value}) for signal #{target}")
                return SignalStatus.MONITORED
            if self.mode is ExecutionMode.MONITOR:
                self._transition(s, row, SignalStatus.MONITORED,
                                 f"monitor-only: would apply {instr.type.value} to signal #{target}")
                return SignalStatus.MONITORED
        ok, detail = self._apply_management(target, instr, origin=f"message {msg.message_id}")
        with self.db.session() as s:
            row = s.get(SignalRow, sig_id)
            self._transition(s, row, SignalStatus.EXECUTED if ok else SignalStatus.FAILED,
                             f"{instr.type.value} on signal #{target}: {detail}")
        return SignalStatus.EXECUTED if ok else SignalStatus.FAILED

    def _apply_management(self, target_id: int, instr: ManagementInstruction,
                          origin: str) -> tuple[bool, str]:
        from tgmt5.execution.management import apply_instruction
        return apply_instruction(self, target_id, instr, origin)

    # ======================================================= periodic
    def on_tick(self) -> None:
        """Periodic work: waiting signals, pending expiry, position sync, trade management."""
        from tgmt5.execution.management import manage_open_trades
        if self.gw is None:
            return
        if hasattr(self.gw, "refresh"):
            try:
                self.gw.refresh()       # simulator: process SL/TP/pending triggers
            except Exception:  # noqa: BLE001
                log.exception("simulator refresh failed")
        self.refresh_account()
        self._process_waiting()
        manage_open_trades(self)

    def _process_waiting(self) -> None:
        now = self.clock()
        with self.db.session() as s:
            waiting = list(s.scalars(select(SignalRow).where(
                SignalRow.status == SignalStatus.WAITING.value,
                SignalRow.is_simulated == self.simulated)))
            ids = []
            for sig in waiting:
                exp = sig.expires_at
                if exp is not None and exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp is not None and now >= exp:
                    self._transition(s, sig, SignalStatus.EXPIRED, "entry zone not reached in time")
                else:
                    ids.append(sig.id)
        for sid in ids:
            if self.mode is ExecutionMode.MONITOR:
                continue
            self.process_signal(sid, None, from_waiting=True)

    # =========================================================== sync
    def sync_leg(self, s: Session, leg: ExecutionRow) -> None:
        from tgmt5.execution.management import sync_leg
        sync_leg(self, s, leg)


# ------------------------------------------------------------------ utils
def _tag(sig_id: int, leg: int, simulated: bool) -> str:
    return f"{'SIM' if simulated else 'TG'}{sig_id}L{leg}"[:31]


def _fingerprint(p: ParsedSignal) -> str:
    raw = f"{p.symbol}|{p.direction.value}|{p.stop_loss}|{','.join(map(str, p.take_profits))}"
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def _signal_dict(p: ParsedSignal) -> dict:
    return {"direction": p.direction.value, "symbol": p.symbol, "entry_type": p.entry_type.value,
            "entry_price": p.entry_price, "entry_low": p.entry_low, "entry_high": p.entry_high,
            "sl": p.stop_loss, "tps": list(p.take_profits)}


def _row_to_signal(r: SignalRow) -> ParsedSignal:
    return ParsedSignal(direction=Direction(r.direction), symbol=r.symbol,
                        entry_type=EntryType(r.entry_type), stop_loss=r.stop_loss,
                        take_profits=tuple(r.take_profits or ()), entry_price=r.entry_price,
                        entry_low=r.entry_low, entry_high=r.entry_high)


def _pending_order(p: ParsedSignal, decision: str) -> tuple[OrderType, float]:
    _, price, kind = decision.split(":")
    buy = p.direction is Direction.BUY
    if kind == "STOP":
        return (OrderType.BUY_STOP if buy else OrderType.SELL_STOP), float(price)
    return (OrderType.BUY_LIMIT if buy else OrderType.SELL_LIMIT), float(price)
