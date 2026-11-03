"""End-to-end pipeline tests against the deterministic fake broker."""
import pytest

from tgmt5.config import AppConfig, SourceConfig, ZonePolicy
from tgmt5.domain.models import ExecutionMode, MessageEventType, SignalStatus
from tgmt5.execution.legs import LegState
from tgmt5.mt5.interface import ACCOUNT_TRADE_MODE_REAL, GatewayError, Retcode
from tgmt5.mt5.sim import with_spec
from tgmt5.risk.engine import RiskEngine
from tgmt5.risk.sizing import loss_at_stop
from tgmt5.domain.models import Direction
from tests.conftest import SOURCE, Harness

GOLD3 = "BUY XAUUSD NOW\nSL: 2650\nTP1: 2670\nTP2: 2680\nTP3: 2690"


def status(h, sid):
    return SignalStatus(h.signal(sid).status)


def test_market_signal_multi_tp_executes_with_total_risk_capped(h):
    sid = h.send(GOLD3)
    sig = h.signal(sid)
    assert status(h, sid) is SignalStatus.EXECUTED, sig.reject_reason
    legs = sig.executions
    assert [l.tp for l in legs] == [2670, 2680, 2690]
    assert all(l.state == LegState.OPEN.value for l in legs)
    total = sum(l.volume for l in legs)
    loss = loss_at_stop(h.broker, h.broker.symbol_spec("XAUUSD"), Direction.BUY, total, 2660.20, 2650)[0]
    assert loss <= 10_000 * 0.005 + 0.01           # 0.5% of equity for the WHOLE signal
    pos = h.broker.positions()
    assert len(pos) == 3 and {p.comment for p in pos} == {l.client_tag for l in legs}
    assert all(p.sl == 2650 for p in pos)
    # audit trail
    assert [e.status_to for e in sig.events][:5] == ["RECEIVED", "PARSED", "VALIDATED", "QUEUED",
                                                     "SUBMITTING"]


def test_duplicate_event_does_not_duplicate_order(h):
    h.send(GOLD3, msg_id=7)
    n = len(h.broker.requests)
    assert h.send(GOLD3, msg_id=7) is None
    assert len(h.broker.requests) == n


def test_monitor_mode_never_sends():
    h = Harness(mode=ExecutionMode.MONITOR)
    sid = h.send(GOLD3)
    assert status(h, sid) is SignalStatus.MONITORED
    assert h.signal(sid).lot_total > 0          # shows what WOULD have happened
    assert h.broker.requests == []


def test_stale_signal_expires(h):
    sid = h.send(GOLD3, age_s=600)
    assert status(h, sid) is SignalStatus.EXPIRED
    assert h.broker.requests == []


def test_unauthorized_source_ignored(h):
    assert h.send(GOLD3, source=999) is None
    assert h.broker.requests == []


def test_disabled_source_ignored(h):
    h.cfg.sources[0].enabled = False
    assert h.send(GOLD3) is None


def test_ambiguous_goes_to_manual_review(h):
    sid = h.send("BUY GOLD NOW TP 2670")
    assert status(h, sid) is SignalStatus.MANUAL_REVIEW
    assert h.broker.requests == []


def test_missing_tp_requires_review_by_default(h):
    sid = h.send("BUY XAUUSD NOW SL 2650")
    assert status(h, sid) is SignalStatus.MANUAL_REVIEW


def test_price_moved_too_far_market_rejected(h):
    # entry 2650 "now", market at 2660.2, SL 2640 -> SL dist 10, tolerance 3
    sid = h.send("BUY XAUUSD NOW 2650 SL 2640 TP 2680")
    assert status(h, sid) is SignalStatus.REJECTED
    assert "moved" in h.signal(sid).reject_reason


def test_wait_for_zone_then_execute(h):
    sid = h.send("GOLD SELL 2670-2675\nSL 2685\nTP 2650")
    s = h.signal(sid)
    assert status(h, sid) is SignalStatus.WAITING, s.reject_reason
    h.engine.on_tick()
    assert status(h, sid) is SignalStatus.WAITING
    h.broker.set_price("XAUUSD", 2671.0, 2671.2)
    h.engine.on_tick()
    assert status(h, sid) is SignalStatus.EXECUTED
    assert h.broker.positions()[0].type == 1


