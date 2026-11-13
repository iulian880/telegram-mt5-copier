from pathlib import Path

from tgmt5 import __main__ as entry


def test_default_data_dir_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.delenv("TGMT5_PORTABLE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert entry.default_data_dir() == tmp_path / "TGMT5"


def test_default_data_dir_portable_next_to_exe(monkeypatch, tmp_path):
    exe_dir = tmp_path / "usb"
    exe_dir.mkdir()
    monkeypatch.setenv("TGMT5_PORTABLE", "1")
    monkeypatch.setattr(entry.sys, "executable", str(exe_dir / "TGMT5-Portable.exe"))
    d = entry.default_data_dir()
    assert d == exe_dir.resolve() / "TGMT5-data"
    assert d.is_dir() and not (d / ".write-test").exists()


def test_default_data_dir_portable_falls_back_when_not_writable(monkeypatch, tmp_path):
    monkeypatch.setenv("TGMT5_PORTABLE", "1")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(entry.sys, "executable", str(tmp_path / "ro" / "TGMT5.exe"))

    def deny(self, *a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(Path, "mkdir", deny)
    assert entry.default_data_dir() == tmp_path / "TGMT5"
