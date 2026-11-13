"""Validated, non-sensitive application configuration.

Secrets (Telegram API hash, session string, MT5 password) are NEVER part of
this model; they live in the OS keyring (see :mod:`tgmt5.secrets`).  This file
can therefore be exported / imported safely.
"""
from __future__ import annotations

import enum
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tgmt5.domain.models import ExecutionMode


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class SizingMode(str, enum.Enum):
    # simple
    LOT_PER_TP = "LOT_PER_TP"            # the same fixed lot on every TP (3 TPs x 0.01)
    FIXED_LOT = "FIXED_LOT"              # fixed total lot per signal, split across the TPs
    FIXED_MONEY = "FIXED_MONEY"          # fixed money at risk per signal (loss if SL is hit)
    # advanced
    PCT_EQUITY = "PCT_EQUITY"            # % of equity at risk per signal
    PCT_BALANCE = "PCT_BALANCE"          # % of balance at risk per signal
    LOT_PER_BALANCE = "LOT_PER_BALANCE"  # lot per TP that grows with balance (0.01 per 300)


#: modes where the lot is chosen directly (the money at risk follows from the stop loss)
LOT_MODES = frozenset({SizingMode.LOT_PER_TP, SizingMode.FIXED_LOT, SizingMode.LOT_PER_BALANCE})
#: modes where a lot is given for EVERY take profit (no allocation split)
PER_TP_MODES = frozenset({SizingMode.LOT_PER_TP, SizingMode.LOT_PER_BALANCE})


class SmallVolumePolicy(str, enum.Enum):
    """What to do when the calculated volume is below what the TPs need."""
    REDUCE = "REDUCE"            # use as many TPs as the volume allows; reject below the minimum lot
    MIN_PER_TP = "MIN_PER_TP"    # open the broker minimum on every TP (may risk more than planned)
    REJECT = "REJECT"            # skip the signal unless every TP can get a valid volume


class ZonePolicy(str, enum.Enum):
    WAIT = "WAIT"              # wait for price to enter the zone until expiry
    REJECT = "REJECT"          # reject if not already inside the zone
    PENDING = "PENDING"        # place a LIMIT order at the zone edge (explicit opt-in)


class TpStrategy(str, enum.Enum):
    SPLIT = "SPLIT"            # one position per TP (hedging accounts only)
    SINGLE_MANAGED = "SINGLE_MANAGED"   # one position, partial closes when TPs are reached
    FIRST_TP_ONLY = "FIRST_TP_ONLY"     # one position, TP = TP1


class TpHitAction(str, enum.Enum):
    IGNORE = "IGNORE"
    BREAKEVEN = "BREAKEVEN"


class SymbolProfile(_Strict):
    """Per-symbol overrides. Point-based limits are optional; fraction-of-SL limits always apply."""
    max_spread_points: float | None = Field(default=None, ge=0)
    max_entry_deviation_points: float | None = Field(default=None, ge=0)
    max_lots: float | None = Field(default=None, gt=0)
    risk_multiplier: float = Field(default=1.0, gt=0, le=1.0)


class RiskSettings(_Strict):
    sizing_mode: SizingMode = SizingMode.PCT_EQUITY
    risk_percent: float = Field(default=0.5, gt=0, le=10.0)
    fixed_lot: float = Field(default=0.01, gt=0, le=100)
    fixed_money: float = Field(default=50.0, gt=0)
    lot_per_tp: float = Field(default=0.01, gt=0, le=100)        # LOT_PER_TP / LOT_PER_BALANCE
    balance_step: float = Field(default=300.0, gt=0)              # LOT_PER_BALANCE: lot per this much
    small_volume_policy: SmallVolumePolicy = SmallVolumePolicy.REDUCE

    # Percentage caps (risk per signal, total open risk, correlated risk). Can be switched off
    # for small accounts, where one minimum lot is already several % of the account.
    percent_caps_enabled: bool = True
    max_risk_per_signal_pct: float = Field(default=1.0, gt=0, le=25.0)
    max_total_open_risk_pct: float = Field(default=3.0, gt=0, le=100.0)
    # Drawdown protection: daily-loss lockout, max-drawdown lockout, consecutive-loss breaker.
    drawdown_protection: bool = True
    max_daily_loss_pct: float = Field(default=3.0, gt=0, le=50.0)
    max_drawdown_pct: float = Field(default=10.0, gt=0, le=90.0)
    max_consecutive_losses: int = Field(default=4, ge=0)        # 0 = disabled
    max_concurrent_trades: int = Field(default=5, ge=1)
    max_trades_per_hour: int = Field(default=6, ge=1)
    max_lots_per_symbol: float = Field(default=5.0, gt=0)
    max_lot_per_order: float = Field(default=2.0, gt=0)
    max_margin_usage_pct: float = Field(default=30.0, gt=0, le=100)
    min_free_margin: float = Field(default=100.0, ge=0)

    # Unit-free price filters (fraction of stop-loss distance) - work for FX and metals alike
    max_spread_sl_fraction: float = Field(default=0.15, gt=0, le=1)
    max_entry_deviation_sl_fraction: float = Field(default=0.30, ge=0, le=2)
    max_slippage_points: int = Field(default=30, ge=0)
    min_risk_reward: float = Field(default=0.0, ge=0)            # 0 = disabled

    correlation_groups: dict[str, list[str]] = Field(default_factory=lambda: {
        "USD_LONG": ["USDJPY", "USDCHF", "USDCAD"],
        "USD_SHORT": ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "XAUUSD"],
    })
    max_correlated_risk_pct: float = Field(default=2.0, gt=0, le=100)

    symbol_whitelist: list[str] = Field(default_factory=list)
    symbol_blacklist: list[str] = Field(default_factory=list)
    symbol_profiles: dict[str, SymbolProfile] = Field(default_factory=dict)

    trading_hours_utc: tuple[int, int] | None = None   # e.g. (6, 20); None = unrestricted

    @model_validator(mode="after")
    def _check(self) -> "RiskSettings":
        if self.percent_caps_enabled and \
                self.sizing_mode in (SizingMode.PCT_BALANCE, SizingMode.PCT_EQUITY) and \
                self.risk_percent > self.max_risk_per_signal_pct:
            raise ValueError("risk_percent exceeds max_risk_per_signal_pct")
        if self.trading_hours_utc is not None:
            a, b = self.trading_hours_utc
            if not (0 <= a <= 23 and 0 <= b <= 24):
                raise ValueError("trading_hours_utc must be within 0..24")
        return self


