# Authenticode-sign one or more files with signtool (SHA-256 + RFC 3161 timestamp).
#
# Which certificate is used (the first one found wins):
#   1. $env:TGMT5_SIGN_PFX (+ $env:TGMT5_SIGN_PFX_PASSWORD) - a .pfx file, e.g. a purchased cert
#   2. $env:TGMT5_SIGN_THUMBPRINT - a cert in CurrentUser\My (e.g. a cert on a hardware token)
#   3. "CN=TGMT5 Dev" in CurrentUser\My - self-signed, made by packaging\new_dev_cert.ps1
#
#   sign.ps1 -Check            exit 0 if a certificate is available, 1 if not
#   sign.ps1 file1.exe [...]   sign the files (fails if there is no certificate)
# Set $env:TGMT5_SIGN_NO_TIMESTAMP=1 to sign offline without a timestamp.
param([switch]$Check, [Parameter(ValueFromRemainingArguments)][string[]]$Path)
$ErrorActionPreference = "Stop"

function Find-SignArgs {
    if ($env:TGMT5_SIGN_PFX) {
        if (-not (Test-Path $env:TGMT5_SIGN_PFX)) { throw "TGMT5_SIGN_PFX not found: $env:TGMT5_SIGN_PFX" }
        $a = @("/f", $env:TGMT5_SIGN_PFX)
        if ($env:TGMT5_SIGN_PFX_PASSWORD) { $a += @("/p", $env:TGMT5_SIGN_PFX_PASSWORD) }
        return $a
    }
    if ($env:TGMT5_SIGN_THUMBPRINT) { return @("/s", "My", "/sha1", $env:TGMT5_SIGN_THUMBPRINT) }
    # Read the store through .NET, not the Cert: drive: Inno Setup runs this script in Windows
    # PowerShell 5.1, and when that is started from PowerShell 7 it can fail to load the module
    # that provides Cert: ("A parameter cannot be found that matches parameter name 'CodeSigningCert'").
    $store = New-Object System.Security.Cryptography.X509Certificates.X509Store("My", "CurrentUser")
    $store.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadOnly)
    $dev = $null
    try {
        foreach ($c in $store.Certificates) {
            if ($c.Subject -eq "CN=TGMT5 Dev" -and $c.HasPrivateKey -and $c.NotAfter -gt [DateTime]::Now -and
                (-not $dev -or $c.NotAfter -gt $dev.NotAfter)) { $dev = $c }
        }
    } finally { $store.Close() }
    if ($dev) { return @("/s", "My", "/sha1", $dev.Thumbprint) }
    return $null
}

$certArgs = Find-SignArgs
if ($Check) { if ($certArgs) { exit 0 } else { exit 1 } }
if (-not $certArgs) { throw "No signing certificate. Run packaging\new_dev_cert.ps1 or set TGMT5_SIGN_PFX / TGMT5_SIGN_THUMBPRINT." }
if (-not $Path) { throw "No files given to sign." }

$signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
if (-not $signtool) { throw "signtool.exe not found. Install the Windows SDK (winget install Microsoft.WindowsSDK.10.0.26100)." }

$tsArgs = if ($env:TGMT5_SIGN_NO_TIMESTAMP) { @() } else { @("/tr", "http://timestamp.digicert.com", "/td", "sha256") }
& $signtool sign /fd sha256 @certArgs @tsArgs /d "TGMT5" @Path
if ($LASTEXITCODE -ne 0) { throw "signtool failed ($LASTEXITCODE)" }
