"""Start-up recovery: restore state and reconcile it with the broker. Never sends new orders."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from tgmt5.domain.models import SignalStatus
from tgmt5.execution.legs import LegState
from tgmt5.execution.management import TRADE_STATUSES, _maybe_close_signal, sync_leg
from tgmt5.persistence.models import ExecutionRow, SignalRow

if TYPE_CHECKING:
    from tgmt5.execution.engine import ExecutionEngine


@dataclass
class RecoveryReport:
    resolved_uncertain: list[str] = field(default_factory=list)
    failed_uncertain: list[str] = field(default_factory=list)
    closed_while_offline: list[str] = field(default_factory=list)
    orphan_positions: list[int] = field(default_factory=list)
    expired_waiting: list[int] = field(default_factory=list)
    interrupted_signals: list[int] = field(default_factory=list)

    def summary(self) -> str:
        return (f"recovered: {len(self.resolved_uncertain)} uncertain resolved, "
                f"{len(self.failed_uncertain)} proven not executed, "
                f"{len(self.closed_while_offline)} closed while offline, "
                f"{len(self.orphan_positions)} unmanaged positions with our magic, "
                f"{len(self.expired_waiting)} waiting signals expired, "
                f"{len(self.interrupted_signals)} interrupted before sending")


def recover(eng: "ExecutionEngine") -> RecoveryReport:
    rep = RecoveryReport()
    gw = eng.gw
    if gw is None or not gw.is_connected():
        eng.audit_app("WARNING", "recovery", "MT5 not connected - recovery deferred")
        return rep
    positions = {p.ticket: p for p in gw.positions()}
    orders = {o.ticket: o for o in gw.orders()}
    now = eng.clock()
    with eng.db.session() as s:
        # 1. legs whose send outcome is unknown (crash between persist and response)
        for leg in s.scalars(select(ExecutionRow).where(
                ExecutionRow.is_simulated == eng.simulated,
                ExecutionRow.state.in_([LegState.SUBMITTING.value, LegState.UNCERTAIN.value]))):
            if eng._reconcile_leg(s, leg):
                rep.resolved_uncertain.append(leg.client_tag)
            else:
                leg.state = LegState.FAILED.value
                leg.broker_comment = (leg.broker_comment + " | not found at broker on recovery").strip(" |")
                rep.failed_uncertain.append(leg.client_tag)
        s.flush()
        # 2. open / pending legs -> sync with actual broker state
        for leg in s.scalars(select(ExecutionRow).where(
                ExecutionRow.is_simulated == eng.simulated,
                ExecutionRow.state.in_([LegState.OPEN.value, LegState.PENDING.value]))):
            before = leg.state
            sync_leg(eng, s, leg, positions, orders)
            if before == LegState.OPEN.value and leg.state == LegState.CLOSED.value:
                rep.closed_while_offline.append(leg.client_tag)
        s.flush()
        # 3. signal-level status
        for sig in s.scalars(select(SignalRow).where(
                SignalRow.is_simulated == eng.simulated,
                SignalRow.status.in_([x.value for x in TRADE_STATUSES] +
                                     [SignalStatus.SUBMITTING.value, SignalStatus.QUEUED.value]))):
            legs = list(sig.executions)
            if not legs:
                eng._transition(s, sig, SignalStatus.FAILED,
                                "application stopped before any order was sent; not replayed")
                rep.interrupted_signals.append(sig.id)
                continue
            states = {LegState(l.state) for l in legs}
            if sig.status in (SignalStatus.SUBMITTING.value, SignalStatus.UNCERTAIN.value,
                              SignalStatus.QUEUED.value):
                live = [l for l in legs if l.state in (LegState.OPEN.value, LegState.PENDING.value,
                                                       LegState.CLOSED.value)]
                status = (SignalStatus.FAILED if not live else
                          SignalStatus.EXECUTED if len(live) == len(legs) else SignalStatus.PARTIAL)
                if eng.simulated and live:
                    status = SignalStatus.SIMULATED
                eng._transition(s, sig, status, "status restored by recovery")
            _maybe_close_signal(eng, s, sig, legs)
        # 4. waiting signals past expiry
        for sig in s.scalars(select(SignalRow).where(SignalRow.status == SignalStatus.WAITING.value,
                                                     SignalRow.is_simulated == eng.simulated)):
            exp = sig.expires_at
            if exp is not None and exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp is None or exp <= now:
                eng._transition(s, sig, SignalStatus.EXPIRED, "expired while application was offline")
                rep.expired_waiting.append(sig.id)
        # 5. positions carrying our magic that we do not know about
        known = {l.position_ticket for l in s.scalars(select(ExecutionRow))}
        magic = eng.cfg.execution.magic_number
        rep.orphan_positions = [t for t, p in positions.items() if p.magic == magic and t not in known]
    eng.audit_app("INFO" if not (rep.failed_uncertain or rep.orphan_positions) else "WARNING",
                  "recovery", rep.summary())
    return rep
