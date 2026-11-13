"""Follow-up instructions, TP/BE automation, netting accounts, recovery."""
import pytest

from tgmt5.config import TpHitAction, TpStrategy, ZonePolicy
from tgmt5.domain.models import ExecutionMode, SignalStatus
from tgmt5.execution.legs import LegState
from tgmt5.execution.recovery import recover
from tgmt5.mt5.interface import Retcode
from tgmt5.persistence.models import ExecutionRow
from tests.conftest import Harness

GOLD3 = "BUY XAUUSD NOW\nSL: 2650\nTP1: 2670\nTP2: 2680\nTP3: 2690"


def st(h, sid):
    return SignalStatus(h.signal(sid).status)


@pytest.fixture
def opened(h):
    sid = h.send(GOLD3, msg_id=10)
    assert st(h, sid) is SignalStatus.EXECUTED
    return h, sid


def test_reply_move_sl_to_be(opened):
    h, sid = opened
    h.broker.set_price("XAUUSD", 2665.0, 2665.2)
    mid = h.send("Move SL to BE", reply_to=10)
    assert st(h, mid) is SignalStatus.EXECUTED
    for p in h.broker.positions():
        assert p.sl == p.price_open
    assert all(l.be_applied for l in h.signal(sid).executions)


def test_breakeven_with_offset(opened):
    h, sid = opened
    h.cfg.execution.breakeven_offset_points = 50
    h.broker.set_price("XAUUSD", 2665.0, 2665.2)
    h.send("SL to breakeven", reply_to=10)
    assert all(p.sl == pytest.approx(p.price_open + 0.5) for p in h.broker.positions())


def test_be_rejected_when_price_below_entry(opened):
    h, sid = opened
    h.broker.set_price("XAUUSD", 2655.0, 2655.2)
    mid = h.send("Move SL to BE", reply_to=10)
    assert st(h, mid) is SignalStatus.FAILED
    assert all(p.sl == 2650 for p in h.broker.positions())


def test_context_management_without_reply_single_trade(opened):
    h, sid = opened
    h.broker.set_price("XAUUSD", 2665.0, 2665.2)
    mid = h.send("Move SL to BE")
    assert st(h, mid) is SignalStatus.EXECUTED
    assert h.signal(mid).parent_signal_id == sid


def test_ambiguous_management_two_trades_requires_review(opened):
    h, _ = opened
    h.cfg.execution.cross_source_dedup_seconds = 0
    h.send("BUY XAUUSD NOW SL 2648 TP 2675")
    n = len(h.broker.requests)
    mid = h.send("Move SL to BE")
    assert st(h, mid) is SignalStatus.MANUAL_REVIEW
    assert len(h.broker.requests) == n


def test_symbol_filters_context(opened):
    h, gold = opened
    eur = h.send("SELL EURUSD NOW SL 1.0880 TP 1.0800")
    assert st(h, eur) is SignalStatus.EXECUTED
    mid = h.send("Close 50% of GOLD position")
    assert h.signal(mid).parent_signal_id == gold


def test_close_partial(opened):
    h, sid = opened
    before = sum(p.volume for p in h.broker.positions())
    mid = h.send("Close 50% of GOLD position", reply_to=10)
    assert st(h, mid) is SignalStatus.EXECUTED
    after = sum(p.volume for p in h.broker.positions())
    assert after == pytest.approx(before - round(before * 0.5, 2), abs=0.011)
    assert after < before


def test_close_all(opened):
    h, sid = opened
    h.send("Close all trades now", reply_to=10)
    assert h.broker.positions() == []
    h.engine.on_tick()
    assert st(h, sid) is SignalStatus.CLOSED


def test_modify_sl(opened):
    h, sid = opened
    h.send("Move SL to 2655", reply_to=10)
    assert all(p.sl == 2655 for p in h.broker.positions())
    assert h.signal(sid).stop_loss == 2655


def test_modify_tp_ambiguous_with_multiple_legs(opened):
    h, sid = opened
    mid = h.send("New TP 2700", reply_to=10)
    assert st(h, mid) is SignalStatus.FAILED
    assert "ambiguous" in h.signal(mid).reject_reason


def test_cancel_pending_by_reply(h):
    sid = h.send("XAUUSD BUY LIMIT 2655\nSL 2645\nTP 2670", msg_id=20)
    assert st(h, sid) is SignalStatus.PENDING_PLACED
    mid = h.send("Cancel previous pending order.", reply_to=20)
    assert st(h, mid) is SignalStatus.EXECUTED
    assert h.broker.orders() == []
    h.engine.on_tick()
    assert st(h, sid) is SignalStatus.CANCELLED


