# Test double for the Windows DhcpServer module, used only by tests/test_winrm.py.
# Data comes from the JSON file named in $env:NBDHCP_FAKE_DATA. Properties carry the same
# .NET types as the real CIM objects ([ipaddress], [timespan], [datetime]) so the plugin's
# ConvertTo-* helpers run unchanged. "errors" in the data makes a cmdlet throw.

$script:Data = Get-Content -Raw -Path $env:NBDHCP_FAKE_DATA | ConvertFrom-Json

function Test-FakeError([string]$Cmdlet) {
    $e = $script:Data.errors
    if ($e -and $e.PSObject.Properties[$Cmdlet]) { throw [string]$e.$Cmdlet }
}

function New-Scope($s) {
    [pscustomobject]@{
        ScopeId       = [ipaddress]$s.ScopeId
        Name          = $s.Name
        StartRange    = [ipaddress]$s.StartRange
        EndRange      = [ipaddress]$s.EndRange
        SubnetMask    = [ipaddress]$s.SubnetMask
        Description   = $s.Description
        State         = $s.State
        LeaseDuration = [timespan]::FromSeconds($s.LeaseDurationSeconds)
    }
}

function Get-DhcpServerv4Scope {
    [CmdletBinding()] param([string]$ScopeId)
    Test-FakeError 'Get-DhcpServerv4Scope'
    $all = @($script:Data.scopes | ForEach-Object { New-Scope $_ })
    if ($ScopeId) {
        $hit = @($all | Where-Object { $_.ScopeId.ToString() -eq $ScopeId })
        if (-not $hit) { throw "Failed to get scope $ScopeId. The specified DHCP scope does not exist." }
        return $hit[0]
    }
    $all
}

function Get-DhcpServerv4Lease {
    [CmdletBinding()] param([string]$ScopeId)
    Test-FakeError 'Get-DhcpServerv4Lease'
    @($script:Data.leases | Where-Object { $_.ScopeId -eq "$ScopeId" } | ForEach-Object {
        [pscustomobject]@{
            IPAddress       = [ipaddress]$_.IPAddress
            ClientId        = $_.ClientId
            HostName        = $_.HostName
            ScopeId         = [ipaddress]$_.ScopeId
            LeaseExpiryTime = if ($_.LeaseExpiryTime) { [datetime]::Parse($_.LeaseExpiryTime, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal) } else { $null }
            AddressState    = $_.AddressState
        }
    })
}

function Get-DhcpServerv4Reservation {
    [CmdletBinding()] param([string]$ScopeId)
    Test-FakeError 'Get-DhcpServerv4Reservation'
    @($script:Data.reservations | Where-Object { $_.ScopeId -eq "$ScopeId" } | ForEach-Object {
        [pscustomobject]@{
            IPAddress   = [ipaddress]$_.IPAddress
            ClientId    = $_.ClientId
            Name        = $_.Name
            Description = $_.Description
            Type        = $_.Type
            ScopeId     = [ipaddress]$_.ScopeId
        }
    })
}

function Get-DhcpServerv4ExclusionRange {
    [CmdletBinding()] param([string]$ScopeId)
    Test-FakeError 'Get-DhcpServerv4ExclusionRange'
    @($script:Data.exclusions | Where-Object { -not $ScopeId -or $_.ScopeId -eq $ScopeId } | ForEach-Object {
        [pscustomobject]@{
            ScopeId    = [ipaddress]$_.ScopeId
            StartRange = [ipaddress]$_.StartRange
            EndRange   = [ipaddress]$_.EndRange
        }
    })
}

function Get-DhcpServerv4OptionValue {
    [CmdletBinding()] param([string]$ScopeId, [switch]$All)
    Test-FakeError 'Get-DhcpServerv4OptionValue'
    $src = if ($ScopeId) { $script:Data.options.scope.$ScopeId } else { $script:Data.options.server }
    @($src | Where-Object { $_ } | ForEach-Object {
        [pscustomobject]@{
            OptionId    = [int]$_.OptionId
            Name        = $_.Name
            Value       = @($_.Value)
            Type        = $_.Type
            VendorClass = $_.VendorClass
        }
    })
}

function Get-DhcpServerv4Failover {
    [CmdletBinding()] param()
    Test-FakeError 'Get-DhcpServerv4Failover'
    @($script:Data.failover | Where-Object { $_ } | ForEach-Object {
        [pscustomobject]@{
            Name                = $_.Name
            PartnerServer       = $_.PartnerServer
            Mode                = $_.Mode
            ScopeId             = @($_.ScopeId | ForEach-Object { [ipaddress]$_ })
            MaxClientLeadTime   = [timespan]::FromSeconds($_.MaxClientLeadTimeSeconds)
            MaxResponseDelay    = [timespan]::FromSeconds($_.MaxResponseDelaySeconds)
            StateSwitchInterval = $null
            EnableAuth          = [bool]$_.EnableAuth
        }
    })
}

# Write cmdlets exist so that a call would be detected, never executed.
foreach ($w in 'Add-DhcpServerv4Scope', 'Set-DhcpServerv4Scope', 'Remove-DhcpServerv4Scope',
               'Add-DhcpServerv4Reservation', 'Set-DhcpServerv4Reservation', 'Remove-DhcpServerv4Reservation',
               'Add-DhcpServerv4ExclusionRange', 'Remove-DhcpServerv4ExclusionRange',
               'Set-DhcpServerv4OptionValue', 'Remove-DhcpServerv4OptionValue',
               'Add-DhcpServerv4Failover', 'Remove-DhcpServerv4Failover', 'Invoke-DhcpServerv4FailoverReplication') {
    Set-Item -Path "function:script:$w" -Value ([scriptblock]::Create("throw 'WRITE CMDLET CALLED: $w'"))
}

Export-ModuleMember -Function Get-DhcpServerv4*, Add-*, Set-*, Remove-*, Invoke-*
