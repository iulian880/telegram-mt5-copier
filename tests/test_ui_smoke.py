"""Offscreen GUI smoke test: every page builds and refreshes against live service data."""
import asyncio
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from tgmt5.ui import theme
    app.setStyleSheet(theme.stylesheet(theme.set_theme("dark")))
    return app


async def test_all_pages_render(qapp, tmp_path):
    from tgmt5.__main__ import build_service
    from tgmt5.domain.models import ExecutionMode
    from tgmt5.sandbox import SANDBOX_CHAT
    from tgmt5.ui.main_window import NAV, MainWindow

    svc = build_service(tmp_path, sandbox=True)
    win = MainWindow(svc)
    win.show()
    await svc.start()
    await svc.connect_mt5("x")
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    await svc.telegram.push(SANDBOX_CHAT, "BUY XAUUSD NOW SL 2650 TP1 2670 TP2 2680")
    await svc.telegram.push(SANDBOX_CHAT, "BUY GOLD now")
    await svc.queue.join()
    snap = await svc.snapshot()
    assert len(snap.positions) == 2
    for i, _ in enumerate(NAV):
        win.goto(i)
        win.pages[i].refresh(snap)
        qapp.processEvents()
        img = win.grab()
        assert not img.isNull()
    # the signal monitor shows both cards, one in manual review
    win.goto(1)
    assert win.pages[1].count.text() == "2 shown"
    win.close()
    await svc.stop()
