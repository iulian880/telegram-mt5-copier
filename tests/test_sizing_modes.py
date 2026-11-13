"""Sizing methods, small-account settings, pre-trade review and the Risk page preview."""
import pytest

from tgmt5.config import AppConfig, SizingMode, SmallVolumePolicy, SourceConfig, TpStrategy
from tgmt5.domain.models import Direction, ExecutionMode, SignalStatus
from tgmt5.mt5.sim import fx_spec
from tgmt5.mt5.symbols import SymbolResolver
from tgmt5.review import pending_reviews, required_keys
from tgmt5.risk.preview import ExampleSignal, build_preview
from tgmt5.risk.sizing import plan_volumes, tp_layout
from tests.conftest import SOURCE, Harness, make_broker

# Lingrid-style signal: 55-pip SL, TP1 0.6R, TP2 1R, TP3 2R
EURUSD3 = "EURUSD BUY 1.08510\nSL 1.07960\nTP1 1.08840\nTP2 1.09060\nTP3 1.09610"
SPEC = fx_spec("EURUSD")


def loss55(v: float) -> float:          # 55 pips at 10 per pip per lot
    return v * 55 * 10


def plan(**risk):
    cfg = AppConfig()
    for k, v in risk.items():
        setattr(cfg.risk, k, v)
    return plan_volumes(cfg.risk, positions=3, tps_covered=1, allocation=[1, 1, 1], spec=SPEC,
                        loss_fn=loss55, equity=300, balance=300)


def small_account_cfg(**risk) -> AppConfig:
    cfg = AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="Lingrid")])
    cfg.risk.percent_caps_enabled = False
    cfg.risk.drawdown_protection = False
    cfg.risk.sizing_mode = SizingMode.LOT_PER_TP
    cfg.risk.lot_per_tp = 0.01
    for k, v in risk.items():
        setattr(cfg.risk, k, v)
    return cfg


# ----------------------------------------------------------------- planner
def test_lot_per_tp_gives_every_tp_the_same_lot():
    p = plan(sizing_mode=SizingMode.LOT_PER_TP, lot_per_tp=0.01)
    assert p.volumes == [0.01, 0.01, 0.01] and p.error == ""
    assert p.risk_money == pytest.approx(16.5) and p.budget is None


def test_lot_per_balance_scales_with_whole_steps():
    cfg = AppConfig()
    cfg.risk.sizing_mode = SizingMode.LOT_PER_BALANCE
    cfg.risk.lot_per_tp, cfg.risk.balance_step = 0.01, 300
    for balance, lot in [(200, 0.01), (299, 0.01), (650, 0.02), (1250, 0.04)]:
        p = plan_volumes(cfg.risk, positions=3, tps_covered=1, allocation=[1], spec=SPEC, loss_fn=loss55,
                         equity=balance, balance=balance)
        assert p.volumes == [lot] * 3, balance


def test_percent_mode_too_small_reduce_uses_fewer_tps():
    # 2% of 300 = 6 -> 0.01 lot only (5.5 per 0.01): one position on TP1
    p = plan(sizing_mode=SizingMode.PCT_EQUITY, max_risk_per_signal_pct=5.0, risk_percent=2.0)
    assert p.volumes == [0.01] and "TP1-TP1" in p.notes[0]


def test_percent_mode_min_per_tp_policy_opens_minimum_everywhere():
    p = plan(sizing_mode=SizingMode.PCT_EQUITY, risk_percent=1.0, small_volume_policy=SmallVolumePolicy.MIN_PER_TP)
    assert p.volumes == [0.01, 0.01, 0.01] and p.over_budget_allowed and p.within_budget


def test_percent_mode_reject_policy():
    p = plan(sizing_mode=SizingMode.PCT_EQUITY, max_risk_per_signal_pct=5.0, risk_percent=2.0,
             small_volume_policy=SmallVolumePolicy.REJECT)
    assert p.volumes == [] and "too small for 3 TPs" in p.error


def test_percent_caps_off_removes_the_cap_from_the_budget():
    on = plan(sizing_mode=SizingMode.FIXED_MONEY, fixed_money=30.0)          # cap 1% of 300 = 3
    off = plan(sizing_mode=SizingMode.FIXED_MONEY, fixed_money=30.0, percent_caps_enabled=False)
    assert on.error and off.volumes == [0.02, 0.02, 0.01]


def test_tp_layout_netting_and_first_tp():
    assert tp_layout(TpStrategy.SPLIT, 3, hedging=True)[1:] == (3, 1)
    assert tp_layout(TpStrategy.SPLIT, 3, hedging=False) == (TpStrategy.SINGLE_MANAGED, 1, 3)
    assert tp_layout(TpStrategy.FIRST_TP_ONLY, 3, hedging=True)[1:] == (1, 1)


def test_lot_per_tp_on_netting_account_is_one_position_of_three_lots():
    cfg = AppConfig()
    cfg.risk.sizing_mode = SizingMode.LOT_PER_TP
    _, positions, covered = tp_layout(TpStrategy.SPLIT, 3, hedging=False)
    p = plan_volumes(cfg.risk, positions=positions, tps_covered=covered, allocation=[1], spec=SPEC,
                     loss_fn=loss55, equity=300, balance=300)
    assert p.volumes == [0.03]


# ----------------------------------------------------------------- engine, 300 account
def test_300_account_opens_001_on_each_of_three_tps():
    h = Harness(broker=make_broker(balance=300), cfg=small_account_cfg())
    sid = h.send(EURUSD3)
    sig = h.signal(sid)
    assert SignalStatus(sig.status) is SignalStatus.EXECUTED, sig.reject_reason
    assert sorted((p.tp, p.volume) for p in h.broker.positions()) == [
        (1.0884, 0.01), (1.0906, 0.01), (1.0961, 0.01)]


