# PyInstaller spec for the PORTABLE build: one self-contained .exe, no installation needed.
# Build with packaging\build_portable.ps1 (→ TGMT5-Portable-<version>.exe in the repository root).
# Data goes into "TGMT5-data" next to the .exe (see rthook_portable.py and tgmt5.__main__).
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
src = root / "src" / "tgmt5"
icon = root / "packaging" / "tgmt5.ico"

a = Analysis(
    [str(src / "__main__.py")],
    pathex=[str(root / "src")],
    datas=[
        (str(src / "persistence" / "migrations"), "tgmt5/persistence/migrations"),
    ],
    hiddenimports=(collect_submodules("tgmt5") + collect_submodules("alembic")
                   + ["MetaTrader5", "keyring.backends.Windows", "qasync"]),
    excludes=["tkinter", "matplotlib", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore"],
    runtime_hooks=[str(root / "packaging" / "rthook_portable.py")],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="TGMT5-Portable", console=False,
          upx=False, icon=str(icon) if icon.exists() else None)
