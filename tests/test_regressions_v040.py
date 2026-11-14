"""Regression tests for the bugs fixed in v0.4.0 (found in the November 2026 review)."""
from __future__ import annotations

from tgmt5.config import AppConfig, SourceConfig
from tgmt5.domain.models import MessageEventType, SignalStatus

from tests.conftest import SOURCE, Harness

AMBIGUOUS_GOLD = "BUY XAUUSD SL 2650 TP 2670"       # no entry condition -> manual review
GOLD3 = "BUY XAUUSD NOW\nSL: 2650\nTP1: 2670\nTP2: 2680\nTP3: 2690"


# ---- chat text must not close trades ------------------------------------------------------
def test_chat_message_does_not_close_open_trade():
    h = Harness()
    sid = h.send(GOLD3, msg_id=10)
    assert h.signal(sid).status == SignalStatus.EXECUTED.value
    n = len(h.broker.positions())
    h.send("Gold is getting close to TP1 2670 🔥")
    h.send("Keep your SL, be patient guys")
    h.send("Take only 1% risk on this one")
    assert len(h.broker.positions()) == n
    assert all(p.volume > 0 for p in h.broker.positions())


# ---- delete events without a chat id -------------------------------------------------------
def test_delete_without_chat_id_ignores_channel_sources():
    h = Harness()
    sid = h.send(AMBIGUOUS_GOLD, msg_id=500)
    assert h.signal(sid).status == SignalStatus.MANUAL_REVIEW.value
    # Telegram sends no chat id for deletions in private chats / basic groups; message ids there
    # are a separate sequence from channel ids, so this must not touch the channel's signal
    h.send("", msg_id=500, source=0, event=MessageEventType.DELETE)
    assert h.signal(sid).status == SignalStatus.MANUAL_REVIEW.value


def test_delete_without_chat_id_still_matches_basic_group_source():
    group = -4512345                                      # basic group: shares the account id space
    cfg = AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="VIP"),
                             SourceConfig(chat_id=group, title="Small group")])
    h = Harness(cfg=cfg)
    sid = h.send(AMBIGUOUS_GOLD, msg_id=777, source=group)
    assert h.signal(sid).status == SignalStatus.MANUAL_REVIEW.value
    h.send("", msg_id=777, source=0, event=MessageEventType.DELETE)
    assert h.signal(sid).status == SignalStatus.CANCELLED.value


# ---- PAPER mode after disconnect + reconnect --------------------------------------------
async def test_paper_mode_works_after_reconnect(tmp_path):
    from tests.conftest import make_broker
    from tgmt5.config import MT5Settings
    from tgmt5.domain.models import ExecutionMode
    from tgmt5.persistence.db import Database
    from tgmt5.persistence.models import SignalRow
    from tgmt5.secrets import MemoryKeyring, SecretStore
    from tgmt5.services.app import AppService
    from tgmt5.telegram.client import FakeTelegramSource

    tg = FakeTelegramSource()
    svc = AppService(tmp_path, secrets=SecretStore(MemoryKeyring()), telegram=tg,
                     gateway_factory=make_broker,          # a new adapter per connect, like the real one
                     db=Database.at_path(tmp_path / "t.db"))
    svc.config = AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="VIP")],
                           mt5=MT5Settings(login=50001234, server="ICMarketsSC-Demo", auto_connect=False))
    svc.config.mark_all_reviewed()
    svc.config.execution.cross_source_dedup_seconds = 0
    await svc.start()
    try:
        await svc.connect_mt5("pw")
        await svc.set_mode(ExecutionMode.PAPER)
        await svc.disconnect_mt5()
        await svc.connect_mt5("pw")
        await svc.set_mode(ExecutionMode.PAPER)
        await tg.push(SOURCE, "BUY XAUUSD NOW SL 2648 TP 2690")
        await svc.queue.join()
        with svc.db.session() as s:
            sig = s.query(SignalRow).order_by(SignalRow.id.desc()).first()
            assert sig.status == SignalStatus.SIMULATED.value, (sig.status, sig.reject_reason)
    finally:
        await svc.stop()


# ---- large accounts: legs are capped at "max lot per position", not rejected ------------
def test_large_account_caps_each_leg_instead_of_rejecting():
    from tests.conftest import make_broker
    h = Harness(broker=make_broker(balance=1_000_000))
    h.engine.refresh_account()
    sid = h.send(GOLD3)
    sig = h.signal(sid)
    assert sig.status == SignalStatus.EXECUTED.value, sig.reject_reason
    cap = h.cfg.risk.max_lot_per_order
    vols = [l.volume for l in sig.executions]
    assert vols and all(v <= cap + 1e-9 for v in vols), vols
    assert vols[0] == cap                               # the biggest (front-loaded) leg hits the cap


