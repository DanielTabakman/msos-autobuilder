[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder-pilot-issue119-6e9434c-20260807T0226Z-c"),
    [string]$SupervisorRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder-pilot-issue119-6e9434c-20260807T0226Z-c-supervisor"),
    [string]$TaskNamespace = "Pilot Issue119",
    [int]$FreshMinutes = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Read-JsonSafe {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { return [pscustomobject]@{ parse_error = $_.Exception.Message; path = $Path } }
}

function Select-YamlScalarSummary {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string[]]$AllowedKeys
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $Allowed = @{}
    foreach ($Key in $AllowedKeys) { $Allowed[$Key] = $true }
    $Out = [ordered]@{}
    foreach ($Line in Get-Content -LiteralPath $Path) {
        if ($Line -match '^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*?)\s*$') {
            $Key = [string]$Matches[1]
            if ($Allowed.ContainsKey($Key)) {
                $Value = [string]$Matches[2]
                if ($Key -match '(?i)token|secret|password|credential|key') { continue }
                $Out[$Key] = $Value
            }
        }
    }
    return [pscustomobject]$Out
}

$Now = [DateTimeOffset]::UtcNow
$StateRoot = Join-Path $SupervisorRoot "state"
$ActivePointerPath = Join-Path $StateRoot "active-release.json"
$Active = Read-JsonSafe -Path $ActivePointerPath

$Release = $null
if ($null -ne $Active -and $Active.PSObject.Properties.Name -contains "release_path") {
    $ReleaseMarker = Join-Path ([string]$Active.release_path) "release.json"
    $Release = Read-JsonSafe -Path $ReleaseMarker
}

$TaskPrefix = if ([string]::IsNullOrWhiteSpace($TaskNamespace)) {
    "MSOS Autobuilder "
} else {
    "MSOS Autobuilder $($TaskNamespace.Trim()) "
}
$Tasks = @()
try {
    $Tasks = @(Get-ScheduledTask -ErrorAction Stop |
        Where-Object { $_.TaskName -like "$TaskPrefix*" } |
        Sort-Object TaskName |
        ForEach-Object {
            [pscustomobject]@{
                name = $_.TaskName
                state = [string]$_.State
                enabled = [bool]$_.Settings.Enabled
            }
        })
}
catch {
    $Tasks = @([pscustomobject]@{ error = $_.Exception.Message })
}

$WitnessRoot = Join-Path $StateRoot "service-witnesses"
$Witnesses = @()
if (Test-Path -LiteralPath $WitnessRoot -PathType Container) {
    $Witnesses = @(Get-ChildItem -LiteralPath $WitnessRoot -Filter "*.json" -File |
        Sort-Object Name |
        ForEach-Object {
            $Payload = Read-JsonSafe -Path $_.FullName
            $Stamp = $null
            foreach ($Name in @("started_at", "recorded_at", "stopped_at", "failed_at")) {
                if ($null -ne $Payload -and $Payload.PSObject.Properties.Name -contains $Name) {
                    try { $Stamp = [DateTimeOffset]::Parse([string]$Payload.$Name) } catch {}
                    if ($null -ne $Stamp) { break }
                }
            }
            $AgeMinutes = if ($null -ne $Stamp) { [math]::Round(($Now - $Stamp).TotalMinutes, 1) } else { $null }
            [pscustomobject]@{
                service = $_.BaseName
                state = if ($null -ne $Payload -and $Payload.PSObject.Properties.Name -contains "state") { [string]$Payload.state } else { $null }
                release_commit = if ($null -ne $Payload -and $Payload.PSObject.Properties.Name -contains "release_commit") { [string]$Payload.release_commit } else { $null }
                timestamp = if ($null -ne $Stamp) { $Stamp.ToString("o") } else { $null }
                age_minutes = $AgeMinutes
                fresh = ($null -ne $AgeMinutes -and $AgeMinutes -le $FreshMinutes)
                path = $_.FullName
            }
        })
}