def test_waiting_signal_expires(h):
    sid = h.send("GOLD SELL 2670-2675\nSL 2685\nTP 2650")
    h.clock.advance(minutes=61)
    h.engine.on_tick()
    assert status(h, sid) is SignalStatus.EXPIRED
    assert h.broker.requests == []


def test_zone_reject_policy(h):
    h.cfg.execution.zone_policy = ZonePolicy.REJECT
    sid = h.send("GOLD SELL 2670-2675\nSL 2685\nTP 2650")
    assert status(h, sid) is SignalStatus.REJECTED


def test_zone_pending_policy_places_limit_and_fills(h):
    h.cfg.execution.zone_policy = ZonePolicy.PENDING
    sid = h.send("GOLD SELL 2670-2675\nSL 2685\nTP 2650")
    assert status(h, sid) is SignalStatus.PENDING_PLACED
    o = h.broker.orders()
    assert len(o) == 1 and o[0].price_open == 2670 and o[0].type == 3   # SELL_LIMIT
    h.broker.set_price("XAUUSD", 2670.5, 2670.7)
    h.engine.on_tick()
    leg = h.signal(sid).executions[0]
    assert leg.state == LegState.OPEN.value and leg.position_ticket


def test_explicit_buy_limit(h):
    sid = h.send("XAUUSD BUY LIMIT 2655\nSL 2645\nTP 2670")
    assert status(h, sid) is SignalStatus.PENDING_PLACED
    assert h.broker.orders()[0].type == 2


def test_spread_filter(h):
    h.broker.set_price("XAUUSD", 2660.0, 2663.0)    # 300 pt spread vs 10 SL -> > 15%
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert status(h, sid) is SignalStatus.REJECTED and "spread" in h.signal(sid).reject_reason


def test_tp_already_reached_rejected(h):
    sid = h.send("BUY XAUUSD NOW SL 2640 TP 2655")
    assert status(h, sid) is SignalStatus.REJECTED


def test_broker_rejection_fails_cleanly(h):
    h.broker.scripted.extend([Retcode.NO_MONEY] * 3)
    sid = h.send(GOLD3)
    assert status(h, sid) is SignalStatus.FAILED
    assert h.broker.positions() == []


def test_partial_leg_failure(h):
    h.broker.scripted.extend([Retcode.MARKET_CLOSED])
    sid = h.send(GOLD3)
    assert status(h, sid) is SignalStatus.PARTIAL
    assert len(h.broker.positions()) == 2


def test_timeout_but_executed_is_reconciled_without_duplicate(h):
    h.broker.scripted.append("EXECUTE_THEN_TIMEOUT")
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    leg = h.signal(sid).executions[0]
    assert status(h, sid) is SignalStatus.EXECUTED
    assert leg.state == LegState.OPEN.value
    assert len(h.broker.requests) == 1 and len(h.broker.positions()) == 1


def test_timeout_not_executed_retries_once_after_reconcile(h):
    h.broker.scripted.append(Retcode.TIMEOUT)
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert status(h, sid) is SignalStatus.EXECUTED
    assert len(h.broker.requests) == 2 and len(h.broker.positions()) == 1


def test_transport_exception_twice_fails_without_order(h):
    h.broker.scripted.extend([ConnectionError("pipe"), ConnectionError("pipe")])
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert status(h, sid) is SignalStatus.FAILED
    assert h.broker.positions() == []


def test_uncertain_and_broker_unreachable_stays_uncertain_no_retry(h, monkeypatch):
    h.broker.scripted.append(Retcode.CONNECTION)
    real = h.broker.positions

    def flaky(symbol=None):
        if h.broker.requests:
            raise GatewayError("down")
        return real(symbol)
    monkeypatch.setattr(h.broker, "positions", flaky)
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert status(h, sid) is SignalStatus.UNCERTAIN
    assert len(h.broker.requests) == 1


def test_invalid_fill_falls_back_to_next_mode(h):
    h.broker.scripted.append(Retcode.INVALID_FILL)
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert status(h, sid) is SignalStatus.EXECUTED
    assert [r.type_filling for r in h.broker.requests] == [0, 1]