def test_300_account_with_percent_caps_on_is_rejected_with_clear_reason():
    h = Harness(broker=make_broker(balance=300), cfg=small_account_cfg(percent_caps_enabled=True))
    sid = h.send(EURUSD3)
    sig = h.signal(sid)
    assert SignalStatus(sig.status) is SignalStatus.REJECTED
    assert "cap" in sig.reject_reason                     # "risk 16.50 > cap 3.00 (1% of equity)"
    assert h.broker.requests == []


def test_consecutive_signals_each_get_their_own_three_positions():
    h = Harness(broker=make_broker(balance=300), cfg=small_account_cfg())
    h.cfg.execution.cross_source_dedup_seconds = 0
    for _ in range(3):
        h.send(EURUSD3)
    assert len(h.broker.positions()) == 9


def test_drawdown_protection_off_never_locks():
    h = Harness(broker=make_broker(balance=300), cfg=small_account_cfg())
    acct = h.broker.account_info()
    h.risk.on_account_update(acct)
    h.broker.balance = h.broker.equity = 150          # -50 % in a day
    h.risk.on_account_update(h.broker.account_info())
    for _ in range(10):
        h.risk.on_signal_closed(-5)
    assert h.risk.blocking_reason() is None
    h.cfg.risk.drawdown_protection = True             # switching it back on enforces again
    h.risk.on_account_update(h.broker.account_info())
    assert h.risk.blocking_reason()


# ----------------------------------------------------------------- review
def test_review_required_fields_follow_the_settings():
    cfg = AppConfig()
    assert "tp_allocation" in required_keys(cfg) and "max_daily_loss_pct" in required_keys(cfg)
    small = small_account_cfg()
    keys = required_keys(small)
    assert "tp_allocation" not in keys                # every TP gets the same lot
    assert "max_daily_loss_pct" not in keys and "max_risk_per_signal_pct" not in keys
    assert "drawdown_protection" in keys and "percent_caps_enabled" in keys


def test_review_turns_red_again_when_value_changes():
    cfg = small_account_cfg()
    assert pending_reviews(cfg) == required_keys(cfg)
    cfg.mark_all_reviewed()
    assert pending_reviews(cfg) == []
    cfg.risk.lot_per_tp = 0.02
    assert pending_reviews(cfg) == ["size"]
    cfg.risk.sizing_mode = SizingMode.FIXED_LOT            # method change also re-opens "size"
    assert set(pending_reviews(cfg)) >= {"sizing_mode", "size", "tp_allocation"}


def test_imported_config_must_be_reviewed_again():
    cfg = small_account_cfg()
    cfg.mark_all_reviewed()
    assert AppConfig.import_json(cfg.export_json()).reviewed == {}


async def test_demo_mode_blocked_until_reviewed(tmp_path):
    from tgmt5.config import MT5Settings
    from tgmt5.persistence.db import Database
    from tgmt5.secrets import MemoryKeyring, SecretStore
    from tgmt5.services.app import AppService
    from tgmt5.telegram.client import FakeTelegramSource
    broker = make_broker()
    svc = AppService(tmp_path, secrets=SecretStore(MemoryKeyring()), telegram=FakeTelegramSource(),
                     gateway_factory=lambda: broker, db=Database.at_path(tmp_path / "t.db"))
    svc.config = small_account_cfg()
    svc.config.mt5 = MT5Settings(login=broker.login, server=broker.server)
    await svc.start()
    try:
        await svc.connect_mt5("pw")
        await svc.confirm_automated_execution()
        with pytest.raises(ValueError, match="Review the red risk settings"):
            await svc.set_mode(ExecutionMode.DEMO)
        svc.config.mark_all_reviewed()
        await svc.set_mode(ExecutionMode.DEMO)
        # the preview uses the detected account and the broker's own numbers
        pv = await svc.sizing_preview(svc.config, fallback_balance=300, sl_pips=55, n_tps=3, recent=False)
        assert pv.detected and pv.equity == broker.account_info().equity
        eur = next(r for r in pv.rows if r.symbol == "EURUSD")
        assert eur.ok and [v for _, v in eur.positions] == [0.01, 0.01, 0.01]
        assert eur.risk_money == pytest.approx(16.5, abs=0.01) and eur.margin
        assert next(r for r in pv.rows if r.symbol == "GBPNZD").reason == "not available at this broker"
    finally:
        await svc.stop()


# ----------------------------------------------------------------- preview offline
def test_offline_preview_uses_example_balance():
    cfg = small_account_cfg(percent_caps_enabled=True)
    pv = build_preview(None, None, cfg, [ExampleSignal("EURUSD", Direction.BUY, 3, sl_pips=55)],
                       fallback_balance=300)
    row = pv.rows[0]
    assert not pv.detected and row.total == 0.03 and row.risk_money == pytest.approx(16.5)
    assert not row.ok and "max risk per signal" in row.reason      # caps on: 16.5 > 3.00
    assert any("Not connected" in s for s in pv.summary)


def test_detected_preview_matches_engine_sizing():
    b = make_broker(balance=300)
    cfg = small_account_cfg()
    pv = build_preview(b, SymbolResolver(b), cfg,
                       [ExampleSignal("EURUSD", Direction.BUY, 3, entry=1.08510, stop_loss=1.07960)],
                       fallback_balance=1)
    h = Harness(broker=make_broker(balance=300), cfg=small_account_cfg())
    sid = h.send(EURUSD3)
    assert pv.rows[0].total == h.signal(sid).lot_total == 0.03
    assert pv.rows[0].risk_money == pytest.approx(h.signal(sid).risk_money, abs=0.02)
