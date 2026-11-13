"""Position sizing driven by the broker's own contract specifications.

The monetary loss at the stop is obtained from ``order_calc_profit`` (exactly
what the terminal would compute, including currency conversion).  Only if the
terminal cannot provide it do we fall back to ``trade_tick_value_loss`` /
``trade_tick_size`` from the symbol specification.  No contract sizes or pip
values are ever hard-coded.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from tgmt5.config import (
    LOT_MODES, PER_TP_MODES, RiskSettings, SizingMode, SmallVolumePolicy, TpStrategy,
)
from tgmt5.domain.models import Direction
from tgmt5.mt5.interface import MT5Gateway, OrderType, SymbolSpec

_EPS = 1e-9


class SizingError(ValueError):
    pass


@dataclass(frozen=True)
class SizingResult:
    volume: float
    risk_money: float          # loss at SL for `volume`, account currency
    loss_per_lot: float
    method: str                # "order_calc_profit" | "tick_value"


def step_decimals(step: float) -> int:
    s = f"{step:.10f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def floor_to_step(volume: float, step: float) -> float:
    """Round *down* to the broker's volume step (never up)."""
    n = math.floor(volume / step + _EPS)
    return round(n * step, step_decimals(step))


def loss_at_stop(gw: MT5Gateway, spec: SymbolSpec, direction: Direction, volume: float,
                 entry: float, stop: float) -> tuple[float, str]:
    """Positive monetary loss (account currency) if `volume` lots hit `stop` from `entry`."""
    if direction.sign * (entry - stop) <= 0:
        raise SizingError("stop loss is not on the losing side of entry")
    otype = OrderType.BUY if direction is Direction.BUY else OrderType.SELL
    p = gw.calc_profit(otype, spec.name, volume, entry, stop)
    if p is not None and p < 0:
        return -p, "order_calc_profit"
    tick_value = spec.trade_tick_value_loss or spec.trade_tick_value
    if not tick_value or not spec.trade_tick_size:
        raise SizingError("broker did not provide profit calculation or tick value")
    ticks = abs(entry - stop) / spec.trade_tick_size
    return ticks * tick_value * volume, "tick_value"


def size_for_risk(gw: MT5Gateway, spec: SymbolSpec, direction: Direction, entry: float,
                  stop: float, risk_money: float, max_volume: float | None = None) -> SizingResult:
    """Largest broker-valid volume whose loss at SL does not exceed ``risk_money``."""
    if risk_money <= 0:
        raise SizingError("risk budget must be positive")
    method = loss_at_stop(gw, spec, direction, 1.0, entry, stop)[1]
    res = size_for_budget(spec, lambda v: loss_at_stop(gw, spec, direction, v, entry, stop)[0],
                          risk_money, max_volume)
    return SizingResult(res.volume, res.risk_money, res.loss_per_lot, method)


def size_for_budget(spec: SymbolSpec, loss_fn: Callable[[float], float], risk_money: float,
                    max_volume: float | None = None) -> SizingResult:
    """Largest broker-valid volume whose loss at SL (``loss_fn(volume)``) fits ``risk_money``."""
    if risk_money <= 0:
        raise SizingError("risk budget must be positive")
    per_lot = loss_fn(1.0)
    if per_lot <= 0:
        raise SizingError("could not determine loss per lot")
    raw = risk_money / per_lot
    cap = spec.volume_max if max_volume is None else min(spec.volume_max, max_volume)
    vol = floor_to_step(min(raw, cap), spec.volume_step)
    if vol < spec.volume_min - _EPS:
        raise SizingError(
            f"minimum volume {spec.volume_min:g} would risk {per_lot * spec.volume_min:.2f}, "
            f"above the budget of {risk_money:.2f}")
    # verify with the broker calculation at the final volume (handles non-linearities/rounding)
    actual = loss_fn(vol)
    while actual > risk_money + 0.01 and vol > spec.volume_min + _EPS:
        vol = floor_to_step(vol - spec.volume_step, spec.volume_step)
        actual = loss_fn(vol)
    if actual > risk_money + 0.01:
        raise SizingError(f"minimum volume exceeds risk budget ({actual:.2f} > {risk_money:.2f})")
    return SizingResult(volume=vol, risk_money=round(actual, 2), loss_per_lot=per_lot, method="loss_fn")


