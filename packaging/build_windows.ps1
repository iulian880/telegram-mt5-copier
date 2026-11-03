# Build the Windows executable. Run from the repository root in PowerShell.
$ErrorActionPreference = "Stop"
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest -q
pyinstaller packaging\tgmt5.spec --noconfirm
Write-Host "Built: dist\TGMT5\TGMT5.exe"
