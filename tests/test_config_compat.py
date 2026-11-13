"""Settings saved by an older version must keep loading after an update (synthetic v0.1.0 data)."""
import json

from tgmt5.config import AppConfig, SizingMode
from tgmt5.review import pending_reviews, required_keys

# AppConfig as saved by v0.1.0 (generated with the v0.1.0 config module; synthetic values)
V010 = json.loads('{"mode":"MONITOR","theme":"dark","risk":{"sizing_mode":"FIXED_LOT","risk_percent":0.5,"fixed_lot":0.03,"fixed_money":50.0,"max_risk_per_signal_pct":1.0,"max_total_open_risk_pct":3.0,"max_daily_loss_pct":3.0,"max_drawdown_pct":10.0,"max_consecutive_losses":4,"max_concurrent_trades":5,"max_trades_per_hour":6,"max_lots_per_symbol":5.0,"max_lot_per_order":2.0,"max_margin_usage_pct":30.0,"min_free_margin":100.0,"max_spread_sl_fraction":0.15,"max_entry_deviation_sl_fraction":0.3,"max_slippage_points":30,"min_risk_reward":0.0,"correlation_groups":{"USD_LONG":["USDJPY","USDCHF","USDCAD"],"USD_SHORT":["EURUSD","GBPUSD","AUDUSD","NZDUSD","XAUUSD"]},"max_correlated_risk_pct":2.0,"symbol_whitelist":[],"symbol_blacklist":[],"symbol_profiles":{},"trading_hours_utc":null},"execution":{"max_signal_age_seconds":120,"zone_policy":"WAIT","waiting_signal_expiry_minutes":60,"pending_order_expiry_minutes":240,"require_take_profit":true,"tp_strategy":"SPLIT","tp_allocation":[0.4,0.3,0.3],"move_sl_to_be_after_tp":1,"breakeven_offset_points":0,"trailing_enabled":false,"trailing_distance_sl_fraction":0.5,"on_tp_hit_message":"IGNORE","apply_edits_to_open_trades":false,"cross_source_dedup_seconds":300,"magic_number":26110301,"reconcile_retry_once":true},"sources":[{"chat_id":-1001234567890,"title":"Signals","enabled":true,"risk_multiplier":1.0,"symbol_aliases":{},"template":"default"}],"mt5":{"terminal_path":"","login":null,"server":"","symbol_suffix":"","expected_currency":""},"paper":{"starting_balance":10000.0,"extra_spread_points":0.0,"slippage_points":0.0,"commission_per_lot":7.0},"notifications":{"desktop":true,"telegram_destination":"","on_executed":true,"on_rejected":true,"on_closed":true}}')


def test_v010_settings_load_unchanged():
    cfg = AppConfig.model_validate(V010)
    assert cfg.risk.sizing_mode is SizingMode.FIXED_LOT and cfg.risk.fixed_lot == 0.03
    assert cfg.execution.tp_allocation == [0.4, 0.3, 0.3]
    assert cfg.sources[0].title == "Signals"
    # new settings get safe defaults: protections stay ON after an update
    assert cfg.risk.percent_caps_enabled and cfg.risk.drawdown_protection


def test_v010_settings_need_one_review_after_update():
    cfg = AppConfig.model_validate(V010)
    assert pending_reviews(cfg) == required_keys(cfg)      # red once, until ticked
