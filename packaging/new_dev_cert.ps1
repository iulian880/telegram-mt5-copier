# Create a self-signed code-signing certificate "CN=TGMT5 Dev" in CurrentUser\My.
# For local/test builds only: Windows does NOT trust it, so SmartScreen still warns.
# For public releases use a real certificate (see docs/INSTALL_WINDOWS.md, "Code signing").
$ErrorActionPreference = "Stop"
$existing = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert |
    Where-Object { $_.Subject -eq "CN=TGMT5 Dev" -and $_.NotAfter -gt (Get-Date) }
if ($existing) { Write-Host "Already exists: $($existing[0].Thumbprint)"; return }
$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=TGMT5 Dev" `
    -CertStoreLocation Cert:\CurrentUser\My -KeyAlgorithm RSA -KeyLength 3072 `
    -HashAlgorithm SHA256 -NotAfter (Get-Date).AddYears(2)
Write-Host "Created CN=TGMT5 Dev: $($cert.Thumbprint)"
