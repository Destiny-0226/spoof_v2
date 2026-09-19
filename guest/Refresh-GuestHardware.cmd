@echo off
setlocal EnableExtensions
cd /d "%~dp0"

net session >nul 2>&1
if not %errorLevel%==0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo Cleaning ghost devices and refreshing network...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p='%~f0'; $t=Get-Content -Raw -LiteralPath $p; $m='___GUEST_REFRESH_PS___'; $i=$t.LastIndexOf($m); if ($i -lt 0) { throw 'script marker missing' }; $PSScriptRoot = Split-Path -Parent $p; Invoke-Expression $t.Substring($i + $m.Length)"
echo.
pause
exit /b

___GUEST_REFRESH_PS___
$WhatIf = $false
$HardNet = $false
$Reboot = $false
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$script:LogLines = New-Object System.Collections.Generic.List[string]
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$rebootNeeded = $false

function Get-LogDir {
    foreach ($d in @($PSScriptRoot, [Environment]::GetFolderPath('Desktop'), $env:TEMP)) {
        if (-not $d) { continue }
        try {
            $probe = Join-Path $d '.guest-refresh-write-test'
            [System.IO.File]::WriteAllText($probe, '1')
            Remove-Item -LiteralPath $probe -Force
            return $d
        } catch {}
    }
    return $env:TEMP
}

$logDir = Get-LogDir
$tempLog = Join-Path $logDir ("guest-refresh-{0}.log" -f $stamp)

$protectedClasses = @('Computer', 'Processor')
$leftoverDriverNames = @(
    'netkvm', 'vioserial', 'viostor', 'vioscsi', 'viorng', 'balloon',
    'qxldod', 'qxl', 'pvpanic', 'fwcfg', 'virtiofs', 'vioinput',
    'viogpudo', 'viofs', 'vioser'
)

function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    $script:LogLines.Add($line)
    Write-Output $line
}

function Save-Log {
    try {
        $text = $script:LogLines -join "`r`n"
        [System.IO.File]::WriteAllText($tempLog, $text, [System.Text.UTF8Encoding]::new($true))
    } catch {}
}

function Get-DevicePresent {
    param([string]$InstanceId)
    try {
        $prop = Get-PnpDeviceProperty -InstanceId $InstanceId -KeyName 'DEVPKEY_Device_IsPresent' -ErrorAction Stop
        if ($null -ne $prop.Data) { return [bool]$prop.Data }
    } catch {}
    $dev = Get-PnpDevice -InstanceId $InstanceId -ErrorAction SilentlyContinue
    if ($null -eq $dev) { return $false }
    return ($dev.Status -eq 'OK')
}

function Get-DeviceProblem {
    param([string]$InstanceId)
    try {
        $prop = Get-PnpDeviceProperty -InstanceId $InstanceId -KeyName 'DEVPKEY_Device_ProblemCode' -ErrorAction Stop
        if ($null -ne $prop.Data) { return [int]$prop.Data }
    } catch {}
    return -1
}

function Test-ProtectedDevice {
    param($Device, [string[]]$LiveNetIds, [string[]]$BootDiskIds)
    if ($protectedClasses -contains $Device.Class) { return $true }
    if ($LiveNetIds -contains $Device.InstanceId) { return $true }
    if ($BootDiskIds -contains $Device.InstanceId) { return $true }
    return $false
}

function Get-LiveNetInstanceIds {
    $ids = New-Object System.Collections.Generic.List[string]
    Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.PnPDeviceID) { [void]$ids.Add($_.PnPDeviceID) }
    }
    Get-CimInstance Win32_NetworkAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.PNPDeviceID } |
        ForEach-Object { [void]$ids.Add($_.PNPDeviceID) }
    return @($ids | Select-Object -Unique)
}

function Get-BootDiskInstanceIds {
    $ids = New-Object System.Collections.Generic.List[string]
    try {
        $boot = Get-Disk | Where-Object { $_.IsBoot -eq $true }
        foreach ($d in $boot) {
            $pnp = (Get-CimInstance Win32_DiskDrive -ErrorAction SilentlyContinue |
                Where-Object { $_.Index -eq $d.Number }).PNPDeviceID
            if ($pnp) { [void]$ids.Add($pnp) }
        }
    } catch {}
    return @($ids | Select-Object -Unique)
}