def test_cancel_previous_pending_by_context(h):
    sid = h.send("XAUUSD BUY LIMIT 2655\nSL 2645\nTP 2670", msg_id=21)
    mid = h.send("Cancel previous pending order.")
    assert h.signal(mid).parent_signal_id == sid and h.broker.orders() == []


def test_cancel_waiting_signal(h):
    sid = h.send("GOLD SELL 2670-2675\nSL 2685\nTP 2650", msg_id=22)
    h.send("cancel", reply_to=22)
    assert st(h, sid) is SignalStatus.CANCELLED


def test_management_for_unknown_trade_rejected(h):
    mid = h.send("Move SL to BE")
    assert st(h, mid) is SignalStatus.REJECTED
    assert h.broker.requests == []


def test_management_in_monitor_mode_is_not_applied():
    h = Harness(mode=ExecutionMode.MONITOR)
    h.send(GOLD3, msg_id=10)
    mid = h.send("Close all", reply_to=10)
    assert st(h, mid) is SignalStatus.MONITORED and h.broker.requests == []


def test_tp_hit_message_ignored_by_default(opened):
    h, sid = opened
    n = len(h.broker.requests)
    mid = h.send("TP1 HIT — Secure profits", reply_to=10)
    assert st(h, mid) is SignalStatus.MONITORED and len(h.broker.requests) == n


def test_tp_hit_message_breakeven_option(opened):
    h, sid = opened
    h.cfg.execution.on_tp_hit_message = TpHitAction.BREAKEVEN
    h.broker.set_price("XAUUSD", 2665.0, 2665.2)
    h.send("TP1 HIT — Secure profits", reply_to=10)
    assert all(p.sl == p.price_open for p in h.broker.positions())


def test_tp1_hit_while_in_monitor_mode_gets_breakeven_after_switching_back(opened):
    """Restart / update with open trades: the app starts in MONITOR, TP1 is hit meanwhile."""
    from tgmt5.services.app import ReadOnlyGateway
    h, sid = opened
    h.engine.set_mode(ExecutionMode.MONITOR, ReadOnlyGateway(h.gw))
    h.broker.set_price("XAUUSD", 2671.0, 2671.2)     # TP1 leg closes at the broker
    h.engine.on_tick()
    legs = h.signal(sid).executions
    assert legs[0].state == LegState.CLOSED.value
    assert not any(l.be_applied for l in legs[1:])    # not attempted, not wrongly marked done
    assert all(p.sl == 2650 for p in h.broker.positions())
    h.engine.set_mode(ExecutionMode.DEMO, h.gw)       # user switches execution back on
    h.engine.on_tick()
    assert all(p.sl == p.price_open for p in h.broker.positions())


def test_tp1_hit_by_price_moves_others_to_be(opened):
    h, sid = opened
    h.broker.set_price("XAUUSD", 2671.0, 2671.2)     # TP1 leg closes at broker
    h.engine.on_tick()
    legs = h.signal(sid).executions
    assert legs[0].state == LegState.CLOSED.value and legs[0].profit > 0
    pos = h.broker.positions()
    assert len(pos) == 2 and all(p.sl == p.price_open for p in pos)
    # price falls back to entry -> remaining legs stopped at BE, signal CLOSED with net profit
    h.broker.set_price("XAUUSD", 2660.0, 2660.2)
    h.engine.on_tick()
    assert st(h, sid) is SignalStatus.CLOSED
    assert h.risk.state.consecutive_losses == 0


def test_trailing_stop(opened):
    h, sid = opened
    h.cfg.execution.trailing_enabled = True
    h.broker.set_price("XAUUSD", 2671.0, 2671.2)
    h.engine.on_tick()     # TP1 closes, BE applied
    h.broker.set_price("XAUUSD", 2678.0, 2678.2)
    h.engine.on_tick()
    for p in h.broker.positions():
        assert p.sl > p.price_open


def test_netting_account_single_managed_position():
    h = Harness(hedging=False)
    sid = h.send(GOLD3, msg_id=10)
    assert st(h, sid) is SignalStatus.EXECUTED
    pos = h.broker.positions()
    assert len(pos) == 1 and pos[0].tp == 2690
    vol = pos[0].volume
    h.broker.set_price("XAUUSD", 2671.0, 2671.2)
    h.engine.on_tick()
    pos = h.broker.positions()
    assert pos[0].volume < vol and pos[0].sl == pos[0].price_open


