<#
.SYNOPSIS
    Sets up the READ-ONLY WinRM/JEA endpoint the netbox-windows-dhcp plugin uses instead of
    PowerShell Universal. Run on each Windows DHCP server, elevated, in Windows PowerShell 5.1.

.DESCRIPTION
    1. Installs the NetboxDhcpReader module (Program Files\WindowsPowerShell\Modules).
    2. Writes a JEA role capability that exposes ONLY the Get-NbDhcp* read functions.
    3. Registers JEA session configuration "NetboxDhcpReader":
         - RestrictedRemoteServer (NoLanguage: callers can run exposed commands only)
         - runs as a temporary virtual account that is a member of the local
           "DHCP Users" group only (read-only DHCP access in Windows), not Administrators
         - only -AllowedPrincipal may connect
         - every session is transcribed to C:\ProgramData\NetboxDhcpReader\Transcripts
    4. Optionally creates a WinRM HTTPS listener (5986) and a firewall rule limited to NetBox.

    Nothing in this script changes DHCP configuration. Re-running it updates everything in place.

.PARAMETER AllowedPrincipal
    Account or group allowed to connect, e.g. 'CORP\svc-netbox-dhcp' or 'CORP\NetBox-DHCP-Readers'.

.PARAMETER NetBoxAddress
    IP address(es) of the NetBox server(s). Used to restrict the firewall rule.