function Remove-GhostDevice {
    param([string]$InstanceId, [string]$Name)
    if ($WhatIf) {
        Write-Log ("WHATIF remove {0} | {1}" -f $Name, $InstanceId)
        return $true
    }
    $pnp = Get-Command pnputil.exe -ErrorAction SilentlyContinue
    if ($pnp) {
        $out = & pnputil.exe /remove-device $InstanceId /subtree 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) {
            $out = & pnputil.exe /remove-device $InstanceId /subtree /force 2>&1 | Out-String
        }
        if ($LASTEXITCODE -eq 0) {
            Write-Log ("removed {0} | {1}" -f $Name, $InstanceId)
            return $true
        }
        Write-Log ("pnputil failed ({0}): {1} | {2}" -f $LASTEXITCODE, $Name, ($out.Trim())) 'WARN'
    }
    try {
        Remove-PnpDevice -InstanceId $InstanceId -Confirm:$false -ErrorAction Stop
        Write-Log ("Remove-PnpDevice {0} | {1}" -f $Name, $InstanceId)
        return $true
    } catch {
        Write-Log ("keep (in use or denied) {0} | {1} | {2}" -f $Name, $InstanceId, $_.Exception.Message) 'WARN'
        return $false
    }
}

function Get-GhostDevices {
    $all = @(Get-PnpDevice -ErrorAction SilentlyContinue)
    $ghosts = New-Object System.Collections.Generic.List[object]
    foreach ($d in $all) {
        if (-not $d.InstanceId) { continue }
        $present = Get-DevicePresent $d.InstanceId
        $problem = Get-DeviceProblem $d.InstanceId
        $phantom = ($problem -eq 24)
        if ($present -and -not $phantom) { continue }
        [void]$ghosts.Add($d)
    }
    return @($ghosts | Sort-Object { $_.InstanceId.Split('\').Count } -Descending)
}

function Remove-EnumPhantomKeys {
    $enumRoot = 'HKLM:\SYSTEM\CurrentControlSet\Enum'
    if (-not (Test-Path $enumRoot)) { return }
    $removed = 0
    Get-ChildItem $enumRoot -ErrorAction SilentlyContinue | ForEach-Object {
        $enumerator = $_
        Get-ChildItem $enumerator.PSPath -ErrorAction SilentlyContinue | ForEach-Object {
            $device = $_
            Get-ChildItem $device.PSPath -ErrorAction SilentlyContinue | ForEach-Object {
                $inst = $_
                $props = Get-ItemProperty $inst.PSPath -ErrorAction SilentlyContinue
                if ($null -eq $props) { return }
                if ($props.Phantom -ne 1) { return }
                $instanceId = '{0}\{1}\{2}' -f $enumerator.PSChildName, $device.PSChildName, $inst.PSChildName
                if (Get-DevicePresent $instanceId) { return }
                if ($WhatIf) {
                    Write-Log ("WHATIF enum phantom {0}" -f $instanceId)
                    return
                }
                $null = Remove-GhostDevice -InstanceId $instanceId -Name ('Enum Phantom ' + $props.DeviceDesc)
                $removed++
            }
        }
    }
    Write-Log ("enum phantom pass done, attempted {0}" -f $removed)
}

function Test-BuiltinInterfaceGuid {
    param([string]$Guid)
    $g = $Guid.ToLower().Trim('{}')
    if ($g -match '806e6f6e6963$') { return $true }
    if ($g -eq '00000000-0000-0000-0000-000000000000') { return $true }
    return $false
}

function Get-LiveInterfaceGuids {
    $guids = New-Object System.Collections.Generic.List[string]
    Get-NetAdapter -IncludeHidden -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.InterfaceGuid) { [void]$guids.Add($_.InterfaceGuid.ToString().ToLower()) }
    }
    return @($guids | Select-Object -Unique)
}

function Remove-StaleTcpipInterfaces {
    $live = Get-LiveInterfaceGuids
    $roots = @(
        'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces',
        'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters\Interfaces'
    )
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
            $guid = $_.PSChildName.ToLower()
            if ($live -contains $guid) { return }
            if (Test-BuiltinInterfaceGuid $guid) { return }
            if ($WhatIf) {
                Write-Log ("WHATIF tcpip interface {0}" -f $guid)
                return
            }
            try {
                Remove-Item -LiteralPath $_.PSPath -Recurse -Force -ErrorAction Stop
                Write-Log ("removed stale tcpip interface {0}" -f $guid)
            } catch {
                Write-Log ("tcpip interface delete failed {0} | {1}" -f $guid, $_.Exception.Message) 'WARN'
            }
        }
    }
}

