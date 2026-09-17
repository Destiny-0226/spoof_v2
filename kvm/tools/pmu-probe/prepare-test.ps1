# Run elevated in the test Guest. This script never starts the driver or reboots.
$ErrorActionPreference = 'Stop'
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator privileges required.'
}
$source = Join-Path $PSScriptRoot 'build\ovo-pmu-probe.sys'
$signTool = "${env:ProgramFiles(x86)}\Windows Kits\10\bin\10.0.28000.0\x64\signtool.exe"
if (!(Test-Path $signTool)) {
    $signTool = "${env:ProgramFiles(x86)}\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe"
}
$stage = Join-Path $PSScriptRoot 'test-signing'
$installed = "$env:WINDIR\System32\drivers\ovo-pmu-probe.sys"
if (!(Test-Path $source) -or !(Test-Path $signTool)) { throw 'Missing driver or SignTool.' }
if ((Test-Path $stage) -or (Test-Path $installed) -or
    (Get-Service OvoPmuProbe -ErrorAction SilentlyContinue)) {
    throw 'Existing preparation or service found; inspect before retrying.'
}
if (Confirm-SecureBootUEFI) { throw 'Secure Boot is enabled; no changes made.' }
$unsignedHash = (Get-FileHash $source -Algorithm SHA256).Hash
if ($unsignedHash -ne 'EF5DF8359A7D85348CCC0323956B3784450D50C01E78BC0A02964E02CEB92E6F') {
    throw 'Driver differs from the inspected build; review before signing.'
}
New-Item $stage -ItemType Directory | Out-Null
& bcdedit.exe /enum '{current}' | Out-File (Join-Path $stage 'bcd-before.txt')
if ($LASTEXITCODE) { throw 'Could not read BCD.' }
& bcdedit.exe /export (Join-Path $stage 'bcd-backup')
if ($LASTEXITCODE) { throw 'Could not export BCD.' }
$cert = New-SelfSignedCertificate -Type CodeSigningCert `
    -Subject 'CN=OVO PMU Probe Test Only' -FriendlyName 'OVO PMU Probe Test Only' `
    -CertStoreLocation Cert:\CurrentUser\My -KeyAlgorithm RSA -KeyLength 3072 `
    -HashAlgorithm SHA256 -KeyExportPolicy NonExportable -NotAfter (Get-Date).AddMonths(6)
$thumbprint = $cert.Thumbprint
# Record the exact certificate immediately, including for partial-failure cleanup.
[ordered]@{
    created = (Get-Date).ToString('o')
    thumbprint = $thumbprint
    sourceSha256 = $unsignedHash
    service = 'OvoPmuProbe'
    installedPath = $installed
    previousTestsigning = 'absent (confirmed before preparation)'
} | ConvertTo-Json | Set-Content (Join-Path $stage 'state.json') -Encoding ASCII
$cer = Join-Path $stage 'ovo-pmu-probe-test.cer'
Export-Certificate -Cert $cert -FilePath $cer | Out-Null
Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null
$signed = Join-Path $stage 'ovo-pmu-probe.sys'
Copy-Item $source $signed
& $signTool sign /v /fd SHA256 /s My /sha1 $thumbprint $signed
if ($LASTEXITCODE) { throw 'Driver signing failed.' }
& $signTool verify /v /pa $signed
if ($LASTEXITCODE) { throw 'Authenticode verification failed.' }
Get-FileHash $signed -Algorithm SHA256 | Format-List |
    Out-File (Join-Path $stage 'signed-sha256.txt')
Copy-Item $signed $installed
& sc.exe create OvoPmuProbe type= kernel start= demand binPath= $installed DisplayName= 'OVO Read-only PMU Probe'
if ($LASTEXITCODE) { throw 'Service registration failed.' }
& bcdedit.exe /set '{current}' testsigning on
if ($LASTEXITCODE) { throw 'Enabling test signing failed. Do not start the driver.' }
& bcdedit.exe /enum '{current}' | Out-File (Join-Path $stage 'bcd-after.txt')
& sc.exe qc OvoPmuProbe
& sc.exe query OvoPmuProbe
Write-Output "TEST_CERT_THUMBPRINT=$thumbprint"
Write-Output 'Prepared only. Restart the Guest manually before loading the driver.'
