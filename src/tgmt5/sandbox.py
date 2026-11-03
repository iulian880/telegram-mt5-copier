"""SANDBOX mode: run the full application without MT5 or Telegram.

Uses the in-memory broker with random-walk prices and a scripted Telegram
feed so the UI and pipeline can be explored on any OS.  Everything is clearly
labelled SANDBOX in the UI; results are meaningless for real trading.
"""
from __future__ import annotations

import asyncio
import random

from tgmt5.mt5.sim import FakeMT5Gateway, fx_spec, gold_spec
from tgmt5.telegram.client import FakeTelegramSource

SANDBOX_CHAT = -100777
SANDBOX_LOGIN = 51000001
SANDBOX_SERVER = "SANDBOX-Demo"


def make_sandbox_broker() -> FakeMT5Gateway:
    b = FakeMT5Gateway(balance=10_000, login=SANDBOX_LOGIN, server=SANDBOX_SERVER,
                       company="Sandbox (simulated)")
    b.add_symbol(fx_spec("EURUSD"), 1.08500, 1.08508)
    b.add_symbol(fx_spec("GBPUSD"), 1.27000, 1.27010)
    b.add_symbol(fx_spec("USDJPY", digits=3, profit_ccy="JPY"), 150.000, 150.010)
    b.add_symbol(gold_spec(), 2660.00, 2660.15)
    return b


def random_walk(b: FakeMT5Gateway, scale: float = 1.0) -> None:
    for sym, t in list(b.ticks.items()):
        spec = b.specs[sym]
        vol = {"XAUUSD": 0.35, "USDJPY": 0.012}.get(sym, 0.00008)
        mid = (t.bid + t.ask) / 2 + random.gauss(0, vol * scale)
        half = (t.ask - t.bid) / 2
        b.set_price(sym, round(mid - half, spec.digits), round(mid + half, spec.digits))


def _script(b: FakeMT5Gateway) -> list[str]:
    g = b.ticks["XAUUSD"].ask
    e = b.ticks["EURUSD"].bid
    return [
        f"BUY XAUUSD NOW\nSL: {g - 6:.0f}\nTP1: {g + 4:.0f}\nTP2: {g + 8:.0f}\nTP3: {g + 12:.0f}",
        "Good morning traders! Big week ahead 🚀",
        f"EURUSD SELL @ {e:.5f}\nSL {e + 0.0030:.5f}\nTP {e - 0.0045:.5f}",
        "Move SL to BE",
        f"GOLD SELL {g + 3:.0f}-{g + 5:.0f}\nSTOP LOSS {g + 11:.0f}\nTAKE PROFIT {g - 6:.0f}",
        "BUY GOLD now",   # no SL -> manual review
        "TP1 HIT — Secure profits",
    ]


async def run_feed(tg: FakeTelegramSource, b: FakeMT5Gateway, interval: float = 20.0) -> None:
    await asyncio.sleep(3)
    while True:
        for text in _script(b):
            await tg.push(SANDBOX_CHAT, text, title="Sandbox Signals")
            await asyncio.sleep(interval)
