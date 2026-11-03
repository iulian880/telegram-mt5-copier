from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import count

import pytest

from tgmt5.config import AppConfig, SourceConfig
from tgmt5.domain.models import ExecutionMode, MessageEventType, TelegramMessage
from tgmt5.execution.engine import ExecutionEngine
from tgmt5.mt5.guard import DemoOnlyGateway
from tgmt5.mt5.sim import FakeMT5Gateway, fx_spec, gold_spec
from tgmt5.persistence.db import Database
from tgmt5.risk.engine import RiskEngine

SOURCE = -1001234567890


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset

    def advance(self, **kw) -> None:
        self.offset += timedelta(**kw)


def make_broker(**kw) -> FakeMT5Gateway:
    b = FakeMT5Gateway(**kw)
    b.add_symbol(fx_spec("EURUSD"), 1.08500, 1.08510)
    b.add_symbol(fx_spec("GBPUSD"), 1.27000, 1.27012)
    b.add_symbol(fx_spec("USDJPY", digits=3, profit_ccy="JPY"), 150.000, 150.012)
    b.add_symbol(gold_spec(), 2660.00, 2660.20)
    b.connected = True
    return b


class Harness:
    def __init__(self, mode: ExecutionMode = ExecutionMode.DEMO, hedging: bool = True,
                 db: Database | None = None, broker: FakeMT5Gateway | None = None,
                 cfg: AppConfig | None = None) -> None:
        self.clock = Clock()
        self.db = db or Database.in_memory()
        self.cfg = cfg or AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="VIP Signals")])
        self.broker = broker or make_broker(hedging=hedging)
        self.gw = DemoOnlyGateway(self.broker, self.broker.login, self.broker.server)
        self.risk = RiskEngine(lambda: self.cfg.risk, lambda: self.db.get_setting("risk_state"),
                               lambda v: self.db.set_setting("risk_state", v), clock=self.clock)
        self.events: list[tuple[str, dict]] = []
        self.engine = ExecutionEngine(self.db, self.gw, self.risk, lambda: self.cfg, mode=mode,
                                      clock=self.clock, emit=lambda k, d: self.events.append((k, d)))
        acct = self.broker.account_info()
        self.risk.state.confirmed_account = f"{acct.login}@{acct.server}"
        self.engine.refresh_account()
        self._ids = count(100)

    def send(self, text: str, *, reply_to: int | None = None, age_s: float = 1.0,
             msg_id: int | None = None, source: int = SOURCE,
             event: MessageEventType = MessageEventType.NEW) -> int | None:
        mid = msg_id if msg_id is not None else next(self._ids)
        now = self.clock()
        self.last_msg_id = mid
        return self.engine.handle_message(TelegramMessage(
            source_id=source, message_id=mid, text=text, date=now - timedelta(seconds=age_s),
            event=event, reply_to_message_id=reply_to, received_at=now))

    def signal(self, sid: int):
        from tgmt5.persistence.models import SignalRow
        with self.db.session() as s:
            row = s.get(SignalRow, sid)
            _ = list(row.executions), list(row.events)
            return row


@pytest.fixture
def h() -> Harness:
    return Harness()
