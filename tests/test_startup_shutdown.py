"""Start-up auto-connect and shutdown that also works when the event loop can no longer run."""
from tgmt5.config import AppConfig, MT5Settings, SourceConfig
from tgmt5.domain.models import ExecutionMode
from tgmt5.persistence.db import Database
from tgmt5.secrets import MemoryKeyring, SecretStore
from tgmt5.services.app import AppService
from tgmt5.telegram.client import FakeTelegramSource
from tests.conftest import SOURCE, make_broker


def make(tmp_path, save_password: bool, auto: bool = True):
    broker = make_broker()
    secrets = SecretStore(MemoryKeyring())
    if save_password:
        secrets.set(SecretStore.mt5_password_key(broker.login), "pw")
    s = AppService(tmp_path, secrets=secrets, telegram=FakeTelegramSource(),
                   gateway_factory=lambda: broker, db=Database.at_path(tmp_path / "t.db"))
    s.config = AppConfig(sources=[SourceConfig(chat_id=SOURCE, title="VIP")],
                         mt5=MT5Settings(login=broker.login, server=broker.server, auto_connect=auto))
    return s, broker


async def wait_tasks(s):
    import asyncio
    for t in s._tasks:
        if t.get_name() == "mt5-auto-connect":
            await asyncio.wait_for(t, 5)


async def test_auto_connects_in_monitor_mode_when_password_saved(tmp_path):
    s, broker = make(tmp_path, save_password=True)
    await s.start()
    try:
        await wait_tasks(s)
        assert s.real_gw is not None and s.engine.mode is ExecutionMode.MONITOR
        snap = await s.snapshot()
        assert snap.mt5_connected
    finally:
        await s.stop()


async def test_no_auto_connect_without_saved_password_or_when_off(tmp_path):
    for save, auto in ((False, True), (True, False)):
        s, _ = make(tmp_path / f"{save}{auto}", save_password=save, auto=auto)
        await s.start()
        try:
            await wait_tasks(s)
            assert s.real_gw is None
        finally:
            await s.stop()


async def test_shutdown_now_is_synchronous_and_idempotent(tmp_path):
    s, broker = make(tmp_path, save_password=True)
    await s.start()
    await wait_tasks(s)
    s.shutdown_now()                 # what aboutToQuit calls when Windows/an installer closes the app
    assert s._closed and broker.connected is False
    s.shutdown_now()                 # second call is a no-op
    await s.stop()                   # and so is the normal async stop afterwards