def test_live_account_blocked_and_emergency_engaged(h):
    h.broker.trade_mode = ACCOUNT_TRADE_MODE_REAL
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert status(h, sid) is SignalStatus.FAILED
    assert h.broker.positions() == [] and h.broker.requests == []
    assert h.risk.state.emergency_stop


def test_first_execution_requires_confirmation(h):
    h.risk.state.confirmed_account = ""
    sid = h.send(GOLD3)
    assert status(h, sid) is SignalStatus.REJECTED
    assert "not confirmed" in h.signal(sid).reject_reason


def test_emergency_stop_blocks(h):
    h.risk.emergency("user pressed STOP")
    sid = h.send(GOLD3)
    assert status(h, sid) is SignalStatus.REJECTED
    assert "Emergency" in h.signal(sid).reject_reason


def test_max_concurrent_trades(h):
    h.cfg.risk.max_concurrent_trades = 1
    a = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    b = h.send("SELL EURUSD NOW SL 1.0880 TP 1.0800")
    assert status(h, a) is SignalStatus.EXECUTED
    assert status(h, b) is SignalStatus.REJECTED and "concurrent" in h.signal(b).reject_reason


def test_total_open_risk_limit(h):
    h.cfg.risk.max_total_open_risk_pct = 0.8
    a = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    b = h.send("SELL EURUSD NOW SL 1.0880 TP 1.0800")
    assert status(h, a) is SignalStatus.EXECUTED
    assert status(h, b) is SignalStatus.REJECTED and "open risk" in h.signal(b).reject_reason


def test_margin_limit(h):
    h.cfg.risk.max_margin_usage_pct = 0.1
    sid = h.send(GOLD3)
    assert status(h, sid) is SignalStatus.REJECTED and "margin" in h.signal(sid).reject_reason


def test_blacklist(h):
    h.cfg.risk.symbol_blacklist = ["XAUUSD"]
    assert status(h, h.send(GOLD3)) is SignalStatus.REJECTED


def test_cross_source_dedup():
    cfg = AppConfig(sources=[SourceConfig(chat_id=SOURCE), SourceConfig(chat_id=2)])
    h = Harness(cfg=cfg)
    a = h.send(GOLD3)
    b = h.send(GOLD3, source=2)
    assert status(h, a) is SignalStatus.EXECUTED
    assert status(h, b) is SignalStatus.REJECTED and "duplicate" in h.signal(b).reject_reason


def test_symbol_missing_at_broker(h):
    sid = h.send("BUY NAS100 NOW SL 17800 TP 18100")
    assert status(h, sid) is SignalStatus.MANUAL_REVIEW


def test_broker_suffix_resolution():
    from tgmt5.mt5.sim import gold_spec
    h = Harness()
    del h.broker.specs["XAUUSD"]
    h.broker.add_symbol(gold_spec("XAUUSD.a"), 2660.00, 2660.20)
    h.engine.resolver.refresh()
    sid = h.send("BUY GOLD NOW SL 2650 TP 2690")
    assert status(h, sid) is SignalStatus.EXECUTED
    assert h.broker.positions()[0].symbol == "XAUUSD.a"


def test_min_volume_above_risk_rejected(h):
    h.cfg.risk.risk_percent = 0.01      # $1 budget, min lot loss $10
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert status(h, sid) is SignalStatus.REJECTED and "minimum volume" in h.signal(sid).reject_reason


# ---------------------------------------------------------------- edits / deletes
def test_edit_adds_sl_then_executes(h):
    sid = h.send("BUY XAUUSD NOW TP 2690", msg_id=50)
    assert status(h, sid) is SignalStatus.MANUAL_REVIEW
    sid2 = h.send("BUY XAUUSD NOW SL 2650 TP 2690", msg_id=50, event=MessageEventType.EDIT)
    assert sid2 == sid and status(h, sid) is SignalStatus.EXECUTED
    assert len(h.broker.positions()) == 1


def test_edit_after_execution_never_reexecutes(h):
    sid = h.send(GOLD3, msg_id=51)
    n = len(h.broker.requests)
    h.send(GOLD3 + "\n(updated)", msg_id=51, event=MessageEventType.EDIT)
    h.send("SELL XAUUSD NOW SL 2670 TP 2640", msg_id=51, event=MessageEventType.EDIT)
    assert len(h.broker.requests) == n
    assert status(h, sid) is SignalStatus.EXECUTED


