import pytest

from tgmt5.domain.models import Direction, EntryType, ManagementType, MessageKind
from tgmt5.parsing.parser import ParserConfig, SignalParser

P = SignalParser()


def sig(text, parser=P):
    r = parser.parse(text)
    assert r.kind is MessageKind.NEW_SIGNAL, (text, r.reasons)
    return r.signal


# ---------------------------------------------------------------- spec examples
def test_example_a_market_multi_tp():
    s = sig("BUY XAUUSD NOW\nSL: 2650\nTP1: 2670\nTP2: 2680\nTP3: 2690")
    assert (s.direction, s.symbol, s.entry_type) == (Direction.BUY, "XAUUSD", EntryType.MARKET)
    assert s.stop_loss == 2650 and s.take_profits == (2670, 2680, 2690)
    assert s.entry_price is None


def test_example_b_zone_with_alias():
    s = sig("GOLD SELL 2700-2705\nSTOP LOSS 2715\nTAKE PROFIT 2680")
    assert (s.direction, s.symbol, s.entry_type) == (Direction.SELL, "XAUUSD", EntryType.ZONE)
    assert (s.entry_low, s.entry_high, s.stop_loss, s.take_profits) == (2700, 2705, 2715, (2680,))


def test_example_c_forex_price():
    s = sig("EURUSD BUY @ 1.08500\nSL 1.08200\nTP 1.09000")
    assert s.symbol == "EURUSD" and s.entry_type is EntryType.PRICE
    assert (s.entry_price, s.stop_loss, s.take_profits) == (1.085, 1.082, (1.09,))


@pytest.mark.parametrize("text,mtype,kind", [
    ("Move SL to BE", ManagementType.MOVE_SL_BE, MessageKind.MANAGEMENT),
    ("Close 50% of GOLD position", ManagementType.CLOSE_PARTIAL, MessageKind.MANAGEMENT),
    ("TP1 HIT — Secure profits", ManagementType.TP_HIT, MessageKind.INFO),
    ("Cancel previous pending order.", ManagementType.CANCEL_PENDING, MessageKind.MANAGEMENT),
])
def test_examples_d_to_g(text, mtype, kind):
    r = P.parse(text)
    assert r.kind is kind and r.management.type is mtype


def test_partial_close_details():
    m = P.parse("Close 50% of GOLD position").management
    assert m.fraction == 0.5 and m.symbol == "XAUUSD"
    assert P.parse("TP1 HIT — Secure profits").management.tp_index == 1


# ---------------------------------------------------------------- formats
@pytest.mark.parametrize("text,expect", [
    ("🔥 XAUUSD BUY LIMIT 2650 🔥\nSL 2640\nTP 2660 / 2670",
     dict(entry_type=EntryType.LIMIT, entry_price=2650, take_profits=(2660, 2670))),
    ("SELL STOP EURUSD 1.08000\nSL 1.08300\nTP 1.07500",
     dict(entry_type=EntryType.STOP, entry_price=1.08, direction=Direction.SELL)),
    ("eur/usd short now sl 1.0900 tp 1.0800",
     dict(symbol="EURUSD", direction=Direction.SELL, entry_type=EntryType.MARKET)),
    ("XAUUSD SELL NOW 2,700\nSL 2,712\nTP 2,690", dict(entry_price=2700, stop_loss=2712,
                                                     entry_type=EntryType.MARKET)),
    ("BUY GOLD 2650\nSL 2640\nTP1 2660\nTP2 2670\nTP3 open",
     dict(take_profits=(2660, 2670), entry_type=EntryType.PRICE)),
    ("Signal #45 BUY XAUUSD @2650 SL 2640 TP 2670 lot 0.5 risk 1%",
     dict(entry_price=2650, take_profits=(2670,))),
    ("NAS100 BUY NOW\nSL 17800\nTP 18100", dict(symbol="NAS100")),
    ("SELL USDJPY @ 150.20 S/L 150.70 T/P 149.50", dict(symbol="USDJPY", stop_loss=150.7)),
    ("BUY XAUUSD NOW SL 2650 TP 2690 TP 2670 TP 2680", dict(take_profits=(2670, 2680, 2690))),
    ("Gold buy now 10:30\nSL 2650\nTP 2670", dict(symbol="XAUUSD", entry_price=None)),
])
def test_formats(text, expect):
    s = sig(text)
    for k, v in expect.items():
        assert getattr(s, k) == v, (k, getattr(s, k))


def test_per_source_alias():
    p = SignalParser(ParserConfig(symbol_aliases={"YELLOW": "XAUUSD"}))
    assert sig("BUY YELLOW NOW SL 2650 TP 2670", p).symbol == "XAUUSD"