def test_first_tp_only_strategy(h):
    h.cfg.execution.tp_strategy = TpStrategy.FIRST_TP_ONLY
    sid = h.send(GOLD3)
    assert [p.tp for p in h.broker.positions()] == [2670]


def test_small_volume_uses_as_many_tps_as_possible(h):
    h.cfg.risk.risk_percent = 0.25        # $25 -> 0.02 lot: cannot split in 3, but in 2
    sid = h.send(GOLD3)
    assert st(h, sid) is SignalStatus.EXECUTED
    assert sorted((p.tp, p.volume) for p in h.broker.positions()) == [(2670, 0.01), (2680, 0.01)]


def test_pending_order_expiry(h):
    sid = h.send("XAUUSD BUY LIMIT 2655\nSL 2645\nTP 2670")
    h.clock.advance(minutes=h.cfg.execution.pending_order_expiry_minutes + 1)
    h.engine.on_tick()
    assert h.broker.orders() == []
    h.engine.on_tick()
    assert st(h, sid) is SignalStatus.CANCELLED


def test_position_closed_manually_in_terminal(opened):
    h, sid = opened
    for p in h.broker.positions():
        h.broker._close(h.broker._positions[p.ticket], p.volume, 2661.0)
    h.engine.on_tick()
    assert st(h, sid) is SignalStatus.CLOSED


# ----------------------------------------------------------------- recovery
def _restart(h):
    """Simulate an application restart against the same DB and broker."""
    return Harness(db=h.db, broker=h.broker, cfg=h.cfg)


def test_recovery_submitting_leg_found_open(h):
    h.broker.scripted.append("EXECUTE_THEN_TIMEOUT")
    # simulate crash: patch reconcile to do nothing during the first run
    h.engine._reconcile_leg = lambda s, leg: True
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert st(h, sid) is SignalStatus.UNCERTAIN
    h2 = _restart(h)
    rep = recover(h2.engine)
    assert rep.resolved_uncertain and st(h2, sid) is SignalStatus.EXECUTED
    assert len(h.broker.requests) == 1


def test_recovery_submitting_leg_absent_marked_failed_not_resent(h):
    h.broker.scripted.append(Retcode.TIMEOUT)
    h.engine._reconcile_leg = lambda s, leg: True
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    h2 = _restart(h)
    rep = recover(h2.engine)
    assert rep.failed_uncertain and st(h2, sid) is SignalStatus.FAILED
    assert len(h.broker.requests) == 1 and h.broker.positions() == []


def test_recovery_crash_before_send(h):
    from tgmt5.persistence.models import SignalRow
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    # forge a signal stuck in QUEUED with no legs (crash between QUEUED and SUBMITTING)
    with h.db.session() as s:
        row = s.get(SignalRow, sid)
        row.status = "QUEUED"
        for e in row.executions:
            s.delete(e)
    h2 = _restart(h)
    rep = recover(h2.engine)
    assert sid in rep.interrupted_signals and st(h2, sid) is SignalStatus.FAILED
    assert len(h.broker.requests) == 1     # never replayed


def test_recovery_detects_closed_while_offline(opened):
    h, sid = opened
    h.broker.set_price("XAUUSD", 2649.0, 2649.2)     # all SLs hit while app is down
    h2 = _restart(h)
    rep = recover(h2.engine)
    assert len(rep.closed_while_offline) == 3 and st(h2, sid) is SignalStatus.CLOSED


def test_recovery_reports_orphans(h):
    from tgmt5.mt5.interface import TRADE_ACTION_DEAL, TradeRequest
    h.broker.send(TradeRequest(action=TRADE_ACTION_DEAL, symbol="EURUSD", volume=0.01, type=0,
                               magic=h.cfg.execution.magic_number, comment="manual"))
    rep = recover(_restart(h).engine)
    assert len(rep.orphan_positions) == 1


def test_recovery_expires_old_waiting(h):
    sid = h.send("GOLD SELL 2670-2675\nSL 2685\nTP 2650")
    h2 = _restart(h)
    h2.clock.advance(hours=2)
    recover(h2.engine)
    assert st(h2, sid) is SignalStatus.EXPIRED


def test_restart_does_not_reprocess_old_messages(opened):
    h, sid = opened
    h2 = _restart(h)
    n = len(h.broker.requests)
    assert h2.send(GOLD3, msg_id=10) is None     # same message delivered again after restart
    assert len(h.broker.requests) == n
