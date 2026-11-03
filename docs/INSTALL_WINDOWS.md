# Windows installation

## Requirements
- Windows 10/11 (64-bit)
- Python 3.12 or newer, from python.org (tick "Add python.exe to PATH")
- MetaTrader 5 desktop terminal (the IC Markets download, or any MT5 build)
- A Telegram account that is a member of the signal groups

## Install from source
```powershell
git clone https://github.com/iulian880/telegram-mt5-copier
cd telegram-mt5-copier
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"     # also installs MetaTrader5 on Windows
pytest -q                   # optional: run the test suite
python -m tgmt5
```

Data is stored in `%LOCALAPPDATA%\TGMT5` (`tgmt5.db` plus rotating JSON logs in `logs\`). Credentials go to **Windows Credential Manager** under the service name `tgmt5`. Use `--data-dir` to put the data somewhere else.

## Build the executable
```powershell
.\packaging\build_windows.ps1
# → dist\TGMT5\TGMT5.exe   (copy the whole dist\TGMT5 folder)
```
The spec file is `packaging/tgmt5.spec`. It is a one-folder build and includes the Alembic migrations. To add an icon, put a `packaging/tgmt5.ico` file there.

## Sandbox mode
`python -m tgmt5 --sandbox` runs the whole app with a simulated broker and Telegram feed. It needs no credentials and its data is kept in a separate `sandbox` folder.