function Reset-DhcpDuid {
    $paths = @(
        'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters'
    )
    $names = @('Dhcpv6DUID', 'Dhcpv6DUIDType')
    foreach ($path in $paths) {
        if (-not (Test-Path $path)) { continue }
        foreach ($name in $names) {
            if ($WhatIf) {
                Write-Log ("WHATIF delete {0}\{1}" -f $path, $name)
                continue
            }
            try {
                if ($null -ne (Get-ItemProperty $path -Name $name -ErrorAction SilentlyContinue)) {
                    Remove-ItemProperty -LiteralPath $path -Name $name -Force -ErrorAction Stop
                    Write-Log ("deleted {0}\{1}" -f $path, $name)
                }
            } catch {
                Write-Log ("DUID delete failed {0}\{1} | {2}" -f $path, $name, $_.Exception.Message) 'WARN'
            }
        }
    }
    $ifRoot = 'HKLM:\SYSTEM\CurrentControlSet\Services\Tcpip6\Parameters\Interfaces'
    if (Test-Path $ifRoot) {
        Get-ChildItem $ifRoot -ErrorAction SilentlyContinue | ForEach-Object {
            foreach ($name in @('Dhcpv6DUID', 'Dhcpv6IAID', 'Dhcpv6IdentityAssociation', 'Lease', 'DhcpServer')) {
                if ($WhatIf) { continue }
                try {
                    if ($null -ne (Get-ItemProperty $_.PSPath -Name $name -ErrorAction SilentlyContinue)) {
                        Remove-ItemProperty -LiteralPath $_.PSPath -Name $name -Force -ErrorAction SilentlyContinue
                    }
                } catch {}
            }
        }
        Write-Log 'cleared per-interface DHCPv6 lease/DUID values'
    }
}

function Reset-NetworkList {
    $roots = @(
        'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Profiles',
        'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Signatures\Managed',
        'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Signatures\Unmanaged',
        'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkList\Nla\Cache'
    )
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        if ($WhatIf) {
            Write-Log ("WHATIF clear {0}" -f $root)
            continue
        }
        Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                Remove-Item -LiteralPath $_.PSPath -Recurse -Force -ErrorAction Stop
            } catch {
                Write-Log ("NetworkList delete failed {0} | {1}" -f $_.PSChildName, $_.Exception.Message) 'WARN'
            }
        }
        Write-Log ("cleared {0}" -f $root)
    }
}

function Reset-NetworkCardsList {
    $root = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\NetworkCards'
    if (-not (Test-Path $root)) { return }
    $live = @(Get-NetAdapter -ErrorAction SilentlyContinue | ForEach-Object { $_.InterfaceDescription })
    Get-ChildItem $root -ErrorAction SilentlyContinue | ForEach-Object {
        $svc = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).ServiceName
        $desc = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).Description
        if ($desc -and ($live -contains $desc)) { return }
        if ($WhatIf) {
            Write-Log ("WHATIF NetworkCards {0} {1}" -f $_.PSChildName, $desc)
            return
        }
        try {
            Remove-Item -LiteralPath $_.PSPath -Recurse -Force -ErrorAction Stop
            Write-Log ("removed NetworkCards {0} | {1}" -f $_.PSChildName, $desc)
        } catch {
            Write-Log ("NetworkCards delete failed {0} | {1}" -f $_.PSChildName, $_.Exception.Message) 'WARN'
        }
    }
}

function Invoke-NetworkRefresh {
    if ($WhatIf) {
        Write-Log 'WHATIF network refresh (dns/arp/renew)'
        return
    }
    try { ipconfig /flushdns | Out-Null } catch {}
    try { arp -d * 2>$null | Out-Null } catch {}
    try { nbtstat -R 2>$null | Out-Null } catch {}
    try { nbtstat -RR 2>$null | Out-Null } catch {}
    Get-NetAdapter -ErrorAction SilentlyContinue |
        Where-Object { $_.Status -eq 'Up' -and $_.MacAddress -and $_.InterfaceDescription -notmatch 'WAN Miniport|Teredo|isatap|6to4' } |
        ForEach-Object {
            Write-Log ("dhcp renew {0} {1}" -f $_.Name, $_.MacAddress)
            try { ipconfig /renew $_.Name | Out-Null } catch {}
        }
}