def fixed_lot(gw: MT5Gateway, spec: SymbolSpec, direction: Direction, entry: float, stop: float,
              lots: float) -> SizingResult:
    vol = floor_to_step(min(lots, spec.volume_max), spec.volume_step)
    if vol < spec.volume_min - _EPS:
        raise SizingError(f"fixed lot {lots} below broker minimum {spec.volume_min}")
    actual, method = loss_at_stop(gw, spec, direction, vol, entry, stop)
    per_lot, _ = loss_at_stop(gw, spec, direction, 1.0, entry, stop)
    return SizingResult(volume=vol, risk_money=round(actual, 2), loss_per_lot=per_lot, method=method)


def split_volume(total: float, weights: list[float], spec: SymbolSpec) -> list[float]:
    """Split ``total`` into legs proportional to ``weights``.

    Every leg is a multiple of the volume step and >= volume_min, and the sum of
    the legs never exceeds ``total`` (so the risk of the whole signal never
    exceeds what was sized for the total).  If the volume cannot be split into
    ``len(weights)`` valid legs an empty list is returned.
    """
    step = spec.volume_step
    n = len(weights)
    units = math.floor(total / step + _EPS)
    min_units = math.ceil(spec.volume_min / step - _EPS)
    if n == 0 or units < n * min_units:
        return []
    wsum = sum(weights)
    # ideal allocation in step units, but every leg gets at least the broker minimum
    ideal = [max(units * w / wsum, min_units) for w in weights]
    alloc = [max(math.floor(x + _EPS), min_units) for x in ideal]
    while sum(alloc) > units:                       # trim legs that are above their minimum
        i = max((k for k in range(n) if alloc[k] > min_units), key=lambda k: alloc[k] - ideal[k])
        alloc[i] -= 1
    rem = units - sum(alloc)                        # largest-remainder distribution
    order = sorted(range(n), key=lambda k: ideal[k] - alloc[k], reverse=True)
    for k in order[:rem]:
        alloc[k] += 1
    d = step_decimals(step)
    legs = [round(a * step, d) for a in alloc]
    assert sum(legs) <= total + _EPS
    return legs


# --------------------------------------------------------------------------- planning
@dataclass
class SizingPlan:
    """Volumes for one signal. ``volumes[i]`` is the position for TP(i+1); a single entry
    means one position (single TP, managed partials or no TP)."""
    volumes: list[float] = field(default_factory=list)
    risk_money: float = 0.0             # loss if the SL is hit, whole signal, account currency
    budget: float | None = None         # planned money at risk (None for lot-based modes)
    over_budget_allowed: bool = False   # minimum-lot policy chose to exceed the budget
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def total(self) -> float:
        return round(sum(self.volumes), 8)

    @property
    def within_budget(self) -> bool:
        return self.budget is None or self.over_budget_allowed or self.risk_money <= self.budget + 0.01


def signal_budget(r: RiskSettings, equity: float, balance: float, multiplier: float = 1.0) -> float | None:
    """Money the sizing method plans to risk on one signal; None for lot-based methods."""
    if r.sizing_mode in LOT_MODES:
        return None
    if r.sizing_mode is SizingMode.PCT_EQUITY:
        budget = equity * r.risk_percent / 100
    elif r.sizing_mode is SizingMode.PCT_BALANCE:
        budget = balance * r.risk_percent / 100
    else:                                   # FIXED_MONEY
        budget = r.fixed_money
    budget *= multiplier
    if r.percent_caps_enabled:
        budget = min(budget, equity * r.max_risk_per_signal_pct / 100)
    return budget


def lot_per_tp_for(r: RiskSettings, balance: float) -> float:
    """Lot per TP for LOT_PER_TP, or the balance-scaled lot for LOT_PER_BALANCE."""
    if r.sizing_mode is SizingMode.LOT_PER_BALANCE:
        return r.lot_per_tp * max(1, math.floor(balance / r.balance_step + _EPS))
    return r.lot_per_tp


def tp_layout(strategy: TpStrategy, n_tps: int, hedging: bool) -> tuple[TpStrategy, int, int]:
    """(effective strategy, positions to open, TPs one position stands for).

    SPLIT needs a hedging account (several positions on one symbol); on netting accounts it
    becomes one managed position with partial closes at the earlier TPs.
    """
    if strategy is TpStrategy.SPLIT and not hedging:
        strategy = TpStrategy.SINGLE_MANAGED
    if n_tps <= 1:
        return strategy, 1, 1
    if strategy is TpStrategy.SPLIT:
        return strategy, n_tps, 1
    if strategy is TpStrategy.SINGLE_MANAGED:
        return strategy, 1, n_tps
    return strategy, 1, 1                   # FIRST_TP_ONLY


