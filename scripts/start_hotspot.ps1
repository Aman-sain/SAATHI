# SAATHI hotspot bring-up (§24 event topology, pre-built from Phase 7 because
# dev-network churn was blocking alert testing — see STATUS 2026-07-12):
# the LAPTOP hosts Wi-Fi "SAATHI"; its IP is then pinned to 192.168.137.1
# (Windows Mobile Hotspot default), Windows can never hop off its own network,
# and every phone-facing URL becomes permanent.
#
# Ownership note: scripts/** is M1's area — drafted by M3 under D-006's
# lead-approved umbrella, flagged for Aman's review.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime

[Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType = WindowsRuntime] | Out-Null
[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType = WindowsRuntime] | Out-Null

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]

function Await($WinRtTask, $ResultType) {
    $t = $asTaskGeneric.MakeGenericMethod($ResultType).Invoke($null, @($WinRtTask))
    $t.Wait() | Out-Null
    $t.Result
}
function AwaitAction($WinRtAction) {
    $t = $asTaskAction.Invoke($null, @($WinRtAction))
    $t.Wait() | Out-Null
}

$profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if ($null -eq $profile) {
    Write-Host 'no internet profile — using the connected WLAN profile instead'
    $profile = [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles() |
        Where-Object { $_.IsWlanConnectionProfile } | Select-Object -First 1
}
if ($null -eq $profile) { throw 'no usable connection profile — connect Wi-Fi once, then rerun' }
Write-Host "sharing profile : $($profile.ProfileName)"

$tm = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
if ($tm.TetheringOperationalState -ne 'On') {
    $config = New-Object Windows.Networking.NetworkOperators.NetworkOperatorTetheringAccessPointConfiguration
    $config.Ssid = 'SAATHI'
    $config.Passphrase = 'saathi2026'   # WPA2 password; rotate on-site (§18)
    AwaitAction ($tm.ConfigureAccessPointAsync($config))
    $result = Await ($tm.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    if ("$($result.Status)" -ne 'Success') {
        throw "hotspot failed to start: $($result.Status) $($result.AdditionalErrorMessage)"
    }
}
Write-Host 'hotspot ON      : SSID=SAATHI pass=saathi2026'
Write-Host 'phone URLs      : PWA  http://192.168.137.1:8000/app'
Write-Host '                  ntfy http://192.168.137.1:2586/<topic from .env>'
Write-Host 'HUB_LAN_IP must be 192.168.137.1 in .env (hub warns at startup if not)'