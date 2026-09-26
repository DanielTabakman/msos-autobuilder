[CmdletBinding()]
param(
    [ValidateSet("start", "status")]
    [string]$Mode = "status",
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder-pilot-issue119-6e9434c-20260807T0226Z-c"),
    [string]$SupervisorRoot = "",
    [string]$TargetRepository = "DanielTabakman/Probability-prediction-engine",
    [string]$TargetRemoteUrl = "https://github.com/DanielTabakman/Probability-prediction-engine.git"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if (-not $SupervisorRoot) { $SupervisorRoot = "$HostRoot-supervisor" }

function Read-JsonSafe {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { return $null }
}

function Get-WitnessEntries {
    param([string]$Pattern = "engos-phase2-witness-a-*")
    $entries = New-Object System.Collections.Generic.List[object]
    foreach ($state in @("pending", "running")) {
        $root = Join-Path $HostRoot ("queue\" + $state)
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        foreach ($item in Get-ChildItem -LiteralPath $root -Filter ($Pattern + ".yaml") -File -ErrorAction SilentlyContinue) {
            [void]$entries.Add([pscustomobject]@{
                job_id = $item.BaseName
                state = $state
                path = $item.FullName
                last_write_utc = $item.LastWriteTimeUtc.ToString("o")
                report = $null
            })
        }
    }
    foreach ($state in @("completed", "failed")) {
        $root = Join-Path $HostRoot ("queue\" + $state)
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        foreach ($item in Get-ChildItem -LiteralPath $root -Filter $Pattern -Directory -ErrorAction SilentlyContinue) {
            $reportPath = Join-Path $item.FullName "report.json"
            $errorPath = Join-Path $item.FullName "error.json"
            $report = if (Test-Path -LiteralPath $reportPath -PathType Leaf) {
                Read-JsonSafe -Path $reportPath
            } elseif (Test-Path -LiteralPath $errorPath -PathType Leaf) {
                Read-JsonSafe -Path $errorPath
            } else { $null }
            [void]$entries.Add([pscustomobject]@{
                job_id = $item.Name
                state = $state
                path = $item.FullName
                last_write_utc = $item.LastWriteTimeUtc.ToString("o")
                report = $report
            })
        }
    }
    return @($entries | Sort-Object last_write_utc -Descending)
}

function Write-Status {
    $entries = @(Get-WitnessEntries)
    [pscustomobject]@{
        version = 1
        type = "engineering-os-phase2-witness-a"
        recorded_at = [DateTimeOffset]::UtcNow.ToString("o")
        host_root = $HostRoot
        entries = $entries
        latest = if ($entries.Count -gt 0) { $entries[0] } else { $null }
        safety = [pscustomobject]@{
            publication_enabled = $false
            merge_enabled = $false
            refill_policy_mutated = $false
        }
    } | ConvertTo-Json -Depth 30
}

if ($Mode -eq "status") {
    Write-Status
    exit 0
}

$activePath = Join-Path $SupervisorRoot "state\active-release.json"
$active = Read-JsonSafe -Path $activePath
if ($null -eq $active -or -not [string]$active.release_path) {
    throw "Active managed release is missing: $activePath"
}
$releasePath = [string]$active.release_path
$python = Join-Path $releasePath ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Managed release Python is missing: $python"
}

$pending = Join-Path $HostRoot "queue\pending"
$running = Join-Path $HostRoot "queue\running"
New-Item -ItemType Directory -Force -Path $pending | Out-Null
New-Item -ItemType Directory -Force -Path $running | Out-Null
if (@(Get-ChildItem -LiteralPath $pending -Filter "*.yaml" -File -ErrorAction SilentlyContinue).Count -gt 0) {
    throw "Witness start requires an empty pending queue."
}
if (@(Get-ChildItem -LiteralPath $running -Filter "*.yaml" -File -ErrorAction SilentlyContinue).Count -gt 0) {
    throw "Witness start requires zero running jobs."
}

$refill = Read-JsonSafe -Path (Join-Path $HostRoot "state\refill-policy.json")
if ($null -eq $refill) { throw "Refill policy is missing." }
$decision = $refill.last_decision_evidence
if ($null -eq $decision) { throw "Refill decision evidence is missing." }
foreach ($name in @("active_running", "active_queued", "feed_awaiting_import")) {
    if ([int]$decision.$name -ne 0) {
        throw "Witness start requires refill $name = 0."
    }
}
if ([string]$decision.status -ne "UNFILLED") {
    throw "Witness start requires refill status UNFILLED; current=$($decision.status)"
}

$hostConfig = Join-Path $HostRoot "host.yaml"
if (-not (Test-Path -LiteralPath $hostConfig -PathType Leaf)) {
    throw "Host config is missing: $hostConfig"
}
$maxConcurrency = & $python -c "import sys; from msos_autobuilder.codex_shadow import load_codex_host_config; print(load_codex_host_config(sys.argv[1]).max_concurrency)" $hostConfig
if ($LASTEXITCODE -ne 0) { throw "Could not read codex.max_concurrency." }
$maxConcurrency = [int]([string]$maxConcurrency).Trim()
if ($maxConcurrency -lt 2) {
    throw "Witness A requires codex.max_concurrency >= 2; current=$maxConcurrency"
}

$codex = Get-Command codex -ErrorAction SilentlyContinue
if ($null -eq $codex) { throw "Codex CLI is not available on PATH." }
$oldPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$codexOutput = (& ([string]$codex.Source) login status 2>&1 | Out-String).Trim()
$codexExit = $LASTEXITCODE
$ErrorActionPreference = $oldPreference
if ($codexExit -ne 0) { throw "Codex is not authenticated: $codexOutput" }

$remoteLine = (& git ls-remote $TargetRemoteUrl refs/heads/main | Select-Object -First 1)
if ($LASTEXITCODE -ne 0 -or -not $remoteLine) {
    throw "Could not resolve target main from $TargetRemoteUrl"
}
$targetCommit = ([string]$remoteLine -split "\s+")[0].Trim().ToLowerInvariant()
if ($targetCommit -notmatch "^[0-9a-f]{40}$") {
    throw "Resolved target commit is malformed: $targetCommit"
}
$short = $targetCommit.Substring(0, 12)
$jobId = "engos-phase2-witness-a-$short"

$existing = @(Get-WitnessEntries -Pattern $jobId)
if ($existing.Count -gt 0) {
    Write-Status
    exit 0
}

$uiPath = "docs/ENGINEERING_OS/MSOS_UI_SURFACE_INVENTORY_V1.md"
$apiPath = "docs/API/MSOS_CAPABILITY_CATALOG_V1.md"
$now = [DateTimeOffset]::UtcNow.ToString("o")
$payload = [ordered]@{
    version = 1
    job_id = $jobId
    approved = $true
    publication_enabled = $false
    requested_by = "engineering-os-phase2-witness-a"
    submitted_at = $now
    approved_at = $now
    expected_source_head = $targetCommit
    founder_build_next = [ordered]@{
        work_admission = [ordered]@{
            admitted_target = [ordered]@{
                target_repository = $TargetRepository
                target_source_commit = $targetCommit
                target_remote_url = $TargetRemoteUrl
            }
        }
    }
    manifest = [ordered]@{
        version = 1
        publication_enabled = $false
        lanes = @(
            [ordered]@{
                task_id = "engos-ui-surface-inventory-v1"
                lane_id = "engos-ui-surface-inventory-v1"
                chapter_id = "ENGINEERING_OS_UI_SURFACE_INVENTORY_V1"
                branch = "witness/engos-ui-surface-inventory-v1"
                layer = "DOCS"
                allowed_paths = @($uiPath)
                required_capabilities = @("codex", "code", "git-clone")
                preferred_cost_class = "standard"
                allow_changes = $true
                instruction = @"
Inspect the current MSOS web application and create exactly $uiPath.
The document is an evidence-based UI surface inventory for Engineering OS issue PPE #5490.
Include: current user-facing routes/surfaces, primary user journeys, duplicated or conflicting entry points, terminology friction, progressive-disclosure opportunities, and a prioritized list of bounded simplification candidates.
Ground every statement in files that exist in this checkout. Do not redesign financial semantics. Do not modify product code, tests, configuration, or any file except $uiPath.
"@
            },
            [ordered]@{
                task_id = "engos-api-capability-catalog-v1"
                lane_id = "engos-api-capability-catalog-v1"
                chapter_id = "ENGINEERING_OS_API_CAPABILITY_CATALOG_V1"
                branch = "witness/engos-api-capability-catalog-v1"
                layer = "DOCS"
                allowed_paths = @($apiPath)
                required_capabilities = @("codex", "code", "git-clone")
                preferred_cost_class = "standard"
                allow_changes = $true
                instruction = @"
Inspect the current PPE/MSOS repository and create exactly $apiPath.
The document is the first evidence-based API capability inventory for Engineering OS issue PPE #5492.
Classify useful capabilities as already API-ready, wrapper-needed, refactor-needed, or not appropriate for external exposure. Record existing endpoint/spec/help/staging evidence where present, including Options Market Read v1.3. Separate core capability from web/Qatom adapters and propose an ordered API catalog without inventing unavailable functionality.
Do not modify application code, tests, configuration, or any file except $apiPath.
"@
            }
        )
    }
}

$json = $payload | ConvertTo-Json -Depth 30
$temp = Join-Path $pending (".$jobId.$([Guid]::NewGuid().ToString("N")).tmp")
$destination = Join-Path $pending "$jobId.yaml"
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText($temp, $json + [Environment]::NewLine, $utf8)

& $python -c "import sys; from pathlib import Path; from msos_autobuilder.persistent_host import parse_host_job; parse_host_job(Path(sys.argv[1]).read_text(encoding='utf-8')); print('validated')" $temp | Out-Null
if ($LASTEXITCODE -ne 0) {
    Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    throw "Generated witness job failed host validation."
}

if (Test-Path -LiteralPath $destination) {
    Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
    Write-Status
    exit 0
}
Move-Item -LiteralPath $temp -Destination $destination

[pscustomobject]@{
    version = 1
    type = "engineering-os-phase2-witness-a-start"
    status = "QUEUED"
    job_id = $jobId
    target_repository = $TargetRepository
    target_commit = $targetCommit
    codex_max_concurrency = $maxConcurrency
    lanes = @(
        [pscustomobject]@{ task_id = "engos-ui-surface-inventory-v1"; allowed_path = $uiPath },
        [pscustomobject]@{ task_id = "engos-api-capability-catalog-v1"; allowed_path = $apiPath }
    )
    publication_enabled = $false
    merge_enabled = $false
    next = "Run this script again with -Mode status to inspect the host archive."
} | ConvertTo-Json -Depth 12
