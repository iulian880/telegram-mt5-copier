import pytest

from tgmt5.domain.models import Direction
from tgmt5.mt5.sim import fx_spec, gold_spec, with_spec
from tgmt5.risk.sizing import (
    SizingError, fixed_lot, floor_to_step, loss_at_stop, size_for_risk, split_volume,
)
from tests.conftest import make_broker


@pytest.fixture
def b():
    return make_broker()


def test_floor_never_rounds_up():
    assert floor_to_step(0.019999, 0.01) == 0.01
    assert floor_to_step(0.03, 0.01) == 0.03          # float noise must not lose a step
    assert floor_to_step(1.2345, 0.1) == 1.2
    assert floor_to_step(7, 1) == 7


def test_eurusd_risk_sizing(b):
    # 30 pip SL on EURUSD, $50 risk -> loss per lot $300 -> 0.16 lots ($48)
    spec = b.symbol_spec("EURUSD")
    r = size_for_risk(b, spec, Direction.BUY, 1.08510, 1.08210, 50.0)
    assert r.volume == 0.16
    assert r.risk_money <= 50.0
    assert r.method == "order_calc_profit"


def test_xauusd_risk_sizing_uses_contract_size(b):
    # XAU contract 100 oz: $10 SL distance -> $1000 per lot. $50 -> 0.05 lots
    spec = b.symbol_spec("XAUUSD")
    r = size_for_risk(b, spec, Direction.BUY, 2660.20, 2650.20, 50.0)
    assert r.volume == 0.05 and r.risk_money == pytest.approx(50.0, abs=0.01)


def test_contract_spec_is_respected_not_assumed(b):
    # Broker with a different gold contract size (e.g. 10 oz) must size 10x larger
    b.add_symbol(gold_spec("XAUUSD.x", contract=10), 2660.00, 2660.20)
    r = size_for_risk(b, b.symbol_spec("XAUUSD.x"), Direction.BUY, 2660.20, 2650.20, 50.0)
    assert r.volume == 0.5


def test_usdjpy_currency_conversion(b):
    # profit currency JPY, account USD: 50 pips = 0.5 JPY * 100k = 50,000 JPY = ~$333/lot
    spec = b.symbol_spec("USDJPY")
    r = size_for_risk(b, spec, Direction.SELL, 150.000, 150.500, 100.0)
    per_lot = 50_000 / 150.006
    assert r.loss_per_lot == pytest.approx(per_lot, rel=1e-3)
    assert r.volume == 0.3 and r.risk_money <= 100


def test_tick_value_fallback_when_calc_profit_unavailable(b):
    b.calc_profit_available = False
    spec = b.symbol_spec("EURUSD")
    r = size_for_risk(b, spec, Direction.BUY, 1.08510, 1.08210, 50.0)
    assert r.method == "tick_value" and r.volume == 0.16


def test_min_volume_exceeds_risk_rejected(b):
    spec = b.symbol_spec("XAUUSD")
    with pytest.raises(SizingError, match="minimum volume"):
        # 0.01 lot with $50 SL distance = $50 > $10 budget
        size_for_risk(b, spec, Direction.BUY, 2660.0, 2610.0, 10.0)


def test_volume_max_and_cap(b):
    spec = with_spec(b.symbol_spec("EURUSD"), volume_max=0.5)
    b.specs["EURUSD"] = spec
    r = size_for_risk(b, spec, Direction.BUY, 1.08510, 1.08500, 10_000.0)
    assert r.volume == 0.5
    r2 = size_for_risk(b, spec, Direction.BUY, 1.08510, 1.08500, 10_000.0, max_volume=0.2)
    assert r2.volume == 0.2


def test_volume_step_respected(b):
    b.specs["EURUSD"] = with_spec(b.symbol_spec("EURUSD"), volume_step=0.1, volume_min=0.1)
    r = size_for_risk(b, b.symbol_spec("EURUSD"), Direction.BUY, 1.08510, 1.08210, 100.0)
    assert r.volume == 0.3   # 0.333 -> floor to 0.3


def test_sl_on_wrong_side(b):
    with pytest.raises(SizingError):
        loss_at_stop(b, b.symbol_spec("EURUSD"), Direction.BUY, 1.0, 1.08, 1.09)


def test_fixed_lot_reports_real_risk(b):
    r = fixed_lot(b, b.symbol_spec("XAUUSD"), Direction.SELL, 2660.0, 2670.0, 0.1)
    assert r.volume == 0.1 and r.risk_money == pytest.approx(100.0)


# ------------------------------------------------------- multi-TP allocation
def test_split_never_exceeds_total():
    spec = fx_spec("EURUSD")
    for total in (0.03, 0.07, 0.1, 0.33, 1.0, 2.57):
        legs = split_volume(total, [0.5, 0.3, 0.2], spec)
        assert legs and sum(legs) <= total + 1e-9
        assert all(v >= spec.volume_min for v in legs)
        assert sum(legs) == pytest.approx(total)    # leftover goes to TP1, nothing lost


def test_split_impossible_returns_empty():
    assert split_volume(0.02, [0.5, 0.3, 0.2], fx_spec("EURUSD")) == []


def test_three_tps_do_not_triple_risk(b):
    """Risk is sized once for the whole signal, then split - never per TP."""
    spec = b.symbol_spec("XAUUSD")
    r = size_for_risk(b, spec, Direction.BUY, 2660.20, 2650.20, 50.0)
    legs = split_volume(r.volume, [1, 1, 1], spec)
    total_loss = sum(loss_at_stop(b, spec, Direction.BUY, v, 2660.20, 2650.20)[0] for v in legs)
    assert total_loss <= 50.0 + 0.01