class ExecutionSettings(_Strict):
    max_signal_age_seconds: int = Field(default=120, ge=5, le=3600)
    zone_policy: ZonePolicy = ZonePolicy.WAIT
    waiting_signal_expiry_minutes: int = Field(default=60, ge=1, le=24 * 60)
    pending_order_expiry_minutes: int = Field(default=240, ge=1, le=7 * 24 * 60)
    require_take_profit: bool = True
    tp_strategy: TpStrategy = TpStrategy.SPLIT
    tp_allocation: list[float] = Field(default_factory=lambda: [0.5, 0.3, 0.2])
    move_sl_to_be_after_tp: int = Field(default=1, ge=0)   # 0 = disabled
    breakeven_offset_points: int = Field(default=0, ge=0)
    trailing_enabled: bool = False
    trailing_distance_sl_fraction: float = Field(default=0.5, gt=0, le=2)
    on_tp_hit_message: TpHitAction = TpHitAction.IGNORE
    apply_edits_to_open_trades: bool = False
    cross_source_dedup_seconds: int = Field(default=300, ge=0)
    magic_number: int = Field(default=26110301, ge=1)
    reconcile_retry_once: bool = True

    @field_validator("tp_allocation")
    @classmethod
    def _alloc(cls, v: list[float]) -> list[float]:
        if not v or any(x <= 0 for x in v):
            raise ValueError("tp_allocation must contain positive weights")
        return v


class SourceConfig(_Strict):
    chat_id: int
    title: str = ""
    enabled: bool = True
    risk_multiplier: float = Field(default=1.0, gt=0, le=1.0)
    symbol_aliases: dict[str, str] = Field(default_factory=dict)   # e.g. {"GOLD": "XAUUSD"}
    template: str = "default"


class MT5Settings(_Strict):
    terminal_path: str = ""
    login: int | None = None
    server: str = ""
    symbol_suffix: str = ""                 # optional manual override; auto-discovered otherwise
    expected_currency: str = ""
    # Real-money accounts are refused unless this is switched on (and each account is then
    # confirmed by typing its number before REAL ACCOUNT mode can be enabled).
    allow_real_account: bool = False
    # Reconnect at start-up (monitor-only) when the password is saved in Credential Manager.
    auto_connect: bool = True


class PaperSettings(_Strict):
    starting_balance: float = Field(default=10_000.0, gt=0)
    extra_spread_points: float = Field(default=0.0, ge=0)
    slippage_points: float = Field(default=0.0, ge=0)
    commission_per_lot: float = Field(default=7.0, ge=0)


class NotificationSettings(_Strict):
    desktop: bool = True
    telegram_destination: str = ""          # e.g. "me" (Saved Messages). Empty = disabled
    on_executed: bool = True
    on_rejected: bool = True
    on_closed: bool = True


class AppConfig(_Strict):
    mode: ExecutionMode = ExecutionMode.MONITOR
    theme: str = "dark"
    risk: RiskSettings = Field(default_factory=RiskSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    sources: list[SourceConfig] = Field(default_factory=list)
    mt5: MT5Settings = Field(default_factory=MT5Settings)
    paper: PaperSettings = Field(default_factory=PaperSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    # Pre-trade review: critical risk field -> value it had when the user ticked it (see tgmt5.review)
    reviewed: dict[str, str] = Field(default_factory=dict)

    def mark_all_reviewed(self) -> None:
        """Tick every critical risk field at its current value (tests, scripted setups)."""
        from tgmt5.review import required_keys, review_value
        self.reviewed = {k: review_value(self, k) for k in required_keys(self)}

    def source(self, chat_id: int) -> SourceConfig | None:
        return next((s for s in self.sources if s.chat_id == chat_id), None)

    # --- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        if not path.exists():
            return cls()
        cfg = cls.model_validate_json(path.read_text(encoding="utf-8"))
        # Safety: the app ALWAYS starts in monitor-only mode, whatever was saved.
        cfg.mode = ExecutionMode.MONITOR
        return cfg

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)

    def export_json(self) -> str:
        """Export non-sensitive configuration (mode is not exported)."""
        data = json.loads(self.model_dump_json())
        data.pop("mode", None)
        return json.dumps(data, indent=2)

    @classmethod
    def import_json(cls, text: str) -> "AppConfig":
        data = json.loads(text)
        data.pop("mode", None)
        data.pop("reviewed", None)      # imported settings must be reviewed again by this user
        if isinstance(data.get("mt5"), dict):
            data["mt5"]["allow_real_account"] = False    # real-money trading is never imported
        return cls.model_validate(data)
