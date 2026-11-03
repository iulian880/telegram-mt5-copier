"""Risk engine: account-level protections and per-trade limit checks.

This module is the single authority on whether risk permits a trade.  It is
called by the execution engine for every order and cannot be bypassed from the
GUI (the GUI has no access to the gateway's ``send``).  Lockout state is
persisted so that it survives restarts.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from tgmt5.config import RiskSettings
from tgmt5.domain.models import Decision, Direction
from tgmt5.mt5.interface import AccountInfo, MT5Gateway, Position

log = logging.getLogger(__name__)
STATE_KEY = "risk_state"


@dataclass
class RiskState:
    emergency_stop: bool = False
    emergency_reason: str = ""
    daily_lockout_day: str = ""           # YYYY-MM-DD of an active daily-loss lockout
    daily_lockout_reason: str = ""
    drawdown_lockout: bool = False         # requires manual reset
    drawdown_reason: str = ""
    breaker_tripped: bool = False          # consecutive-loss breaker; manual reset
    consecutive_losses: int = 0
    equity_peak: float = 0.0
    day: str = ""
    day_start_equity: float = 0.0
    confirmed_account: str = ""            # "login@server" the user confirmed for automated DEMO exec
    trade_times: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class OpenRisk:
    symbol: str          # canonical
    risk_money: float
    volume: float


@dataclass(frozen=True)
class TradeProposal:
    symbol: str          # canonical
    broker_symbol: str
    direction: Direction
    entry: float
    stop_loss: float
    volumes: tuple[float, ...]
    risk_money: float
    margin_required: float
    take_profits: tuple[float, ...]


class RiskEngine:
    def __init__(self, settings: Callable[[], RiskSettings],
                 load: Callable[[], dict | None], save: Callable[[dict], None],
                 clock: Callable[[], datetime] | None = None) -> None:
        self._settings = settings
        self._save = save
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        raw = load() or {}
        known = {k: v for k, v in raw.items() if k in RiskState.__dataclass_fields__}
        self.state = RiskState(**known)
        self.mt5_unstable = False

    @property
    def settings(self) -> RiskSettings:
        return self._settings()

    def persist(self) -> None:
        self._save(asdict(self.state))

    # --------------------------------------------------------- lockouts
    def emergency(self, reason: str) -> None:
        self.state.emergency_stop = True
        self.state.emergency_reason = reason
        self.persist()
        log.warning("EMERGENCY STOP engaged: %s", reason)

    def clear_emergency(self) -> None:
        self.state.emergency_stop = False
        self.state.emergency_reason = ""
        self.persist()

    def reset_drawdown_lockout(self, current_equity: float) -> None:
        self.state.drawdown_lockout = False
        self.state.drawdown_reason = ""
        self.state.equity_peak = current_equity
        self.persist()

    def reset_breaker(self) -> None:
        self.state.breaker_tripped = False
        self.state.consecutive_losses = 0
        self.persist()

    def blocking_reason(self) -> str | None:
        s = self.state
        today = self._clock().date().isoformat()
        if s.emergency_stop:
            return f"Emergency stop active ({s.emergency_reason})"
        if s.drawdown_lockout:
            return f"Drawdown lockout ({s.drawdown_reason})"
        if s.daily_lockout_day == today:
            return f"Daily loss lockout ({s.daily_lockout_reason})"
        if s.breaker_tripped:
            return f"Consecutive-loss circuit breaker ({s.consecutive_losses} losses)"
        if self.mt5_unstable:
            return "MT5 connectivity unstable - execution paused"
        return None

    def on_account_update(self, acct: AccountInfo) -> None:
        """Update day/peak tracking and trip equity lockouts. Call on every account poll."""
        s = self.state
        cfg = self.settings
        today = self._clock().date().isoformat()
        changed = False
        if s.day != today:
            s.day = today
            s.day_start_equity = acct.equity
            changed = True
        if acct.equity > s.equity_peak:
            s.equity_peak = acct.equity
            changed = True
        if s.day_start_equity > 0:
            day_loss_pct = (s.day_start_equity - acct.equity) / s.day_start_equity * 100
            if day_loss_pct >= cfg.max_daily_loss_pct and s.daily_lockout_day != today:
                s.daily_lockout_day = today
                s.daily_lockout_reason = f"daily loss {day_loss_pct:.2f}% >= {cfg.max_daily_loss_pct}%"
                changed = True
        if s.equity_peak > 0:
            dd = (s.equity_peak - acct.equity) / s.equity_peak * 100
            if dd >= cfg.max_drawdown_pct and not s.drawdown_lockout:
                s.drawdown_lockout = True
                s.drawdown_reason = f"drawdown {dd:.2f}% >= {cfg.max_drawdown_pct}%"
                changed = True
        if changed:
            self.persist()

    def on_signal_closed(self, net_profit: float) -> None:
        s = self.state
        if net_profit < 0:
            s.consecutive_losses += 1
        elif net_profit > 0:
            s.consecutive_losses = 0
        lim = self.settings.max_consecutive_losses
        if lim and s.consecutive_losses >= lim:
            s.breaker_tripped = True
        self.persist()

    def record_trade(self) -> None:
        now = self._clock().timestamp()
        self.state.trade_times = [t for t in self.state.trade_times if now - t < 3600] + [now]
        self.persist()

    # ------------------------------------------------------------ metrics
    def daily_pnl(self, acct: AccountInfo) -> float:
        return acct.equity - self.state.day_start_equity if self.state.day_start_equity else 0.0

    def drawdown_pct(self, acct: AccountInfo) -> float:
        peak = max(self.state.equity_peak, acct.equity)
        return (peak - acct.equity) / peak * 100 if peak > 0 else 0.0

    # --------------------------------------------------------- per trade
    def risk_budget(self, acct: AccountInfo, multiplier: float = 1.0) -> float:
        """Monetary risk allowed for one signal (already capped at max_risk_per_signal)."""
        from tgmt5.config import SizingMode
        cfg = self.settings
        if cfg.sizing_mode is SizingMode.PCT_EQUITY:
            budget = acct.equity * cfg.risk_percent / 100
        elif cfg.sizing_mode is SizingMode.PCT_BALANCE:
            budget = acct.balance * cfg.risk_percent / 100
        elif cfg.sizing_mode is SizingMode.FIXED_MONEY:
            budget = cfg.fixed_money
        else:  # FIXED_LOT: budget is only the hard cap
            budget = acct.equity * cfg.max_risk_per_signal_pct / 100
        budget *= multiplier
        return min(budget, acct.equity * cfg.max_risk_per_signal_pct / 100)

    def check(self, p: TradeProposal, acct: AccountInfo, open_risk: list[OpenRisk],
              active_trades: int, decision: Decision | None = None) -> Decision:
        cfg = self.settings
        d = decision or Decision(approved=True)
        now = self._clock()

        reason = self.blocking_reason()
        d.add("lockouts", reason is None, reason or "")
        d.add("whitelist", not cfg.symbol_whitelist or p.symbol in cfg.symbol_whitelist,
              f"{p.symbol} not in whitelist")
        d.add("blacklist", p.symbol not in cfg.symbol_blacklist, f"{p.symbol} is blacklisted")
        if cfg.trading_hours_utc:
            a, b = cfg.trading_hours_utc
            h = now.hour
            inside = a <= h < b if a <= b else (h >= a or h < b)
            d.add("session", inside, f"outside trading hours {a}-{b} UTC")
        d.add("stop_loss", p.stop_loss > 0, "no stop loss")
        total_vol = sum(p.volumes)
        prof = cfg.symbol_profiles.get(p.symbol)
        max_lot = min(cfg.max_lot_per_order, prof.max_lots if prof and prof.max_lots else 1e9)
        d.add("max_lot", all(v <= max_lot + 1e-9 for v in p.volumes),
              f"order volume exceeds {max_lot}")
        cap = acct.equity * cfg.max_risk_per_signal_pct / 100
        d.add("risk_per_signal", p.risk_money <= cap + 0.01,
              f"risk {p.risk_money:.2f} > cap {cap:.2f}")
        total_open = sum(r.risk_money for r in open_risk)
        tcap = acct.equity * cfg.max_total_open_risk_pct / 100
        d.add("total_open_risk", total_open + p.risk_money <= tcap + 0.01,
              f"open risk {total_open:.2f} + {p.risk_money:.2f} > {tcap:.2f}")
        for group, members in cfg.correlation_groups.items():
            if p.symbol in members:
                g = sum(r.risk_money for r in open_risk if r.symbol in members)
                gcap = acct.equity * cfg.max_correlated_risk_pct / 100
                d.add(f"correlated[{group}]", g + p.risk_money <= gcap + 0.01,
                      f"correlated risk {g + p.risk_money:.2f} > {gcap:.2f}")
        sym_vol = sum(r.volume for r in open_risk if r.symbol == p.symbol)
        d.add("symbol_exposure", sym_vol + total_vol <= cfg.max_lots_per_symbol + 1e-9,
              f"{p.symbol} exposure {sym_vol + total_vol:.2f} lots > {cfg.max_lots_per_symbol}")
        d.add("concurrent_trades", active_trades < cfg.max_concurrent_trades,
              f"{active_trades} active trades (max {cfg.max_concurrent_trades})")
        recent = [t for t in self.state.trade_times if now.timestamp() - t < 3600]
        d.add("trade_frequency", len(recent) < cfg.max_trades_per_hour,
              f"{len(recent)} trades in the last hour (max {cfg.max_trades_per_hour})")
        new_margin = acct.margin + p.margin_required
        usage = new_margin / acct.equity * 100 if acct.equity > 0 else 100
        d.add("margin_usage", usage <= cfg.max_margin_usage_pct,
              f"margin usage would be {usage:.1f}% (max {cfg.max_margin_usage_pct}%)")
        free_after = acct.margin_free - p.margin_required
        d.add("free_margin", free_after >= cfg.min_free_margin,
              f"free margin after trade {free_after:.2f} < {cfg.min_free_margin}")
        if cfg.min_risk_reward > 0 and p.take_profits:
            rr = abs(p.take_profits[0] - p.entry) / abs(p.entry - p.stop_loss)
            d.add("risk_reward", rr >= cfg.min_risk_reward,
                  f"R:R {rr:.2f} < {cfg.min_risk_reward}")
        return d


def position_open_risk(gw: MT5Gateway, pos: Position) -> float:
    """Remaining loss if the position's SL is hit, measured from its open price.

    Zero if the SL is at/through break-even; a position without SL is treated
    as unbounded risk (equity) so that it blocks new trades.
    """
    if not pos.sl:
        acct = gw.account_info()
        return acct.equity if acct else float("inf")
    p = gw.calc_profit(pos.type, pos.symbol, pos.volume, pos.price_open, pos.sl)
    if p is None:
        spec = gw.symbol_spec(pos.symbol)
        if spec is None:
            return float("inf")
        sign = 1 if pos.type == 0 else -1
        p = sign * (pos.sl - pos.price_open) / spec.trade_tick_size * \
            (spec.trade_tick_value_loss or spec.trade_tick_value) * pos.volume
    return max(0.0, -p)


def day_bounds(now: datetime) -> tuple[float, float]:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()