$RefillPolicy = Read-JsonSafe -Path (Join-Path $HostRoot "state\refill-policy.json")
$ServiceConfig = Select-YamlScalarSummary -Path (Join-Path $HostRoot "service.yaml") -AllowedKeys @(
    "enabled", "repository", "branch", "path", "desired_capacity", "max_queued", "max_awaiting_review",
    "poll_seconds", "host_root", "source_repo"
)
$PublisherConfig = Select-YamlScalarSummary -Path (Join-Path $HostRoot "controlled-publisher.yaml") -AllowedKeys @(
    "draft_pr_publication_enabled", "merge_enabled", "main_write_enabled", "repository", "branch", "results_branch"
)

$CodexCommand = Get-Command codex -ErrorAction SilentlyContinue
if ($null -eq $CodexCommand -and $env:LOCALAPPDATA) {
    $Candidate = Join-Path $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin\codex.exe"
    if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
        $CodexCommand = [pscustomobject]@{ Source = $Candidate }
    }
}
$Codex = [ordered]@{
    found = ($null -ne $CodexCommand)
    executable = if ($null -ne $CodexCommand) { [string]$CodexCommand.Source } else { $null }
    authenticated = $false
    detail = $null
}
if ($null -ne $CodexCommand) {
    try {
        $Output = & ([string]$CodexCommand.Source) login status 2>&1 | Out-String
        $Codex.authenticated = ($LASTEXITCODE -eq 0)
        $Codex.detail = $Output.Trim()
    }
    catch {
        $Codex.detail = $_.Exception.Message
    }
}

$Errors = New-Object System.Collections.Generic.List[string]
if ($null -eq $Active) { [void]$Errors.Add("active_release_missing") }
if ($Tasks.Count -eq 0) { [void]$Errors.Add("managed_tasks_missing") }
$ExpectedServices = @("host", "relay", "gate", "revision", "publisher", "refill")
foreach ($Service in $ExpectedServices) {
    $Match = @($Witnesses | Where-Object { $_.service -eq $Service })
    if ($Match.Count -eq 0) { [void]$Errors.Add("witness_missing:$Service"); continue }
    if (-not $Match[0].fresh) { [void]$Errors.Add("witness_stale:$Service") }
    if ($Match[0].state -ne "running") { [void]$Errors.Add("witness_not_running:$Service") }
    if ($null -ne $Active -and $Active.PSObject.Properties.Name -contains "commit" -and $Match[0].release_commit -ne [string]$Active.commit) {
        [void]$Errors.Add("witness_release_mismatch:$Service")
    }
}
if (-not $Codex.found) { [void]$Errors.Add("codex_missing") }
elseif (-not $Codex.authenticated) { [void]$Errors.Add("codex_not_authenticated") }

$Overall = if ($null -eq $Active -or $Tasks.Count -eq 0) {
    "MISSING"
} elseif ($Errors.Count -eq 0) {
    "HEALTHY"
} elseif (@($Errors | Where-Object { $_ -like "witness_stale:*" }).Count -gt 0) {
    "STALE"
} else {
    "BLOCKED"
}

$Payload = [ordered]@{
    version = 1
    type = "engineering-os-runtime-preflight"
    recorded_at = $Now.ToString("o")
    machine = $env:COMPUTERNAME
    overall = $Overall
    errors = @($Errors)
    roots = [ordered]@{
        host = $HostRoot
        supervisor = $SupervisorRoot
    }
    active_release = $Active
    release_marker = $Release
    tasks = $Tasks
    service_witnesses = $Witnesses
    service_config_summary = $ServiceConfig
    refill_policy = $RefillPolicy
    publisher_config_summary = $PublisherConfig
    codex = [pscustomobject]$Codex
    safety = [ordered]@{
        read_only = $true
        mutations_performed = $false
    }
}
$Payload | ConvertTo-Json -Depth 20