# ---- a pending order acknowledged with DONE (10009) is still a pending order -------------
def test_pending_order_with_done_retcode_stays_pending():
    from tests.conftest import make_broker
    from tgmt5.mt5.interface import Retcode, TradeResult

    b = make_broker()
    real_pending = b._pending

    def done_instead_of_placed(r):            # most real servers answer DONE, not PLACED
        res = real_pending(r)
        if res.retcode == Retcode.PLACED:
            return TradeResult(retcode=Retcode.DONE, order=res.order, volume=res.volume, price=0.0)
        return res
    b._pending = done_instead_of_placed
    h = Harness(broker=b)
    sid = h.send("BUY LIMIT XAUUSD 2655 SL 2645 TP 2675")
    assert h.signal(sid).status == SignalStatus.PENDING_PLACED.value
    h.engine.on_tick()
    sig = h.signal(sid)
    assert sig.status == SignalStatus.PENDING_PLACED.value, sig.status
    assert [l.state for l in sig.executions] == ["PENDING"]
    assert len(b.orders()) == 1


# ---- an error right after a successful send must not leave the signal SUBMITTING ---------
def test_error_after_send_does_not_strand_the_signal():
    from tgmt5.mt5.interface import GatewayError

    h = Harness()
    orig = h.broker.positions
    armed = {"on": True}

    def flaky(symbol=None):
        if armed["on"] and h.broker.requests:          # fails right after the order was sent
            armed["on"] = False
            raise GatewayError("positions_get failed: [-10004] No IPC connection")
        return orig(symbol)
    h.broker.positions = flaky
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    sig = h.signal(sid)
    assert sig.status == SignalStatus.EXECUTED.value, (sig.status, [l.state for l in sig.executions])
    assert [l.state for l in sig.executions] == ["OPEN"]
    assert h.engine.active_trade_count() == 1
    h.engine.on_tick()
    assert h.signal(sid).executions[0].position_ticket == h.broker.positions()[0].ticket


# ---- one failing signal must not undo another signal's broker actions in the same tick -----
def test_management_failure_does_not_repeat_partial_close():
    from tgmt5.config import TpStrategy
    from tgmt5.mt5.interface import GatewayError

    h = Harness()
    h.cfg.execution.tp_strategy = TpStrategy.SINGLE_MANAGED
    h.cfg.execution.cross_source_dedup_seconds = 0
    g = h.send("BUY XAUUSD NOW\nSL: 2650\nTP1: 2665\nTP2: 2680\nTP3: 2690")
    e = h.send("BUY EURUSD NOW SL 1.0830 TP 1.0900")
    assert h.signal(g).status == h.signal(e).status == SignalStatus.EXECUTED.value
    vol0 = next(p.volume for p in h.broker.positions() if p.symbol == "XAUUSD")
    h.broker.set_price("EURUSD", 1.0820)        # EURUSD stopped out -> its deals are looked up
    h.broker.set_price("XAUUSD", 2666.0)        # gold beyond managed TP1 -> partial close
    orig = h.broker.history_deals

    def broken(a, b):
        raise GatewayError("history_deals_get failed")
    h.broker.history_deals = broken
    h.engine.on_tick()                          # must not raise
    h.broker.history_deals = orig
    h.engine.on_tick()
    h.engine.on_tick()
    vol = next(p.volume for p in h.broker.positions() if p.symbol == "XAUUSD")
    from tgmt5.mt5.interface import TRADE_ACTION_DEAL
    partials = sum(1 for r in h.broker.requests
                   if r.position and r.symbol == "XAUUSD" and r.action == TRADE_ACTION_DEAL)
    assert partials == 1, partials
    assert 0 < vol < vol0


# ---- MT5 trade-server time is converted to UTC in the adapter -----------------------------
def test_real_gateway_converts_server_time_to_utc():
    import time
    from types import SimpleNamespace as NS

    from tgmt5.mt5.real import RealMT5Gateway

    OFF = 2 * 3600                                     # IC Markets winter time: UTC+2
    calls = {}

    class FakeMT5:
        def initialize(self, **kw):
            return True

        def symbols_get(self):
            now = time.time()
            return [NS(name="EURUSD", visible=True, time=int(now + OFF - 3)),
                    NS(name="EXOTIC", visible=True, time=int(now + OFF - 5400))]   # stale quote

        def account_info(self):
            return NS(login=1, server="S", company="C", currency="USD", balance=1, equity=1, margin=0,
                      margin_free=1, leverage=100, trade_mode=0, margin_mode=2, trade_allowed=True, name="n")

        def symbol_info_tick(self, symbol):
            return NS(bid=1.0, ask=1.1, time=int(time.time() + OFF - (1800 if symbol == "STALE" else 1)))

        def positions_get(self, **kw):
            return [NS(ticket=1, symbol="EURUSD", type=0, volume=0.1, price_open=1, price_current=1, sl=0,
                       tp=0, profit=0, magic=1, comment="", time=int(time.time() + OFF - 600),
                       identifier=1, swap=0)]

        def history_deals_get(self, a, b):
            calls["range"] = (a.timestamp(), b.timestamp())
            return []

    gw = RealMT5Gateway(module=FakeMT5())
    gw.connect(__import__("tgmt5.mt5.interface", fromlist=["ConnectParams"]).ConnectParams())
    assert gw.server_offset_seconds == OFF
    assert abs(gw.tick("EURUSD").time - time.time()) < 5             # fresh quote, in UTC
    stale = gw.tick("STALE")
    assert gw.server_offset_seconds == OFF                            # a stale quote never moves it
    assert time.time() - stale.time > 1700                            # ...and is seen as 30 min old
    assert abs((time.time() - gw.positions()[0].time) - 600) < 5      # position opened 10 min ago
    t0 = time.time()
    gw.history_deals(t0 - 100, t0)
    assert abs(calls["range"][1] - (t0 + OFF)) < 1                    # range asked in server time