def test_edit_same_text_ignored(h):
    h.send(GOLD3, msg_id=52)
    assert h.send(GOLD3, msg_id=52, event=MessageEventType.EDIT) is None


def test_delete_cancels_waiting(h):
    sid = h.send("GOLD SELL 2670-2675\nSL 2685\nTP 2650", msg_id=60)
    h.send("", msg_id=60, event=MessageEventType.DELETE)
    assert status(h, sid) is SignalStatus.CANCELLED
    h.broker.set_price("XAUUSD", 2671.0, 2671.2)
    h.engine.on_tick()
    assert h.broker.positions() == []


def test_delete_after_execution_keeps_trade(h):
    sid = h.send(GOLD3, msg_id=61)
    h.send("", msg_id=61, event=MessageEventType.DELETE)
    assert status(h, sid) is SignalStatus.EXECUTED and len(h.broker.positions()) == 3


# ---------------------------------------------------------------- lockouts
def test_daily_loss_lockout_persists_across_restart(h):
    h.cfg.risk.max_daily_loss_pct = 0.5
    h.cfg.risk.risk_percent = 1.0
    assert status(h, h.send("BUY XAUUSD NOW SL 2640 TP 2690")) is SignalStatus.EXECUTED
    h.broker.set_price("XAUUSD", 2641.0, 2641.2)    # floating loss ~0.77% of equity
    h.engine.refresh_account()
    assert h.risk.blocking_reason() and "Daily" in h.risk.blocking_reason()
    # restart: new risk engine from the same DB
    r2 = RiskEngine(lambda: h.cfg.risk, lambda: h.db.get_setting("risk_state"),
                    lambda v: h.db.set_setting("risk_state", v), clock=h.clock)
    assert "Daily" in r2.blocking_reason()
    # next day -> lockout cleared
    h.clock.advance(days=1)
    assert r2.blocking_reason() is None


def test_drawdown_lockout_requires_manual_reset(h):
    h.cfg.risk.max_daily_loss_pct = 50
    h.cfg.risk.max_drawdown_pct = 0.5
    h.cfg.risk.risk_percent = 1.0
    assert status(h, h.send("BUY XAUUSD NOW SL 2640 TP 2690")) is SignalStatus.EXECUTED
    h.broker.set_price("XAUUSD", 2641.0, 2641.2)
    h.engine.refresh_account()
    assert "Drawdown" in h.risk.blocking_reason()
    h.clock.advance(days=1)
    assert "Drawdown" in h.risk.blocking_reason()
    h.risk.reset_drawdown_lockout(h.broker.account_info().equity)
    assert h.risk.blocking_reason() is None


def test_emergency_persists_across_restart(h):
    h.risk.emergency("test")
    r2 = RiskEngine(lambda: h.cfg.risk, lambda: h.db.get_setting("risk_state"),
                    lambda v: h.db.set_setting("risk_state", v))
    assert r2.state.emergency_stop


def test_consecutive_loss_breaker(h):
    h.cfg.risk.max_consecutive_losses = 2
    for _ in range(2):
        sid = h.send("BUY XAUUSD NOW SL 2655 TP 2690")
        assert status(h, sid) is SignalStatus.EXECUTED
        h.broker.set_price("XAUUSD", 2654.0, 2654.2)
        h.engine.on_tick()
        assert status(h, sid) is SignalStatus.CLOSED
        h.broker.set_price("XAUUSD", 2660.0, 2660.2)
    assert "circuit breaker" in h.risk.blocking_reason()


def test_trade_frequency_limit(h):
    h.cfg.risk.max_trades_per_hour = 1
    h.cfg.execution.cross_source_dedup_seconds = 0
    a = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    b = h.send("SELL EURUSD NOW SL 1.0880 TP 1.0800")
    assert status(h, a) is SignalStatus.EXECUTED
    assert "frequency" in h.signal(b).reject_reason


def test_mt5_unstable_pauses(h):
    h.risk.mt5_unstable = True
    assert status(h, h.send(GOLD3)) is SignalStatus.REJECTED


def test_disconnected_mt5_rejects(h):
    h.broker.connected = False
    assert status(h, h.send(GOLD3)) is SignalStatus.REJECTED