.PARAMETER CertificateThumbprint
    Thumbprint of an existing LocalMachine\My certificate (Server Authentication, CN/SAN = this
    server's FQDN) for the HTTPS listener. Preferred: issued by your AD CS.

.PARAMETER CreateSelfSignedCertificate
    Create a self-signed certificate for this server's FQDN instead, and export its public part
    to C:\ProgramData\NetboxDhcpReader\winrm-<host>.cer for NetBox's "CA certificate" field.

.PARAMETER SkipListener
    Do not touch WinRM listeners or the firewall (use when HTTPS WinRM is already managed by GPO).

.EXAMPLE
    .\Install-NetboxDhcpReader.ps1 -AllowedPrincipal 'CORP\svc-netbox-dhcp' -NetBoxAddress 10.10.20.15 -CreateSelfSignedCertificate

.EXAMPLE
    .\Install-NetboxDhcpReader.ps1 -Uninstall
#>
[CmdletBinding(DefaultParameterSetName = 'Install')]
param(
    [Parameter(ParameterSetName = 'Install', Mandatory)] [string]   $AllowedPrincipal,
    [Parameter(ParameterSetName = 'Install')]            [string[]] $NetBoxAddress,
    [Parameter(ParameterSetName = 'Install')]            [string]   $CertificateThumbprint,
    [Parameter(ParameterSetName = 'Install')]            [switch]   $CreateSelfSignedCertificate,
    [Parameter(ParameterSetName = 'Install')]            [switch]   $SkipListener,
    [Parameter(ParameterSetName = 'Uninstall', Mandatory)] [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
$Name        = 'NetboxDhcpReader'
$ModuleRoot  = Join-Path $env:ProgramFiles "WindowsPowerShell\Modules\$Name"
$DataRoot    = Join-Path $env:ProgramData $Name
$Transcripts = Join-Path $DataRoot 'Transcripts'
$FirewallRule = 'NetBox-DHCP-WinRM-HTTPS'
$Source      = Join-Path $PSScriptRoot $Name

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }

# ---------------------------------------------------------------- preflight
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an elevated PowerShell prompt.'
}
if ($PSVersionTable.PSEdition -ne 'Desktop') {
    throw 'Run this script in Windows PowerShell 5.1 (powershell.exe), not PowerShell 7.'
}

if ($Uninstall) {
    Step "Removing JEA session configuration $Name"
    if (Get-PSSessionConfiguration -Name $Name -ErrorAction SilentlyContinue) {
        Unregister-PSSessionConfiguration -Name $Name -Force
    }
    Step 'Removing firewall rule'
    Get-NetFirewallRule -Name $FirewallRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    Step "Removing module $ModuleRoot"
    if (Test-Path $ModuleRoot) { Remove-Item $ModuleRoot -Recurse -Force }
    Write-Host "Done. Transcripts kept in $Transcripts. The HTTPS listener was left in place." -ForegroundColor Green
    return
}

if (-not (Test-Path (Join-Path $Source "$Name.psm1"))) {
    throw "Module source not found at $Source. Run this script from the repository's windows folder."
}
if (-not (Get-Module -ListAvailable -Name DhcpServer)) {
    throw 'The DhcpServer PowerShell module is missing. Run this on a server with the DHCP Server role.'
}
if (-not (Get-LocalGroup -Name 'DHCP Users' -ErrorAction SilentlyContinue)) {
    throw "Local group 'DHCP Users' not found. Is the DHCP Server role installed? (Domain controllers have no local groups and are not supported by this script.)"
}
try {
    $sid = (New-Object Security.Principal.NTAccount($AllowedPrincipal)).Translate([Security.Principal.SecurityIdentifier])
} catch {
    throw "AllowedPrincipal '$AllowedPrincipal' could not be resolved: $($_.Exception.Message)"
}
$fqdn = [System.Net.Dns]::GetHostEntry([System.Net.Dns]::GetHostName()).HostName

# ---------------------------------------------------------------- 1. module
Step "Installing module to $ModuleRoot"
New-Item -ItemType Directory -Path $ModuleRoot -Force | Out-Null
Copy-Item (Join-Path $Source "$Name.psm1") $ModuleRoot -Force
Copy-Item (Join-Path $Source "$Name.psd1") $ModuleRoot -Force
# Only Administrators/SYSTEM may change the module or role (JEA trusts what is on disk).
$acl = New-Object Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
foreach ($who in 'BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM') {
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($who, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
}
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule('BUILTIN\Users', 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
Set-Acl -Path $ModuleRoot -AclObject $acl
$manifest = Test-ModuleManifest (Join-Path $ModuleRoot "$Name.psd1")
$readFunctions = @($manifest.ExportedFunctions.Keys)
if ($readFunctions | Where-Object { $_ -notlike 'Get-NbDhcp*' }) {
    throw "Refusing to continue: the module exports non-read functions: $($readFunctions -join ', ')"
}
Write-Host "    exports: $($readFunctions -join ', ')"

# ---------------------------------------------------------------- 2. role capability
Step 'Writing JEA role capability (read functions only)'
$rcDir = Join-Path $ModuleRoot 'RoleCapabilities'
New-Item -ItemType Directory -Path $rcDir -Force | Out-Null
New-PSRoleCapabilityFile -Path (Join-Path $rcDir "$Name.psrc") `
    -Description 'NetBox: read-only Windows DHCP' `
    -ModulesToImport $Name `
    -VisibleFunctions $readFunctions

# ---------------------------------------------------------------- 3. session configuration
Step "Registering JEA session configuration '$Name'"
New-Item -ItemType Directory -Path $Transcripts -Force | Out-Null
$pssc = Join-Path $DataRoot "$Name.pssc"
New-PSSessionConfigurationFile -Path $pssc `
    -SessionType RestrictedRemoteServer `
    -RunAsVirtualAccount `
    -RunAsVirtualAccountGroups 'DHCP Users' `
    -TranscriptDirectory $Transcripts `
    -RoleDefinitions @{ $AllowedPrincipal = @{ RoleCapabilities = $Name } }
$test = Test-PSSessionConfigurationFile -Path $pssc
if (-not $test) { throw "Generated session configuration $pssc is invalid." }
if (Get-PSSessionConfiguration -Name $Name -ErrorAction SilentlyContinue) {
    Unregister-PSSessionConfiguration -Name $Name -Force -NoServiceRestart
}
Register-PSSessionConfiguration -Name $Name -Path $pssc -Force -NoServiceRestart | Out-Null

# ---------------------------------------------------------------- 4. HTTPS listener + firewall
if (-not $SkipListener) {
    if (-not $CertificateThumbprint -and $CreateSelfSignedCertificate) {
        Step "Creating self-signed certificate for $fqdn"
        $cert = New-SelfSignedCertificate -DnsName $fqdn, $env:COMPUTERNAME -CertStoreLocation Cert:\LocalMachine\My `
            -KeyExportPolicy NonExportable -NotAfter (Get-Date).AddYears(5) -FriendlyName 'NetBox DHCP WinRM'
        $CertificateThumbprint = $cert.Thumbprint
        $cer = Join-Path $DataRoot "winrm-$($env:COMPUTERNAME).cer"
        $pem = "-----BEGIN CERTIFICATE-----`n" +
               [Convert]::ToBase64String($cert.RawData, 'InsertLineBreaks') +
               "`n-----END CERTIFICATE-----`n"
        Set-Content -Path $cer -Value $pem -Encoding Ascii
        Write-Host "    paste the contents of $cer into the server's CA certificate in NetBox"
    }
    $existing = Get-ChildItem WSMan:\localhost\Listener | Where-Object { $_.Keys -contains 'Transport=HTTPS' }
    if ($CertificateThumbprint) {
        $c = Get-Item "Cert:\LocalMachine\My\$CertificateThumbprint" -ErrorAction SilentlyContinue
        if (-not $c) { throw "Certificate $CertificateThumbprint not found in LocalMachine\My." }
        Step 'Configuring WinRM HTTPS listener on 5986'
        if ($existing) { $existing | Remove-Item -Recurse -Force }
        New-Item -Path WSMan:\localhost\Listener -Transport HTTPS -Address * -CertificateThumbPrint $CertificateThumbprint -HostName $fqdn -Force | Out-Null
    } elseif (-not $existing) {
        throw 'No HTTPS listener exists. Pass -CertificateThumbprint or -CreateSelfSignedCertificate (or -SkipListener).'
    } else {
        Write-Host '    existing HTTPS listener kept'
    }
    Step 'Firewall: allow TCP 5986 from NetBox only'
    Get-NetFirewallRule -Name $FirewallRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    $remote = if ($NetBoxAddress) { $NetBoxAddress } else { Write-Warning 'No -NetBoxAddress given: 5986 opened to any address.'; 'Any' }
    New-NetFirewallRule -Name $FirewallRule -DisplayName 'NetBox DHCP (WinRM HTTPS, read-only JEA)' `
        -Direction Inbound -Protocol TCP -LocalPort 5986 -RemoteAddress $remote -Action Allow -Profile Domain, Private | Out-Null
}

Step 'Restarting WinRM'
Restart-Service WinRM

# ---------------------------------------------------------------- 5. self-test (as this admin, through JEA)
Step 'Self-test through the JEA endpoint'
try {
    $s = New-PSSession -ComputerName localhost -ConfigurationName $Name -ErrorAction Stop
    $health = Invoke-Command -Session $s -ScriptBlock { Get-NbDhcpHealth } | ConvertFrom-Json
    $visible = Invoke-Command -Session $s -ScriptBlock { Get-Command } | Select-Object -ExpandProperty Name
    Remove-PSSession $s
    Write-Host "    health: status=$($health.status) body=$($health.body)"
    $writes = @($visible | Where-Object { $_ -match '^(Add|Set|Remove|New|Invoke|Restart|Stop|Start)-' -and $_ -notmatch '^(Get|Measure|Select|Exit|Out)-' })
    if ($writes) { Write-Warning "Unexpected commands visible in the endpoint: $($writes -join ', ')" }
} catch {
    Write-Warning "Self-test skipped or failed: $($_.Exception.Message)"
    Write-Warning "(The self-test needs your admin account to be in '$AllowedPrincipal'. Test from NetBox instead.)"
}

Write-Host ''
Write-Host "Done. JEA endpoint '$Name' on ${fqdn}:5986 (HTTPS), read-only." -ForegroundColor Green
Write-Host "In NetBox: server hostname = $fqdn, port = 5986, HTTPS on."
