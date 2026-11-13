"""Named risk profiles on the Risk page: save, apply, rename, delete, survive export/import."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from tgmt5.config import AppConfig, SizingMode  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
async def page(qapp, tmp_path):
    from tgmt5.__main__ import build_service
    from tgmt5.ui.pages.risk import RiskPage
    svc = build_service(tmp_path, sandbox=True)
    await svc.start()
    p = RiskPage(svc)
    yield p
    await svc.stop()


async def test_save_apply_rename_delete(page):
    svc = page.svc
    # build a profile in the form: 0.01 on every TP, caps and drawdown protection off
    page.method.setCurrentIndex(page.method.findData(SizingMode.LOT_PER_TP.value))
    page.lot_tp.setValue(0.02)
    page.caps.setChecked(False)
    page.ddp.setChecked(False)
    await page.save_profile("Lingrid 300")
    assert "Lingrid 300" in svc.config.risk_profiles
    saved = svc.config.risk_profiles["Lingrid 300"]
    assert saved["risk"]["lot_per_tp"] == 0.02 and saved["risk"]["percent_caps_enabled"] is False
    assert "magic_number" not in saved["execution"]
    # saving a profile does not change the live settings
    assert svc.config.risk.sizing_mode is SizingMode.PCT_EQUITY

    page.load()                                   # back to the saved (live) settings in the form
    assert page._mode() is SizingMode.PCT_EQUITY
    page.apply_profile("Lingrid 300")              # loads into the form, not saved yet
    assert page._mode() is SizingMode.LOT_PER_TP and page.lot_tp.value() == 0.02
    assert not page.caps.isChecked() and not page.ddp.isChecked()
    assert svc.config.risk.sizing_mode is SizingMode.PCT_EQUITY

    await page.rename_profile("Lingrid 300", "Lingrid small")
    assert list(svc.config.risk_profiles) == ["Lingrid small"]
    assert page.preset.currentData() == "custom:Lingrid small"
    await page.delete_profile("Lingrid small")
    assert svc.config.risk_profiles == {}


async def test_profile_names_are_checked(page):
    with pytest.raises(ValueError):
        page._check_name("   ")
    with pytest.raises(ValueError):
        page._check_name("x" * 41)
    with pytest.raises(ValueError):
        page._check_name("Built-in copy")
    assert page._check_name("  Lingrid   300  ") == "Lingrid 300"
    await page.save_profile("A")
    await page.save_profile("B")
    with pytest.raises(ValueError, match="already exists"):
        page._check_name("b", exclude="A")      # renaming A to an existing name


async def test_broken_profile_is_rejected_without_touching_the_form(page):
    cfg = page.svc.config.model_copy(deep=True)
    cfg.risk_profiles["old"] = {"risk": {"sizing_mode": "NO_SUCH_MODE"}, "execution": {}}
    await page.svc.save_config(cfg)
    before = page._mode()
    with pytest.raises(ValueError, match="cannot be used"):
        page.apply_profile("old")
    assert page._mode() is before


def test_profiles_survive_export_and_import():
    cfg = AppConfig()
    cfg.risk_profiles["Mine"] = {"risk": {"sizing_mode": "LOT_PER_TP", "lot_per_tp": 0.01}, "execution": {}}
    assert AppConfig.import_json(cfg.export_json()).risk_profiles == cfg.risk_profiles
