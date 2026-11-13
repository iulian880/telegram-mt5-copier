# Build the portable single-file TGMT5-Portable-<version>.exe in the repository root.
# Run from the repository root. Needs .venv with the dev extras (packaging\build_windows.ps1 makes it).
$ErrorActionPreference = "Stop"
if (-not (Test-Path ".venv\Scripts\pyinstaller.exe")) { throw ".venv missing. Run packaging\build_windows.ps1 first." }
$version = & .venv\Scripts\python.exe -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
# PyInstaller logs to stderr; in Windows PowerShell 5.1 that would abort under "Stop", so rely on the exit code.
$ErrorActionPreference = "Continue"
& .venv\Scripts\pyinstaller.exe packaging\tgmt5_portable.spec --noconfirm --log-level WARN --distpath build\portable-dist --workpath build\portable-work
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
$ErrorActionPreference = "Stop"
$out = "TGMT5-Portable-$version.exe"
Move-Item -Force "build\portable-dist\TGMT5-Portable.exe" $out
& "$PSScriptRoot\sign.ps1" -Check
if ($LASTEXITCODE -eq 0) { & "$PSScriptRoot\sign.ps1" $out }
else { Write-Warning "No signing certificate: $out left unsigned (see packaging\sign.ps1)." }
Write-Host "Built: $out"
