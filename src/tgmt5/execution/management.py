"""Trade management: follow-up instructions, position sync, TP/BE/trailing automation.

All functions run on the MT5 worker thread and are driven by
:class:`tgmt5.execution.engine.ExecutionEngine`.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from tgmt5.domain.models import (
    ExecutionMode, ManagementInstruction, ManagementType, MessageKind, SignalStatus, TelegramMessage,
)
from tgmt5.execution.legs import LegState
from tgmt5.mt5.interface import (
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN, SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC, TRADE_ACTION_DEAL, TRADE_ACTION_MODIFY, TRADE_ACTION_REMOVE,
    TRADE_ACTION_SLTP, OrderType, Retcode, TradeRequest, TradeResult,
)
from tgmt5.persistence.models import ExecutionRow, SignalEventRow, SignalRow, TelegramMessageRow
from tgmt5.risk.sizing import floor_to_step, step_decimals

if TYPE_CHECKING:
    from tgmt5.execution.engine import ExecutionEngine

log = logging.getLogger(__name__)

TRADE_STATUSES = (SignalStatus.EXECUTED, SignalStatus.PARTIAL, SignalStatus.PENDING_PLACED,
                  SignalStatus.SIMULATED, SignalStatus.UNCERTAIN)
FINAL_LEG = (LegState.CLOSED, LegState.REJECTED, LegState.FAILED, LegState.CANCELLED)


# ------------------------------------------------------------------ targeting
def resolve_target(s: Session, eng: "ExecutionEngine", msg: TelegramMessage,
                   instr: ManagementInstruction) -> tuple[int | None, str]:
    """Find the signal a management message refers to. Never guesses between candidates."""
    statuses = [x.value for x in TRADE_STATUSES] + [SignalStatus.WAITING.value]
    if eng.mode is ExecutionMode.MONITOR:
        statuses.append(SignalStatus.MONITORED.value)

    if msg.reply_to_message_id:
        mrow = s.scalar(select(TelegramMessageRow).where(
            TelegramMessageRow.source_id == msg.source_id,
            TelegramMessageRow.message_id == msg.reply_to_message_id))
        if mrow is None:
            return None, "ambiguous: replied-to message is unknown"
        sig = s.scalar(select(SignalRow).where(SignalRow.message_row_id == mrow.id)
                       .order_by(SignalRow.id))
        if sig is not None and sig.parent_signal_id:
            sig = s.get(SignalRow, sig.parent_signal_id)
        if sig is None or sig.kind != MessageKind.NEW_SIGNAL.value:
            return None, "ambiguous: replied-to message is not a trade signal"
        if instr.symbol and sig.symbol != instr.symbol:
            return None, f"ambiguous: reply references {sig.symbol} but message names {instr.symbol}"
        if sig.status not in statuses:
            return None, f"signal #{sig.id} is not active ({sig.status})"
        return sig.id, "reply"

    q = select(SignalRow).where(
        SignalRow.source_id == msg.source_id, SignalRow.kind == MessageKind.NEW_SIGNAL.value,
        SignalRow.is_simulated == eng.simulated, SignalRow.status.in_(statuses))
    if instr.symbol:
        q = q.where(SignalRow.symbol == instr.symbol)
    cands = list(s.scalars(q))
    if instr.type is ManagementType.CANCEL_PENDING:
        cands = [c for c in cands if c.status in (SignalStatus.WAITING.value,
                                                  SignalStatus.PENDING_PLACED.value)
                 or any(e.state == LegState.PENDING.value for e in c.executions)]
    if not cands:
        return None, "no matching active trade from this source"
    if len(cands) > 1:
        return None, f"ambiguous: {len(cands)} active trades match - confirm manually"
    return cands[0].id, "context"


# ------------------------------------------------------------------ broker ops
def _fillings(spec) -> list[int]:
    modes = []
    if spec.filling_mode & SYMBOL_FILLING_FOK:
        modes.append(ORDER_FILLING_FOK)
    if spec.filling_mode & SYMBOL_FILLING_IOC:
        modes.append(ORDER_FILLING_IOC)
    return modes + [ORDER_FILLING_RETURN]


def _send(eng: "ExecutionEngine", req: TradeRequest, what: str) -> TradeResult:
    try:
        res = eng.gw.send(req)
    except Exception as e:  # noqa: BLE001
        res = TradeResult(retcode=Retcode.CONNECTION, comment=f"exception: {e}")
    eng.audit_app("INFO" if res.ok else "WARNING", "mt5", f"{what}: {res.text}",
                  {"request": req.as_dict(), "retcode": res.retcode})
    return res


def close_leg(eng: "ExecutionEngine", s: Session, leg: ExecutionRow, volume: float,
              reason: str) -> tuple[bool, str]:
    gw = eng.gw
    spec = gw.symbol_spec(leg.symbol)
    tick = gw.tick(leg.symbol)
    if spec is None or tick is None or not leg.position_ticket:
        return False, "no price/spec/ticket"
    volume = floor_to_step(min(volume, leg.volume - leg.closed_volume), spec.volume_step)
    if volume < spec.volume_min - 1e-9:
        return False, f"volume {volume} below broker minimum"
    remaining = round(leg.volume - leg.closed_volume - volume, step_decimals(spec.volume_step))
    if 0 < remaining < spec.volume_min - 1e-9:
        volume = round(leg.volume - leg.closed_volume, step_decimals(spec.volume_step))
    buy = leg.direction == "BUY"
    before = next((p for p in gw.positions(leg.symbol) if p.ticket == leg.position_ticket), None)
    if before is None:
        sync_leg(eng, s, leg)
        return False, "position already closed"
    for filling in _fillings(spec):
        req = TradeRequest(action=TRADE_ACTION_DEAL, symbol=leg.symbol, volume=volume,
                           type=OrderType.SELL if buy else OrderType.BUY,
                           price=tick.bid if buy else tick.ask, position=leg.position_ticket,
                           deviation=eng.cfg.risk.max_slippage_points,
                           magic=eng.cfg.execution.magic_number,
                           comment=(leg.client_tag + "X")[:31], type_filling=filling)
        res = _send(eng, req, f"close {volume} of {leg.client_tag} ({reason})")
        if res.retcode == Retcode.INVALID_FILL:
            continue
        if res.uncertain:
            after = next((p for p in gw.positions(leg.symbol) if p.ticket == leg.position_ticket), None)
            done = after is None or after.volume < before.volume - 1e-9
            if not done:
                return False, "close outcome unknown; position unchanged - not retried"
        elif not res.ok:
            return False, res.text
        leg.closed_volume = round(leg.closed_volume + volume, 8)
        s.add(SignalEventRow(signal_id=leg.signal_id, status_from=None, status_to="LEG",
                             message=f"closed {volume} lots of leg {leg.leg_index} ({reason})",
                             ts=eng.clock()))
        sync_leg(eng, s, leg)
        return True, f"closed {volume}"
    return False, "no supported filling mode"


def modify_leg_sl(eng: "ExecutionEngine", s: Session, leg: ExecutionRow, new_sl: float,
                  reason: str, new_tp: float | None = None) -> tuple[bool, str]:
    gw = eng.gw
    spec = gw.symbol_spec(leg.symbol)
    tick = gw.tick(leg.symbol)
    if spec is None or tick is None:
        return False, "no price/spec"
    new_sl = round(new_sl, spec.digits)
    tp = leg.tp if new_tp is None else new_tp
    buy = leg.direction == "BUY"
    min_dist = spec.trade_stops_level * spec.point
    if leg.state == LegState.PENDING.value:
        req = TradeRequest(action=TRADE_ACTION_MODIFY, symbol=leg.symbol, order=leg.order_ticket,
                           price=leg.requested_price or 0.0, sl=new_sl, tp=tp or 0.0)
    else:
        ref = tick.bid if buy else tick.ask
        if (buy and new_sl >= ref - min_dist) or (not buy and new_sl <= ref + min_dist):
            return False, f"SL {new_sl} not valid vs current price {ref}"
        req = TradeRequest(action=TRADE_ACTION_SLTP, symbol=leg.symbol,
                           position=leg.position_ticket, sl=new_sl, tp=tp or 0.0,
                           magic=eng.cfg.execution.magic_number)
    res = _send(eng, req, f"modify {leg.client_tag} SL->{new_sl} ({reason})")
    if res.ok or res.retcode == Retcode.NO_CHANGES:
        leg.sl = new_sl
        if new_tp is not None:
            leg.tp = new_tp
        return True, "modified"
    return False, res.text


def cancel_pending_leg(eng: "ExecutionEngine", s: Session, leg: ExecutionRow, reason: str) -> tuple[bool, str]:
    res = _send(eng, TradeRequest(action=TRADE_ACTION_REMOVE, order=leg.order_ticket),
                f"cancel {leg.client_tag} ({reason})")
    if res.ok or res.retcode == Retcode.INVALID_ORDER:
        if res.retcode == Retcode.INVALID_ORDER:
            sync_leg(eng, s, leg)       # maybe it was filled meanwhile
            if leg.state != LegState.PENDING.value:
                return leg.state == LegState.CANCELLED.value, f"order already {leg.state}"
        leg.state = LegState.CANCELLED.value
        leg.closed_at = eng.clock()
        return True, "cancelled"
    return False, res.text


# ------------------------------------------------------------------ instructions
def apply_instruction(eng: "ExecutionEngine", target_id: int, instr: ManagementInstruction,
                      origin: str) -> tuple[bool, str]:
    if eng.gw is None:
        return False, "MT5 not connected"
    with eng.db.session() as s:
        sig = s.get(SignalRow, target_id)
        legs = list(sig.executions)
        open_legs = [l for l in legs if l.state == LegState.OPEN.value]
        pending = [l for l in legs if l.state == LegState.PENDING.value]
        t = instr.type
        results: list[tuple[bool, str]] = []

        if t is ManagementType.TP_HIT:
            t = ManagementType.MOVE_SL_BE   # only reached when on_tp_hit_message == BREAKEVEN

        if t is ManagementType.CANCEL_PENDING:
            if sig.status == SignalStatus.WAITING.value:
                eng._transition(s, sig, SignalStatus.CANCELLED, f"cancelled by provider ({origin})")
                return True, "waiting signal cancelled"
            if not pending:
                return False, "no pending order to cancel"
            results = [cancel_pending_leg(eng, s, l, origin) for l in pending]

        elif t is ManagementType.CLOSE_ALL:
            if sig.status == SignalStatus.WAITING.value:
                eng._transition(s, sig, SignalStatus.CANCELLED, f"closed by provider ({origin})")
                return True, "waiting signal cancelled"
            results = [close_leg(eng, s, l, l.volume - l.closed_volume, origin) for l in open_legs]
            results += [cancel_pending_leg(eng, s, l, origin) for l in pending]
            if not results:
                return False, "nothing open"

        elif t is ManagementType.CLOSE_PARTIAL:
            assert instr.fraction is not None
            total_open = sum(l.volume - l.closed_volume for l in open_legs)
            if not open_legs:
                return False, "no open position"
            spec = eng.gw.symbol_spec(open_legs[0].symbol)
            target = floor_to_step(total_open * instr.fraction, spec.volume_step)
            if target < spec.volume_min - 1e-9:
                return False, f"{instr.fraction:.0%} of {total_open} lots is below broker minimum"
            remaining = target
            for l in sorted(open_legs, key=lambda x: x.tp_index or 0):
                if remaining <= 1e-9:
                    break
                avail = l.volume - l.closed_volume
                take = min(avail, remaining)
                ok, msg = close_leg(eng, s, l, take, origin)
                results.append((ok, msg))
                if ok:
                    remaining = round(remaining - take, 8)

        elif t is ManagementType.MOVE_SL_BE:
            if not open_legs and not pending:
                return False, "no open position"
            off = eng.cfg.execution.breakeven_offset_points
            for l in open_legs:
                spec = eng.gw.symbol_spec(l.symbol)
                entry = l.fill_price or l.requested_price
                be = entry + (off * spec.point if l.direction == "BUY" else -off * spec.point)
                ok, msg = modify_leg_sl(eng, s, l, be, origin)
                if ok:
                    l.be_applied = True
                results.append((ok, msg))

        elif t is ManagementType.MODIFY_SL:
            assert instr.price is not None
            d = 1 if sig.direction == "BUY" else -1
            ref_entry = sig.entry_price or sig.entry_low or sig.stop_loss
            if instr.price <= 0 or abs(instr.price - ref_entry) / ref_entry > 0.2:
                return False, "new SL implausible"
            results = [modify_leg_sl(eng, s, l, instr.price, origin) for l in open_legs + pending]
            if any(ok for ok, _ in results):
                sig.stop_loss = instr.price

        elif t is ManagementType.MODIFY_TP:
            assert instr.price is not None
            live = open_legs + pending
            if len({l.tp for l in live}) > 1:
                return False, "ambiguous: several TP legs - confirm manually which to change"
            for l in live:
                ok, msg = modify_leg_sl(eng, s, l, l.sl, origin, new_tp=instr.price)
                results.append((ok, msg))
        else:
            return False, f"unsupported instruction {t.value}"

        ok_all = bool(results) and all(ok for ok, _ in results)
        detail = "; ".join(m for _, m in results) or "no action"
        s.add(SignalEventRow(signal_id=sig.id, status_from=sig.status, status_to=sig.status,
                             message=f"{instr.type.value} ({origin}): {detail}", ts=eng.clock()))
        return ok_all, detail


# ------------------------------------------------------------------ sync & automation
def sync_leg(eng: "ExecutionEngine", s: Session, leg: ExecutionRow,
             positions: dict | None = None, orders: dict | None = None) -> None:
    gw = eng.gw
    if positions is None:
        positions = {p.ticket: p for p in gw.positions()}
    if orders is None:
        orders = {o.ticket: o for o in gw.orders()}
    magic = eng.cfg.execution.magic_number
    if leg.state == LegState.PENDING.value:
        if leg.order_ticket in orders:
            created = leg.created_at.replace(tzinfo=timezone.utc) if leg.created_at.tzinfo is None \
                else leg.created_at
            if eng.clock() - created > timedelta(minutes=eng.cfg.execution.pending_order_expiry_minutes):
                cancel_pending_leg(eng, s, leg, "pending order expired")
            return
        pos = next((p for p in positions.values() if p.comment == leg.client_tag and p.magic == magic),
                   None)
        if pos is not None:
            leg.state, leg.position_ticket = LegState.OPEN.value, pos.ticket
            leg.fill_price, leg.opened_at = pos.price_open, eng.clock()
            leg.volume = pos.volume
            return
        now = eng.clock().timestamp()
        deals = [d for d in gw.history_deals(now - 86400 * 30, now + 60)
                 if d.comment == leg.client_tag and d.entry == 0]
        if deals:
            leg.state, leg.position_ticket = LegState.OPEN.value, deals[0].position_id
            leg.fill_price = deals[0].price
            # fall through: position may already be closed
        else:
            leg.state, leg.closed_at = LegState.CANCELLED.value, eng.clock()
            return
    if leg.state != LegState.OPEN.value:
        return
    pos = positions.get(leg.position_ticket)
    if pos is not None:
        leg.sl, leg.tp = pos.sl or leg.sl, pos.tp or leg.tp
        return
    # position is gone -> closed (by SL/TP, by us, or manually in the terminal)
    now = eng.clock().timestamp()
    deals = [d for d in gw.history_deals(now - 86400 * 30, now + 60)
             if d.position_id == leg.position_ticket]
    exits = [d for d in deals if d.entry in (1, 2, 3)]
    if exits:
        leg.profit = round(sum(d.profit + d.swap for d in exits), 2)
        leg.commission = round(sum(d.commission for d in deals), 2)
        leg.close_price = exits[-1].price
    leg.state = LegState.CLOSED.value
    leg.closed_at = eng.clock()
    leg.closed_volume = leg.volume


def manage_open_trades(eng: "ExecutionEngine") -> None:
    gw = eng.gw
    if gw is None or not gw.is_connected():
        return
    try:
        positions = {p.ticket: p for p in gw.positions()}
        orders = {o.ticket: o for o in gw.orders()}
    except Exception as e:  # noqa: BLE001
        eng.audit_app("ERROR", "mt5", f"position sync failed: {e}")
        return
    ex = eng.cfg.execution
    with eng.db.session() as s:
        sigs = list(s.scalars(select(SignalRow).where(
            SignalRow.is_simulated == eng.simulated,
            SignalRow.status.in_([x.value for x in TRADE_STATUSES]))))
        for sig in sigs:
            legs = list(sig.executions)
            for leg in legs:
                if leg.state == LegState.UNCERTAIN.value:
                    if not eng._reconcile_leg(s, leg):
                        leg.state = LegState.FAILED.value
                        eng.audit_app("WARNING", "reconcile",
                                      f"{leg.client_tag}: not found at broker - marked FAILED")
                elif leg.state in (LegState.OPEN.value, LegState.PENDING.value):
                    was_open = leg.state == LegState.OPEN.value
                    sync_leg(eng, s, leg, positions, orders)
                    if was_open and leg.state == LegState.CLOSED.value:
                        s.add(SignalEventRow(signal_id=sig.id, status_from=sig.status,
                                             status_to=sig.status, ts=eng.clock(),
                                             message=f"leg {leg.leg_index} closed, P/L {leg.profit}"))
            s.flush()
            # Monitor-only mode (e.g. right after a restart or an update) cannot send orders:
            # keep tracking the legs but leave breakeven / partials / trailing for later, so they
            # are applied as soon as execution is switched back on instead of being marked done.
            if eng.mode is not ExecutionMode.MONITOR:
                _auto_breakeven(eng, s, sig, legs, ex)
                _managed_partials(eng, s, sig, legs, ex)
                if ex.trailing_enabled:
                    _trail(eng, s, sig, legs, ex)
            _maybe_close_signal(eng, s, sig, legs)


def _auto_breakeven(eng, s, sig, legs, ex) -> None:
    if not ex.move_sl_to_be_after_tp:
        return
    trigger = next((l for l in legs if l.tp_index == ex.move_sl_to_be_after_tp
                    and l.state == LegState.CLOSED.value and (l.profit or 0) > 0), None)
    if trigger is None:
        return
    for l in legs:
        if l.state == LegState.OPEN.value and not l.be_applied:
            spec = eng.gw.symbol_spec(l.symbol)
            off = ex.breakeven_offset_points * spec.point
            entry = l.fill_price or l.requested_price
            be = entry + off if l.direction == "BUY" else entry - off
            ok, msg = modify_leg_sl(eng, s, l, be, f"TP{trigger.tp_index} reached")
            l.be_applied = ok or l.be_applied
            if not ok:
                l.be_applied = True  # do not hammer the broker; logged once
                eng.audit_app("WARNING", "management", f"{l.client_tag}: breakeven failed: {msg}")


def _managed_partials(eng, s, sig, legs, ex) -> None:
    m = sig.management or {}
    tps = m.get("managed_tps") or []
    if not tps:
        return
    done = list(m.get("done", []))
    leg = next((l for l in legs if l.state == LegState.OPEN.value), None)
    if leg is None:
        return
    tick = eng.gw.tick(leg.symbol)
    spec = eng.gw.symbol_spec(leg.symbol)
    if tick is None or spec is None:
        return
    buy = leg.direction == "BUY"
    px = tick.bid if buy else tick.ask
    weights = (ex.tp_allocation + [ex.tp_allocation[-1]] * (len(tps) + 1))[:len(tps) + 1]
    wsum = sum(weights)
    for i, tp in enumerate(tps):
        if i in done:
            continue
        reached = px >= tp if buy else px <= tp
        if not reached:
            break
        vol = floor_to_step(leg.volume * weights[i] / wsum, spec.volume_step)
        ok, msg = close_leg(eng, s, leg, vol, f"managed TP{i + 1}") if vol >= spec.volume_min \
            else (True, "partial below minimum volume - skipped")
        done.append(i)
        if i + 1 == ex.move_sl_to_be_after_tp and leg.state == LegState.OPEN.value and not leg.be_applied:
            off = ex.breakeven_offset_points * spec.point
            entry = leg.fill_price or leg.requested_price
            okb, _ = modify_leg_sl(eng, s, leg, entry + off if buy else entry - off, f"TP{i + 1}")
            leg.be_applied = True
    sig.management = {**m, "done": done}


def _trail(eng, s, sig, legs, ex) -> None:
    for l in legs:
        if l.state != LegState.OPEN.value or not l.be_applied:
            continue
        tick = eng.gw.tick(l.symbol)
        spec = eng.gw.symbol_spec(l.symbol)
        if tick is None or spec is None or sig.stop_loss is None:
            continue
        entry = l.fill_price or l.requested_price
        dist = abs(entry - sig.stop_loss) * ex.trailing_distance_sl_fraction
        buy = l.direction == "BUY"
        px = tick.bid if buy else tick.ask
        new_sl = round(px - dist if buy else px + dist, spec.digits)
        better = (new_sl > (l.sl or 0) + spec.point) if buy else (new_sl < (l.sl or 1e18) - spec.point)
        if better:
            modify_leg_sl(eng, s, l, new_sl, "trailing stop")


def _maybe_close_signal(eng, s, sig, legs) -> None:
    states = [LegState(l.state) for l in legs]
    if not states or not all(st in FINAL_LEG for st in states):
        return
    if sig.status in (SignalStatus.CLOSED.value, SignalStatus.CANCELLED.value,
                      SignalStatus.FAILED.value):
        return
    if any(st is LegState.CLOSED for st in states):
        net = sum((l.profit or 0) + (l.commission or 0) for l in legs)
        eng._transition(s, sig, SignalStatus.CLOSED, f"all positions closed, net {net:.2f}",
                        {"net": net})
        if not eng.simulated:
            eng.risk.on_signal_closed(net)
        eng.emit("notify", event="closed", title=f"{sig.direction} {sig.symbol} closed",
                 body=f"net {net:+.2f}")
    elif any(st is LegState.CANCELLED for st in states):
        eng._transition(s, sig, SignalStatus.CANCELLED, "pending order(s) cancelled/expired")
    else:
        eng._transition(s, sig, SignalStatus.FAILED, "no order was executed")
