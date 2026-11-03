# PyInstaller spec - build on Windows:  pyinstaller packaging/tgmt5.spec --noconfirm
# Produces dist/TGMT5/TGMT5.exe (one-folder build; faster start-up and fewer AV false positives).
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
src = root / "src" / "tgmt5"

a = Analysis(
    [str(src / "__main__.py")],
    pathex=[str(root / "src")],
    datas=[
        (str(src / "persistence" / "migrations"), "tgmt5/persistence/migrations"),
    ],
    hiddenimports=(collect_submodules("tgmt5") + collect_submodules("alembic")
                   + ["MetaTrader5", "keyring.backends.Windows", "qasync"]),
    excludes=["tkinter", "matplotlib", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="TGMT5", console=False,
          icon=str(root / "packaging" / "tgmt5.ico") if (root / "packaging" / "tgmt5.ico").exists() else None)
coll = COLLECT(exe, a.binaries, a.datas, name="TGMT5")