def plan_volumes(r: RiskSettings, *, positions: int, tps_covered: int, allocation: list[float],
                 spec: SymbolSpec, loss_fn: Callable[[float], float], equity: float, balance: float,
                 multiplier: float = 1.0, max_total: float | None = None) -> SizingPlan:
    """Decide the volume of every position of a signal.

    ``positions``: how many positions the TP strategy wants (one per TP when splitting).
    ``tps_covered``: how many TPs one position stands for (a managed single position covers all).
    ``loss_fn(volume)``: loss at the SL for that total volume (broker calculation in the engine).
    This is the only place sizing decisions are made; the engine and the Risk page preview
    both call it, so the preview shows exactly what the engine would do.
    """
    plan = SizingPlan()
    step, vmin = spec.volume_step, spec.volume_min
    policy = r.small_volume_policy
    weights = (list(allocation) + [allocation[-1]] * positions)[:positions]
    try:
        if r.sizing_mode in PER_TP_MODES:
            want = lot_per_tp_for(r, balance) * multiplier
            leg = floor_to_step(min(want, spec.volume_max), step)
            if leg < vmin - _EPS:
                if policy is not SmallVolumePolicy.MIN_PER_TP:
                    plan.error = f"lot per TP {want:g} is below the broker minimum {vmin:g}"
                    return plan
                plan.notes.append(f"lot per TP {want:g} is below the broker minimum; using {vmin:g}")
                leg = vmin
            if positions > 1:
                vols = [leg] * positions
            else:
                vols = [floor_to_step(min(leg * max(1, tps_covered), spec.volume_max), step)]
        else:
            plan.budget = signal_budget(r, equity, balance, multiplier)
            if r.sizing_mode is SizingMode.FIXED_LOT:
                total = floor_to_step(min(r.fixed_lot * multiplier, spec.volume_max), step)
            else:
                try:
                    total = size_for_budget(spec, loss_fn, plan.budget or 0.0, max_total).volume
                except SizingError as e:
                    if policy is not SmallVolumePolicy.MIN_PER_TP:
                        plan.error = str(e)
                        return plan
                    total = 0.0
            vols = _fit(total, weights, spec, policy, plan)
            if plan.error:
                return plan
        plan.volumes = vols
        plan.risk_money = round(loss_fn(plan.total), 2)
    except SizingError as e:
        plan.volumes, plan.error = [], str(e)
    return plan


def _fit(total: float, weights: list[float], spec: SymbolSpec, policy: SmallVolumePolicy,
         plan: SizingPlan) -> list[float]:
    """Split ``total`` over the TP positions, applying the small-volume policy."""
    vmin, n = spec.volume_min, len(weights)
    if n == 1:
        if total >= vmin - _EPS:
            return [total]
        if policy is SmallVolumePolicy.MIN_PER_TP:
            plan.notes.append(f"volume {total:g} is below the broker minimum; using {vmin:g}")
            plan.over_budget_allowed = True
            return [vmin]
        plan.error = f"volume {total:g} is below the broker minimum {vmin:g}"
        return []
    legs = split_volume(total, weights, spec)
    if legs:
        return legs
    if policy is SmallVolumePolicy.REJECT:
        plan.error = f"volume {total:g} is too small for {n} TPs (minimum {vmin:g} each)"
        return []
    if policy is SmallVolumePolicy.MIN_PER_TP:
        plan.notes.append(f"volume {total:g} is too small for {n} TPs; using {vmin:g} on every TP")
        plan.over_budget_allowed = True
        return [vmin] * n
    # REDUCE: as many TPs as the volume allows, starting with TP1
    k = min(n - 1, math.floor(total / spec.volume_step + _EPS) //
            max(1, math.ceil(vmin / spec.volume_step - _EPS)))
    if k < 1:
        plan.error = f"volume {total:g} is below the broker minimum {vmin:g}"
        return []
    plan.notes.append(f"volume {total:g} is too small for {n} TPs; using TP1-TP{k} only")
    return split_volume(total, weights[:k], spec)
