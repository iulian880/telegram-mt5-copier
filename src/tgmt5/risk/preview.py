""""What would be opened?" preview for the Risk page.

Runs the engine's own sizing planner (:func:`tgmt5.risk.sizing.plan_volumes`) and the main
risk checks against example signals.  With a connected MT5 account it uses the detected
balance/equity and the broker's own calculations (``order_calc_profit`` / ``order_calc_margin``),
so the lots and money shown are exactly what the engine would use for a signal arriving now.
Without a connection it uses an example balance and an approximate pip value.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from tgmt5.config import AppConfig, TpStrategy
from tgmt5.domain.models import Direction
from tgmt5.mt5.interface import MT5Gateway, OrderType, SymbolSpec
from tgmt5.mt5.sim import fx_spec
from tgmt5.mt5.symbols import SymbolResolver
from tgmt5.risk.sizing import SizingError, loss_at_stop, plan_volumes, tp_layout

#: symbols of the example signal set (the pairs the Lingrid-style channels post)
DEFAULT_SYMBOLS = ("EURUSD", "GBPUSD", "AUDUSD", "USDCHF", "USDCAD", "GBPCAD", "GBPNZD")
#: offline approximation: money per pip for 1.0 lot (≈ USD-quoted majors)
APPROX_PIP_VALUE = 10.0
NOT_AT_BROKER = "not available at this broker"


@dataclass(frozen=True)
class ExampleSignal:
    symbol: str
    direction: Direction
    n_tps: int
    sl_pips: float = 0.0              # used when entry/stop_loss are not given
    entry: float | None = None        # None = current market price
    stop_loss: float | None = None
    label: str = "example"


@dataclass
class PreviewRow:
    symbol: str
    label: str
    sl_pips: float
    positions: list[tuple[str, float]]   # ("TP1", 0.01), ... or ("TP1-TP3", 0.03) for one position
    total: float = 0.0
    risk_money: float | None = None
    risk_pct: float | None = None
    margin: float | None = None
    ok: bool = False
    reason: str = ""
    notes: list[str] = field(default_factory=list)


@dataclass
class SizingPreview:
    detected: bool                # True = real account values and broker calculations
    account: str                  # human description of the basis
    currency: str
    equity: float
    balance: float
    hedging: bool
    rows: list[PreviewRow] = field(default_factory=list)
    summary: list[str] = field(default_factory=list)


def pip_size(spec: SymbolSpec) -> float:
    return spec.point * 10 if spec.digits in (3, 5) else spec.point


def build_preview(gw: MT5Gateway | None, resolver: SymbolResolver | None, cfg: AppConfig,
                  examples: list[ExampleSignal], *, fallback_balance: float) -> SizingPreview:
    acct = gw.account_info() if gw is not None else None
    if acct is not None:
        pv = SizingPreview(True, f"{acct.login} @ {acct.server} (detected from MT5)", acct.currency,
                           acct.equity, acct.balance, acct.is_hedging)
    else:
        pv = SizingPreview(False, "not connected: example balance, approximate pip values", "",
                           fallback_balance, fallback_balance, True)
    for ex in examples:
        pv.rows.append(_row(gw, resolver, cfg, ex, pv, acct))
    pv.summary = _summary(cfg, pv, acct)
    return pv


def _row(gw, resolver, cfg: AppConfig, ex: ExampleSignal, pv: SizingPreview, acct) -> PreviewRow:
    r, e = cfg.risk, cfg.execution
    row = PreviewRow(ex.symbol, ex.label, ex.sl_pips, [])
    direction = ex.direction
    if pv.detected:
        sym = resolver.resolve(ex.symbol) if resolver else None
        spec = gw.symbol_spec(sym) if sym else None
        tick = gw.tick(sym) if sym else None
        if spec is None or tick is None:
            row.reason = NOT_AT_BROKER
            return row
        entry = ex.entry if ex.entry is not None else (tick.ask if direction is Direction.BUY else tick.bid)
        sl = ex.stop_loss if ex.stop_loss is not None else entry - direction.sign * ex.sl_pips * pip_size(spec)
        row.sl_pips = round(abs(entry - sl) / pip_size(spec), 1)

        def loss_fn(v: float) -> float:
            return loss_at_stop(gw, spec, direction, v, entry, sl)[0]
    else:
        spec = fx_spec(ex.symbol, digits=3 if "JPY" in ex.symbol else 5)
        if ex.entry is not None and ex.stop_loss is not None:
            row.sl_pips = round(abs(ex.entry - ex.stop_loss) / pip_size(spec), 1)
        sl_pips = row.sl_pips

        def loss_fn(v: float) -> float:
            return v * sl_pips * APPROX_PIP_VALUE

    prof = r.symbol_profiles.get(ex.symbol)
    mult = prof.risk_multiplier if prof else 1.0
    strategy, positions, covered = tp_layout(e.tp_strategy, ex.n_tps, pv.hedging)
    try:
        plan = plan_volumes(r, positions=positions, tps_covered=covered, allocation=e.tp_allocation,
                            spec=spec, loss_fn=loss_fn, equity=pv.equity, balance=pv.balance,
                            multiplier=mult,
                            max_total=min(r.max_lot_per_order * max(1, ex.n_tps), r.max_lots_per_symbol))
    except SizingError as err:
        row.reason = str(err)
        return row
    row.notes = list(plan.notes)
    if plan.error:
        row.reason = plan.error
        return row
    if ex.n_tps == 0:
        row.positions = [("no TP", plan.volumes[0])]
    elif len(plan.volumes) > 1:
        row.positions = [(f"TP{i + 1}", v) for i, v in enumerate(plan.volumes)]
    elif strategy is TpStrategy.SINGLE_MANAGED and ex.n_tps > 1:
        row.positions = [(f"TP1-TP{ex.n_tps} (one position, partial closes)", plan.volumes[0])]
    else:
        row.positions = [("TP1", plan.volumes[0])]
    row.total = plan.total
    row.risk_money = plan.risk_money
    row.risk_pct = plan.risk_money / pv.equity * 100 if pv.equity > 0 else None

    problems: list[str] = []
    if not plan.within_budget:
        problems.append(f"risk {plan.risk_money:.2f} exceeds the budget {plan.budget or 0:.2f}")
    if r.percent_caps_enabled:
        cap = pv.equity * r.max_risk_per_signal_pct / 100
        if plan.risk_money > cap + 0.01:
            problems.append(f"risk {plan.risk_money:.2f} > max risk per signal {cap:.2f} "
                            f"({r.max_risk_per_signal_pct:g}%)")
    if any(v > r.max_lot_per_order + 1e-9 for v in plan.volumes):
        problems.append(f"a position is above the max lot per order {r.max_lot_per_order:g}")
    if plan.total > r.max_lots_per_symbol + 1e-9:
        problems.append(f"total {plan.total:g} lots > max lots per symbol {r.max_lots_per_symbol:g}")
    if pv.detected and acct is not None:
        otype = OrderType.BUY if direction is Direction.BUY else OrderType.SELL
        m = gw.calc_margin(otype, spec.name, plan.total, entry)
        row.margin = m
        if m is not None and acct.equity > 0:
            usage = (acct.margin + m) / acct.equity * 100
            if usage > r.max_margin_usage_pct:
                problems.append(f"margin usage would be {usage:.0f}% (max {r.max_margin_usage_pct:g}%)")
            if acct.margin_free - m < r.min_free_margin:
                problems.append(f"free margin after the trade {acct.margin_free - m:.2f} "
                                f"< min free margin {r.min_free_margin:g}")
    row.ok = not problems
    row.reason = "; ".join(problems)
    return row


def _summary(cfg: AppConfig, pv: SizingPreview, acct) -> list[str]:
    r = cfg.risk
    ccy = f" {pv.currency}" if pv.currency else ""
    out: list[str] = []
    ok = [x for x in pv.rows if x.ok and x.risk_money is not None]
    n = r.max_concurrent_trades
    if ok and pv.equity > 0:
        worst = max(x.risk_money for x in ok)
        avg = sum(x.risk_money for x in ok) / len(ok)
        total = n * avg
        line = (f"If {n} signals (max concurrent trades) are open together and all hit their stop loss: "
                f"about -{total:,.2f}{ccy} ({total / pv.equity * 100:.1f}% of equity); "
                f"largest single signal -{worst:,.2f}{ccy}.")
        if r.percent_caps_enabled:
            cap = pv.equity * r.max_total_open_risk_pct / 100
            fits = max(0, math.floor(cap / avg)) if avg > 0 else n
            line += f" The total open risk cap ({cap:,.2f}{ccy}) allows about {min(fits, n)} at once."
        out.append(line)
        margins = [x.margin for x in ok if x.margin]
        if acct is not None and margins:
            m = sum(margins) / len(margins)
            k = max(0, math.floor((acct.margin_free - r.min_free_margin) / m)) if m > 0 else 0
            out.append(f"Margin per signal about {m:,.2f}{ccy}; free margin {acct.margin_free:,.2f}{ccy} "
                       f"allows about {k} more signal(s) of this size (keeping {r.min_free_margin:g}{ccy} free).")
    missing = [x.symbol for x in pv.rows if x.reason == NOT_AT_BROKER]
    rejected = [x for x in pv.rows if not x.ok and x.reason != NOT_AT_BROKER]
    for x in rejected:
        out.append(f"{x.symbol} would NOT open: {x.reason}.")
    if any("minimum volume" in x.reason or "max risk per signal" in x.reason for x in rejected):
        out.append("Tip for small accounts: choose 'Fixed lot on every TP' (or set 'If the volume is too "
                   "small' to the minimum lot) and switch off the percentage risk caps, or use the "
                   "'Small account' preset.")
    if missing:
        out.append("Not offered by this broker / account: " + ", ".join(missing) + ".")
    if not r.percent_caps_enabled:
        out.append("Percentage risk caps are OFF: risk per signal and total open risk are not limited by %.")
    if not r.drawdown_protection:
        out.append("Drawdown protection is OFF: no daily-loss, drawdown or losing-streak lockouts.")
    if not pv.detected:
        out.append(f"Not connected to MT5: using an example balance and about {APPROX_PIP_VALUE:g} per pip "
                   f"per lot. Connect MT5 for exact broker values.")
    return out
