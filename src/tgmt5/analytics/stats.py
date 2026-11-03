"""Performance analytics computed from persisted executions (DEMO and PAPER kept separate)."""
from __future__ import annotations

import csv
import io
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from tgmt5.domain.models import MessageKind, SignalStatus
from tgmt5.execution.legs import LegState
from tgmt5.persistence.db import Database
from tgmt5.persistence.models import EquitySnapshotRow, ExecutionRow, SignalRow


@dataclass
class TradeOutcome:
    signal_id: int
    source_id: int
    symbol: str
    direction: str
    opened: datetime | None
    closed: datetime | None
    net: float
    risk: float | None

    @property
    def r_multiple(self) -> float | None:
        return self.net / self.risk if self.risk else None


@dataclass
class Stats:
    simulated: bool
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net: float = 0.0
    profit_factor: float | None = None
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_r: float | None = None
    max_drawdown: float = 0.0
    per_symbol: dict[str, dict] = field(default_factory=dict)
    per_source: dict[int, dict] = field(default_factory=dict)
    signal_latency_ms: dict[str, float] = field(default_factory=dict)
    exec_latency_ms: dict[str, float] = field(default_factory=dict)
    slippage_points: dict[str, float] = field(default_factory=dict)
    spread_points: dict[str, float] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    cumulative: list[tuple[datetime, float]] = field(default_factory=list)
    daily_pnl: list[tuple[str, float]] = field(default_factory=list)


def _summ(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {}
    xs = sorted(xs)
    return {"avg": statistics.fmean(xs), "median": statistics.median(xs),
            "p95": xs[min(len(xs) - 1, int(len(xs) * 0.95))], "max": xs[-1], "n": len(xs)}


def _bucket(reason: str) -> str:
    r = (reason or "").lower()
    for key, label in (("spread", "Spread"), ("old", "Stale signal"), ("moved", "Price moved"),
                       ("zone", "Entry zone"), ("risk", "Risk limit"), ("margin", "Margin"),
                       ("duplicate", "Duplicate"), ("lockout", "Lockout"), ("emergency", "Emergency stop"),
                       ("concurrent", "Max trades"), ("minimum volume", "Min volume"),
                       ("not confirmed", "Not confirmed"), ("stop loss", "Stop loss"),
                       ("take profit", "Take profit"), ("tp", "Take profit"),
                       ("symbol", "Symbol"), ("connected", "Connectivity")):
        if key in r:
            return label
    return "Other"


def outcomes(db: Database, simulated: bool) -> list[TradeOutcome]:
    with db.session() as s:
        sigs = list(s.scalars(select(SignalRow).where(
            SignalRow.is_simulated == simulated, SignalRow.status == SignalStatus.CLOSED.value)))
        out = []
        for sig in sigs:
            legs = [l for l in sig.executions if l.state == LegState.CLOSED.value]
            net = sum((l.profit or 0) + (l.commission or 0) for l in legs)
            opened = min((l.opened_at for l in legs if l.opened_at), default=None)
            closed = max((l.closed_at for l in legs if l.closed_at), default=None)
            out.append(TradeOutcome(sig.id, sig.source_id, sig.symbol or "?", sig.direction or "",
                                    opened, closed, round(net, 2), sig.risk_money))
        return sorted(out, key=lambda o: o.closed or datetime.min.replace(tzinfo=timezone.utc))


def compute(db: Database, simulated: bool) -> Stats:
    st = Stats(simulated=simulated)
    outs = outcomes(db, simulated)
    st.trades = len(outs)
    wins = [o.net for o in outs if o.net > 0]
    losses = [o.net for o in outs if o.net < 0]
    st.wins, st.losses = len(wins), len(losses)
    st.win_rate = st.wins / st.trades * 100 if st.trades else 0.0
    st.gross_profit, st.gross_loss = sum(wins), sum(losses)
    st.net = st.gross_profit + st.gross_loss
    st.profit_factor = (st.gross_profit / -st.gross_loss) if st.gross_loss else None
    st.expectancy = st.net / st.trades if st.trades else 0.0
    st.avg_win = statistics.fmean(wins) if wins else 0.0
    st.avg_loss = statistics.fmean(losses) if losses else 0.0
    rs = [o.r_multiple for o in outs if o.r_multiple is not None]
    st.avg_r = statistics.fmean(rs) if rs else None

    cum, peak, mdd = 0.0, 0.0, 0.0
    daily: dict[str, float] = defaultdict(float)
    for o in outs:
        cum += o.net
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
        if o.closed:
            st.cumulative.append((o.closed, round(cum, 2)))
            daily[o.closed.date().isoformat()] += o.net
    st.max_drawdown = round(mdd, 2)
    st.daily_pnl = sorted((k, round(v, 2)) for k, v in daily.items())

    for key, attr in (("symbol", "per_symbol"), ("source_id", "per_source")):
        groups: dict = defaultdict(list)
        for o in outs:
            groups[getattr(o, key)].append(o.net)
        setattr(st, attr, {k: {"trades": len(v), "net": round(sum(v), 2),
                               "win_rate": sum(1 for x in v if x > 0) / len(v) * 100}
                           for k, v in groups.items()})

    with db.session() as s:
        sigs = list(s.scalars(select(SignalRow).where(SignalRow.is_simulated == simulated,
                                                      SignalRow.kind == MessageKind.NEW_SIGNAL.value)))
        st.status_counts = dict(Counter(x.status for x in sigs))
        st.rejections = dict(Counter(_bucket(x.reject_reason) for x in sigs
                                     if x.status in (SignalStatus.REJECTED.value,
                                                     SignalStatus.EXPIRED.value,
                                                     SignalStatus.FAILED.value)))
        st.signal_latency_ms = _summ([x.latency_ms for x in sigs if x.latency_ms is not None])
        legs = list(s.scalars(select(ExecutionRow).where(ExecutionRow.is_simulated == simulated)))
        st.exec_latency_ms = _summ([l.latency_ms for l in legs if l.latency_ms is not None])
        st.slippage_points = _summ([l.slippage_points for l in legs if l.slippage_points is not None])
        st.spread_points = _summ([l.spread_points for l in legs if l.spread_points is not None])
        st.equity_curve = [(e.ts, e.equity) for e in s.scalars(
            select(EquitySnapshotRow).where(EquitySnapshotRow.is_simulated == simulated)
            .order_by(EquitySnapshotRow.ts))]
    return st


def export_csv(db: Database, simulated: bool) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["mode", "signal_id", "source_id", "symbol", "direction", "leg", "ticket", "volume",
                "entry", "sl", "tp", "close_price", "profit", "commission", "opened_at",
                "closed_at", "state"])
    with db.session() as s:
        for l in s.scalars(select(ExecutionRow).where(ExecutionRow.is_simulated == simulated)
                           .order_by(ExecutionRow.id)):
            w.writerow(["PAPER" if simulated else "DEMO", l.signal_id, l.signal.source_id, l.symbol,
                        l.direction, l.leg_index, l.position_ticket or l.order_ticket, l.volume,
                        l.fill_price, l.sl, l.tp, l.close_price, l.profit, l.commission,
                        l.opened_at, l.closed_at, l.state])
    return buf.getvalue()