function Invoke-HardNet {
    if (-not $HardNet) { return }
    if ($WhatIf) {
        Write-Log 'WHATIF netsh int ip reset + winsock reset'
        return
    }
    Write-Log 'HardNet: netsh int ip reset / winsock reset'
    try { netsh int ip reset | Out-Null } catch {}
    try { netsh int ipv6 reset | Out-Null } catch {}
    try { netsh winsock reset | Out-Null } catch {}
    $script:rebootNeeded = $true
}

function Remove-LeftoverDrivers {
    $pnputil = Get-Command pnputil.exe -ErrorAction SilentlyContinue
    if (-not $pnputil) { return }
    $raw = & pnputil.exe /enum-drivers 2>&1 | Out-String
    $blocks = $raw -split '(?=Published Name:)'
    foreach ($block in $blocks) {
        if ($block -notmatch 'Published Name:\s+(\S+)') { continue }
        $pub = $Matches[1]
        $orig = ''
        if ($block -match 'Original Name:\s+(\S+)') { $orig = $Matches[1] }
        $origBase = [IO.Path]::GetFileNameWithoutExtension($orig).ToLower()
        $hit = $false
        foreach ($name in $leftoverDriverNames) {
            if ($origBase -eq $name -or $origBase -like ($name + '*')) { $hit = $true; break }
        }
        if (-not $hit) { continue }
        if ($WhatIf) {
            Write-Log ("WHATIF delete-driver {0} ({1})" -f $pub, $orig)
            continue
        }
        $out = & pnputil.exe /delete-driver $pub /uninstall /force 2>&1 | Out-String
        if ($LASTEXITCODE -eq 0) {
            Write-Log ("deleted leftover driver {0} ({1})" -f $pub, $orig)
        } else {
            Write-Log ("driver still in use {0} ({1}): {2}" -f $pub, $orig, $out.Trim()) 'WARN'
        }
    }
}

function Write-Summary {
    param([int]$GhostBefore, [int]$Removed, [int]$GhostAfter)
    Write-Log '---- summary ----'
    Write-Log ("ghosts before: {0}" -f $GhostBefore)
    Write-Log ("removed: {0}" -f $Removed)
    Write-Log ("ghosts after: {0}" -f $GhostAfter)
    $net = Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' }
    foreach ($n in $net) {
        $ip = (Get-NetIPAddress -InterfaceIndex $n.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress
        Write-Log ("nic {0} mac={1} ip={2} pnp={3}" -f $n.Name, $n.MacAddress, ($ip -join ','), $n.PnPDeviceID)
    }
    Write-Log ("log {0}" -f $tempLog)
}

Write-Log 'guest hardware refresh start'
Write-Log ("whatif={0} hardnet={1} reboot={2}" -f [bool]$WhatIf, [bool]$HardNet, [bool]$Reboot)

$liveNet = Get-LiveNetInstanceIds
$bootDisk = Get-BootDiskInstanceIds
Write-Log ("protected nic: {0}" -f ($(if ($liveNet) { $liveNet -join ' ; ' } else { '(none)' })))
Write-Log ("protected boot disk: {0}" -f ($(if ($bootDisk) { $bootDisk -join ' ; ' } else { '(none)' })))

$ghosts = Get-GhostDevices
$ghostBefore = $ghosts.Count
Write-Log ("ghost devices: {0}" -f $ghostBefore)
$removed = 0
foreach ($d in $ghosts) {
    if (Test-ProtectedDevice -Device $d -LiveNetIds $liveNet -BootDiskIds $bootDisk) {
        Write-Log ("skip protected {0} | {1}" -f $d.FriendlyName, $d.InstanceId) 'WARN'
        continue
    }
    if (Remove-GhostDevice -InstanceId $d.InstanceId -Name $d.FriendlyName) { $removed++ }
}

Remove-EnumPhantomKeys

$ghostAfter = @(Get-GhostDevices).Count
Write-Log ("PnP ghosts remaining: {0}" -f $ghostAfter)

Remove-StaleTcpipInterfaces
Reset-DhcpDuid
Reset-NetworkList
Reset-NetworkCardsList
Invoke-NetworkRefresh
Invoke-HardNet
Remove-LeftoverDrivers

if ($HardNet -or $Reboot) { $rebootNeeded = $true }
Write-Summary -GhostBefore $ghostBefore -Removed $removed -GhostAfter $ghostAfter
Save-Log

if ($rebootNeeded -and $Reboot -and -not $WhatIf) {
    Write-Log 'rebooting'
    Save-Log
    shutdown.exe /r /t 5 /c guest-refresh
}
exit 0
