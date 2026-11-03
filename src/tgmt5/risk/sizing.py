"""Position sizing driven by the broker's own contract specifications.

The monetary loss at the stop is obtained from ``order_calc_profit`` (exactly
what the terminal would compute, including currency conversion).  Only if the
terminal cannot provide it do we fall back to ``trade_tick_value_loss`` /
``trade_tick_size`` from the symbol specification.  No contract sizes or pip
values are ever hard-coded.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

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
    per_lot, method = loss_at_stop(gw, spec, direction, 1.0, entry, stop)
    if per_lot <= 0:
        raise SizingError("could not determine loss per lot")
    raw = risk_money / per_lot
    cap = spec.volume_max if max_volume is None else min(spec.volume_max, max_volume)
    vol = floor_to_step(min(raw, cap), spec.volume_step)
    if vol < spec.volume_min - _EPS:
        raise SizingError(
            f"minimum volume {spec.volume_min} would risk {per_lot * spec.volume_min:.2f}, "
            f"above the budget of {risk_money:.2f}")
    # verify with the broker calculation at the final volume (handles non-linearities/rounding)
    actual, _ = loss_at_stop(gw, spec, direction, vol, entry, stop)
    while actual > risk_money + 0.01 and vol > spec.volume_min + _EPS:
        vol = floor_to_step(vol - spec.volume_step, spec.volume_step)
        actual, _ = loss_at_stop(gw, spec, direction, vol, entry, stop)
    if actual > risk_money + 0.01:
        raise SizingError(f"minimum volume exceeds risk budget ({actual:.2f} > {risk_money:.2f})")
    return SizingResult(volume=vol, risk_money=round(actual, 2), loss_per_lot=per_lot, method=method)


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
