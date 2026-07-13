[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("stop", "start", "states")]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [string]$TaskNamesJson
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskNames = @($TaskNamesJson | ConvertFrom-Json)
if ($TaskNames.Count -eq 0) {
    throw "At least one managed task name is required."
}
if (@($TaskNames | Where-Object { -not ($_ -is [string]) -or -not $_.Trim() }).Count -gt 0) {
    throw "Managed task names must be non-empty strings."
}

switch ($Action) {
    "stop" {
        foreach ($Name in $TaskNames) {
            $Task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
            Stop-ScheduledTask -TaskName $Task.TaskName -ErrorAction SilentlyContinue
        }
    }
    "start" {
        foreach ($Name in $TaskNames) {
            $Task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
            Enable-ScheduledTask -TaskName $Task.TaskName -ErrorAction Stop | Out-Null
            Start-ScheduledTask -TaskName $Task.TaskName -ErrorAction Stop
        }
    }
    "states" {
        $States = @{}
        foreach ($Name in $TaskNames) {
            $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
            $States[$Name] = if ($null -eq $Task) { "Missing" } else { [string]$Task.State }
        }
        $States | ConvertTo-Json -Compress
    }
}
