# Build the Windows executable. Run from the repository root in PowerShell.
$ErrorActionPreference = "Stop"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    # Newest installed Python 3 via the py launcher, else python on PATH.
    if (Get-Command py -ErrorAction SilentlyContinue) { py -3 -m venv .venv } else { python -m venv .venv }
    if ($LASTEXITCODE -ne 0) { throw "Could not create .venv (install Python 3.12+ from python.org)." }
}
& .venv\Scripts\python.exe -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 or newer is required (.venv has $(& .venv\Scripts\python.exe --version)). Delete .venv and install a newer Python." }
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest -q
pyinstaller packaging\tgmt5.spec --noconfirm
& "$PSScriptRoot\sign.ps1" -Check
if ($LASTEXITCODE -eq 0) { & "$PSScriptRoot\sign.ps1" dist\TGMT5\TGMT5.exe }
else { Write-Warning "No signing certificate: TGMT5.exe left unsigned (see packaging\sign.ps1)." }
Write-Host "Built: dist\TGMT5\TGMT5.exe"
