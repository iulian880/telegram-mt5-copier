"""Pre-trade review of the critical risk settings.

Each critical field is shown in red with a tick button on the Risk page.  Ticking it
stores the field's current value in ``AppConfig.reviewed``; if the value changes later
the field turns red again.  DEMO auto-execution cannot be enabled while any required
field is unreviewed (see ``AppService.set_mode``).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tgmt5.config import PER_TP_MODES, SizingMode, TpStrategy

if TYPE_CHECKING:
    from tgmt5.config import AppConfig

#: key -> label shown to the user (in the order of the Risk page)
REVIEW_LABELS: dict[str, str] = {
    "sizing_mode": "Sizing method",
    "size": "Size (lot / money / %)",
    "tp_strategy": "Multiple TP handling",
    "tp_allocation": "TP volume allocation",
    "small_volume_policy": "If the volume is too small",
    "max_lot_per_order": "Max lot per order",
    "max_concurrent_trades": "Max concurrent trades",
    "min_free_margin": "Min free margin",
    "percent_caps_enabled": "Percentage risk caps (on/off)",
    "max_risk_per_signal_pct": "Max risk per signal",
    "max_total_open_risk_pct": "Max total open risk",
    "drawdown_protection": "Drawdown protection (on/off)",
    "max_daily_loss_pct": "Daily loss lockout",
    "max_drawdown_pct": "Max drawdown lockout",
    "move_sl_to_be_after_tp": "Move SL to breakeven after TP",
    "max_entry_deviation_sl_fraction": "Entry tolerance",
}


def _size_value(cfg: "AppConfig") -> object:
    r = cfg.risk
    return {
        SizingMode.LOT_PER_TP: r.lot_per_tp,
        SizingMode.LOT_PER_BALANCE: [r.lot_per_tp, r.balance_step],
        SizingMode.FIXED_LOT: r.fixed_lot,
        SizingMode.FIXED_MONEY: r.fixed_money,
        SizingMode.PCT_EQUITY: r.risk_percent,
        SizingMode.PCT_BALANCE: r.risk_percent,
    }[r.sizing_mode]


def review_value(cfg: "AppConfig", key: str) -> str:
    """Canonical string of the value a review stamp refers to."""
    r, e = cfg.risk, cfg.execution
    if key == "size":
        value: object = [r.sizing_mode.value, _size_value(cfg)]
    elif key == "tp_allocation":
        value = e.tp_allocation
    elif hasattr(r, key):
        value = getattr(r, key)
    else:
        value = getattr(e, key)
    if hasattr(value, "value"):          # enums
        value = value.value
    return json.dumps(value)


def required_keys(cfg: "AppConfig") -> list[str]:
    """Critical fields that must be reviewed for the current settings."""
    r, e = cfg.risk, cfg.execution
    keys = []
    for k in REVIEW_LABELS:
        if k == "tp_allocation" and (r.sizing_mode in PER_TP_MODES or e.tp_strategy is not TpStrategy.SPLIT):
            continue
        if k in ("max_risk_per_signal_pct", "max_total_open_risk_pct") and not r.percent_caps_enabled:
            continue
        if k in ("max_daily_loss_pct", "max_drawdown_pct") and not r.drawdown_protection:
            continue
        keys.append(k)
    return keys


def pending_reviews(cfg: "AppConfig") -> list[str]:
    """Required fields that are not ticked, or whose value changed since they were ticked."""
    return [k for k in required_keys(cfg) if cfg.reviewed.get(k) != review_value(cfg, k)]
