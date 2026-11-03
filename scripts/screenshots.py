"""Render README screenshots from SANDBOX mode (simulated broker + Telegram feed).

    QT_QPA_PLATFORM=offscreen python scripts/screenshots.py docs/screenshots
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("TGMT5_SANDBOX_INTERVAL", "2.5")
os.environ.setdefault("TGMT5_SANDBOX_VOL", "2.5")

from PySide6.QtWidgets import QApplication  # noqa: E402
import qasync  # noqa: E402

from tgmt5.__main__ import _sandbox_tasks, build_service  # noqa: E402
from tgmt5.domain.models import ExecutionMode  # noqa: E402
from tgmt5.ui import theme  # noqa: E402


async def run(out: Path, seconds: float) -> None:
    from tgmt5.ui.main_window import MainWindow
    svc = build_service(Path(tempfile.mkdtemp()), sandbox=True)
    svc.equity_snapshot_seconds = 3
    svc.config.risk.max_trades_per_hour = 60
    svc.config.risk.max_concurrent_trades = 8
    svc.config.risk.max_total_open_risk_pct = 6
    svc.config.risk.max_correlated_risk_pct = 5
    svc.config.notifications.desktop = False
    win = MainWindow(svc)
    win.resize(1560, 1000)
    win.show()
    await svc.start()
    asyncio.create_task(_sandbox_tasks(svc))
    await asyncio.sleep(2)
    await svc.confirm_automated_execution()
    await svc.set_mode(ExecutionMode.DEMO)
    await asyncio.sleep(seconds)
    out.mkdir(parents=True, exist_ok=True)
    for i, (name, _) in enumerate(__import__("tgmt5.ui.main_window", fromlist=["NAV"]).NAV):
        win.goto(i)
        if name == "History":
            win.pages[i].reload()
        await asyncio.sleep(1.6)
        win.grab().save(str(out / f"{i + 1:02d}-{name.lower()}.png"))
        print("saved", name)
    await svc.stop()


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots")
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60
    app = QApplication(sys.argv)
    app.setStyleSheet(theme.stylesheet(theme.set_theme("dark")))
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        loop.run_until_complete(run(out, seconds))


if __name__ == "__main__":
    main()
