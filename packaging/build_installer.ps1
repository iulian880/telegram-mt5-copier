# Build TGMT5-Setup-<version>.exe (in the repository root) from dist\TGMT5.
# Run from the repository root after packaging\build_windows.ps1.
$ErrorActionPreference = "Stop"
$candidates = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
)
$iscc = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found. Install it: winget install JRSoftware.InnoSetup" }
if (-not (Test-Path "dist\TGMT5\TGMT5.exe")) { throw "dist\TGMT5\TGMT5.exe missing. Run packaging\build_windows.ps1 first." }
# If a signing certificate is available, Inno Setup signs both the setup and the uninstaller.
$version = & .venv\Scripts\python.exe -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
$isccArgs = @("/DAppVersion=$version")
& "$PSScriptRoot\sign.ps1" -Check
if ($LASTEXITCODE -eq 0) {
    $signCmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `$q$PSScriptRoot\sign.ps1`$q `$f"
    $isccArgs += @("/DSIGN", "/Stgmt5sign=$signCmd")
} else { Write-Warning "No signing certificate: installer left unsigned (see packaging\sign.ps1)." }
& $iscc @isccArgs "packaging\installer.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
Write-Host "Built installer: TGMT5-Setup-*.exe in the repository root"