# ---- a reply in PAPER mode must not touch a DEMO/real signal (and vice versa) ------------
def test_reply_in_paper_mode_does_not_touch_real_signal():
    from tgmt5.domain.models import ExecutionMode
    from tgmt5.mt5.sim import PaperGateway

    h = Harness()
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690", msg_id=10)
    assert h.signal(sid).status == SignalStatus.EXECUTED.value
    pg = PaperGateway(h.broker, balance=10000, extra_spread_points=0, slippage_points=0,
                      commission_per_lot=0)
    h.engine.set_mode(ExecutionMode.PAPER, pg)
    mid = h.send("Close it now", reply_to=10)
    h.engine.on_tick()
    sig = h.signal(sid)
    assert [l.state for l in sig.executions] == ["OPEN"], sig.status
    assert sig.status == SignalStatus.EXECUTED.value
    assert len(h.broker.positions()) == 1
    assert h.signal(mid).status == SignalStatus.MANUAL_REVIEW.value


# ---- netting accounts: a second signal on a symbol the app holds is refused ----------------
def test_netting_account_refuses_second_signal_on_same_symbol():
    h = Harness(hedging=False)
    h.cfg.execution.cross_source_dedup_seconds = 0
    a = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    b = h.send("BUY XAUUSD NOW SL 2648 TP 2685")
    assert h.signal(a).status == SignalStatus.EXECUTED.value
    sb = h.signal(b)
    assert sb.status == SignalStatus.REJECTED.value and "netting" in sb.reject_reason, sb.reject_reason
    h.engine.on_tick()
    assert h.signal(a).status == SignalStatus.EXECUTED.value
    pos = h.broker.positions()
    assert len(pos) == 1 and pos[0].sl == 2650
    c = h.send("BUY EURUSD NOW SL 1.0830 TP 1.0900")            # another symbol is fine
    assert h.signal(c).status == SignalStatus.EXECUTED.value


# ---- risk baselines belong to one account -------------------------------------------------
def test_switching_account_rebaselines_equity_lockouts():
    from tgmt5.config import RiskSettings
    from tgmt5.mt5.interface import AccountInfo
    from tgmt5.risk.engine import RiskEngine

    store = {}
    r = RiskEngine(lambda: RiskSettings(), lambda: store.get("x"), lambda v: store.__setitem__("x", v))
    big = AccountInfo(1, "Broker-Demo", "", "USD", 100_000, 100_000, 0, 100_000, 500, 0, 2)
    small = AccountInfo(2, "Broker-Demo", "", "USD", 5_000, 5_000, 0, 5_000, 500, 0, 2)
    r.on_account_update(big)
    r.on_account_update(small)             # e.g. an expired demo replaced by a new, smaller one
    assert r.blocking_reason() is None, r.blocking_reason()
    small_loss = AccountInfo(2, "Broker-Demo", "", "USD", 5_000, 4_000, 0, 4_000, 500, 0, 2)
    r.on_account_update(small_loss)        # a real 20% loss on the new account still locks out
    assert r.blocking_reason() is not None


# ---- a timed-out order that shows up late must not be sent twice ---------------------------
def test_late_order_after_timeout_is_not_duplicated(monkeypatch):
    h = Harness()
    h.broker.scripted.append("EXECUTE_THEN_TIMEOUT")         # executed at the server, answer lost
    hide = {"left": 1}                                        # ...and not visible on the first look
    real_pos, real_ord, real_deals = h.broker.positions, h.broker.orders, h.broker.history_deals

    def lagging(fn, empty):
        def wrapper(*a, **kw):
            if h.broker.requests and hide["left"] > 0:
                return empty
            return fn(*a, **kw)
        return wrapper
    monkeypatch.setattr(h.broker, "positions", lagging(real_pos, []))
    monkeypatch.setattr(h.broker, "orders", lagging(real_ord, []))

    def deals(*a, **kw):
        if h.broker.requests and hide["left"] > 0:
            hide["left"] -= 1                                  # first full look is over
            return []
        return real_deals(*a, **kw)
    monkeypatch.setattr(h.broker, "history_deals", deals)
    sid = h.send("BUY XAUUSD NOW SL 2650 TP 2690")
    assert len(h.broker.requests) == 1, "order was sent twice"
    assert len(real_pos()) == 1
    assert h.signal(sid).status == SignalStatus.EXECUTED.value
