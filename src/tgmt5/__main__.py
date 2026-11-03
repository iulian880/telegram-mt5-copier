"""Entry point: ``python -m tgmt5`` (or the packaged ``TGMT5.exe``).

Options:
  --sandbox          run against a simulated broker and Telegram feed (any OS)
  --data-dir PATH    where the database and logs are stored
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path


def default_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "TGMT5" if base else Path.home() / ".tgmt5"


def build_service(data_dir: Path, sandbox: bool):
    from tgmt5.config import MT5Settings, SourceConfig
    from tgmt5.secrets import MemoryKeyring, SecretStore
    from tgmt5.services.app import AppService

    if sandbox:
        from tgmt5 import sandbox as sb
        from tgmt5.telegram.client import FakeTelegramSource
        broker = sb.make_sandbox_broker()
        svc = AppService(data_dir / "sandbox", secrets=SecretStore(MemoryKeyring()),
                         telegram=FakeTelegramSource(), gateway_factory=lambda: broker, sandbox=True)
        if not svc.config.sources:
            svc.config.sources = [SourceConfig(chat_id=sb.SANDBOX_CHAT, title="Sandbox Signals")]
        svc.config.mt5 = MT5Settings(login=sb.SANDBOX_LOGIN, server=sb.SANDBOX_SERVER)
        svc.sandbox_broker = broker
        return svc

    from tgmt5.mt5.real import RealMT5Gateway
    from tgmt5.telegram.client import TelethonSource
    secrets = SecretStore()
    return AppService(data_dir, secrets=secrets, telegram=TelethonSource(secrets),
                      gateway_factory=RealMT5Gateway)


async def _sandbox_tasks(svc) -> None:
    from tgmt5 import sandbox as sb
    await svc.connect_mt5("sandbox")
    asyncio.create_task(sb.run_feed(svc.telegram, svc.sandbox_broker,
                                    float(os.environ.get("TGMT5_SANDBOX_INTERVAL", "20"))))
    while True:
        await svc.run_mt5(sb.random_walk, svc.sandbox_broker,
                          float(os.environ.get("TGMT5_SANDBOX_VOL", "1")))
        await asyncio.sleep(1.0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tgmt5")
    ap.add_argument("--sandbox", action="store_true")
    ap.add_argument("--data-dir", type=Path, default=default_data_dir())
    args = ap.parse_args(argv)

    from tgmt5.logging_setup import setup_logging
    setup_logging(args.data_dir / "logs")

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFont, QGuiApplication
    from PySide6.QtWidgets import QApplication
    import qasync

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("TGMT5")
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    svc = build_service(args.data_dir, args.sandbox)
    from tgmt5.ui import theme
    pal = theme.set_theme(svc.config.theme)
    app.setStyleSheet(theme.stylesheet(pal))
    f = QFont()
    f.setFamilies(["Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", "Arial"])
    f.setPointSizeF(9.75)
    app.setFont(f)

    from tgmt5.ui.main_window import MainWindow
    win = MainWindow(svc)
    win.show()

    async def boot():
        await svc.start()
        if args.sandbox:
            asyncio.create_task(_sandbox_tasks(svc))

    close = asyncio.Event()
    app.aboutToQuit.connect(close.set)
    with loop:
        loop.create_task(boot())
        loop.run_until_complete(close.wait())
        loop.run_until_complete(svc.stop())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