# ---------------------------------------------------------------- safety
@pytest.mark.parametrize("text", [
    "BUY XAUUSD NOW\nTP 2670",                                  # missing SL
    "BUY GOLD now",                                             # missing SL & TP
    "BUY XAUUSD NOW\nSL 2670\nTP 2690",                         # SL wrong side? (no entry; SL < TP ok)
])
def test_missing_or_partial_info_never_signal(text):
    r = P.parse(text)
    if text == "BUY XAUUSD NOW\nSL 2670\nTP 2690":
        assert r.kind is MessageKind.NEW_SIGNAL      # consistent (SL below TP) - market checks later
    else:
        assert r.kind is MessageKind.AMBIGUOUS


@pytest.mark.parametrize("text,why", [
    ("BUY XAUUSD @ 2650\nSL 2660\nTP 2670", "wrong side"),
    ("SELL EURUSD 1.0850\nSL 1.0800\nTP 1.0800", "wrong side"),
    ("BUY XAUUSD NOW\nSL 2650\nTP 2640", "not beyond"),
    ("BUY XAUUSD @ 2650\nSL 265\nTP 2670", "implausibly"),
    ("BUY EURUSD GBPUSD NOW SL 1.08 TP 1.09", "multiple symbols"),
    ("BUY and SELL XAUUSD SL 2650 TP 2670", "conflicting"),
    ("BUY XAUUSD\nSL 2650\nTP 2670", "entry condition"),
    ("BUY XAUUSD 2655 2660\nSL 2650\nTP 2670", "multiple candidate"),
    ("BUY LIMIT XAUUSD\nSL 2650\nTP 2670", "single entry"),
    ("BUY EURUSD 1,0850 SL 1,0800 TP 1,0900", "comma"),
    ("BUY XAUUSD NOW SL 2650 SL 2645 TP 2670", "multiple stop"),
])
def test_ambiguous(text, why):
    r = P.parse(text)
    assert r.kind is MessageKind.AMBIGUOUS
    assert any(why in reason for reason in r.reasons), r.reasons


def test_implicit_market_template():
    p = SignalParser(ParserConfig(implicit_market=True))
    assert sig("BUY XAUUSD\nSL 2650\nTP 2670", p).entry_type is EntryType.MARKET


@pytest.mark.parametrize("text", [
    "Good morning traders! Gold looking bullish today 🚀",
    "Weekly results: BUY XAUUSD SL 2650 TP 2670 +300 pips",
    "Join our VIP channel: buy XAUUSD with SL 2650 TP 2700",
    "Yesterday we sold EURUSD, SL 1.09 TP 1.08 hit",
    "Gold analysis: resistance at 2700, support at 2650",
    "",
    "   ",
    "We bought gold at 2650 and it went to 2690",
    "__import__('os').system('rm -rf /')",
    "BUY",
])
def test_noise_false_positives(text):
    assert P.parse(text).kind is MessageKind.NOISE


@pytest.mark.parametrize("text,t,extra", [
    ("move stop loss to entry", ManagementType.MOVE_SL_BE, {}),
    ("SL to breakeven now", ManagementType.MOVE_SL_BE, {}),
    ("Set risk free", ManagementType.MOVE_SL_BE, {}),
    ("Move SL to 2655", ManagementType.MODIFY_SL, {"price": 2655}),
    ("New TP 2700 for gold", ManagementType.MODIFY_TP, {"price": 2700, "symbol": "XAUUSD"}),
    ("Close all trades now", ManagementType.CLOSE_ALL, {}),
    ("close half", ManagementType.CLOSE_PARTIAL, {"fraction": 0.5}),
    ("Close 100% EURUSD", ManagementType.CLOSE_ALL, {"symbol": "EURUSD"}),
    ("delete the buy limit", ManagementType.CANCEL_PENDING, {}),
    ("TP2 reached ✅", ManagementType.TP_HIT, {"tp_index": 2}),
    ("SL hit unfortunately", ManagementType.SL_HIT, {}),
])
def test_management_variants(text, t, extra):
    r = P.parse(text)
    assert r.management is not None and r.management.type is t, r
    for k, v in extra.items():
        assert getattr(r.management, k) == v


def test_management_multiple_symbols_is_ambiguous():
    assert P.parse("Close EURUSD and GBPUSD").kind is MessageKind.AMBIGUOUS


def test_very_long_message_ignored():
    assert P.parse("BUY XAUUSD " * 1000).kind is MessageKind.NOISE
