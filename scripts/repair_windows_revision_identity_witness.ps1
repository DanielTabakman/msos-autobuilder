[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$MachineId = $env:COMPUTERNAME,
    [string]$JobId = "ppe-frozen-evaluation-contract-v1-revision-1",
    [string]$CandidateTaskName = "MSOS Autobuilder Candidate Gate",
    [string]$RevisionTaskName = "MSOS Autobuilder Revision Loop"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$GateConfig = Join-Path $HostRoot "candidate-gate.yaml"
$LedgerPath = Join-Path $HostRoot "state\candidate-gate-seen.json"
$ResultsCheckout = Join-Path $HostRoot "state\candidate-gate-results-repo"
$GateReportPath = Join-Path $ResultsCheckout "results\$MachineId\$JobId\gate-report.json"

if (-not (Test-Path $VenvPython)) { throw "Autobuilder Python not found at $VenvPython" }
if (-not (Test-Path $GateConfig)) { throw "Candidate-gate config not found at $GateConfig" }
if (-not (Test-Path (Join-Path $ResultsCheckout ".git"))) {
    throw "Candidate-gate results checkout not found at $ResultsCheckout"
}

$TasksToRestart = @()
foreach ($TaskName in @($CandidateTaskName, $RevisionTaskName)) {
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -ne $Task) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $TasksToRestart += $TaskName
    }
}

try {
    & git -C $ResultsCheckout fetch --no-tags origin results | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not refresh the results checkout." }
    & git -C $ResultsCheckout checkout -B results origin/results | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Could not reset the results checkout." }

    if (-not (Test-Path $GateReportPath)) {
        throw "Expected failed gate report not found at $GateReportPath"
    }

    $GateReport = Get-Content -Path $GateReportPath -Raw | ConvertFrom-Json
    $InfrastructureFailure = $false
    foreach ($Check in @($GateReport.checks)) {
        if (
            ([string]$Check.name -eq "frozen-evaluation-snapshot-identity") -and
            ([string]$Check.stderr -like "*ImportError*frozen_evaluation_contract*")
        ) {
            $InfrastructureFailure = $true
            break
        }
    }
    if (-not $InfrastructureFailure) {
        throw "Refusing repair: the existing failure is not the known identity-witness import bug."
    }

    if (-not (Test-Path $LedgerPath)) { throw "Candidate-gate ledger not found at $LedgerPath" }
    $Ledger = Get-Content -Path $LedgerPath -Raw | ConvertFrom-Json
    if ($Ledger.PSObject.Properties.Name -contains $JobId) {
        $Ledger.PSObject.Properties.Remove($JobId)
        $LedgerJson = ($Ledger | ConvertTo-Json -Depth 30) + [Environment]::NewLine
        $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($LedgerPath, $LedgerJson, $Utf8NoBom)
    }

    Write-Host "Re-running the corrected revision candidate witness..." -ForegroundColor Cyan
    & $VenvPython -m msos_autobuilder.candidate_gate_revisions --config $GateConfig --once | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Candidate gate repair run failed." }

    & git -C $ResultsCheckout fetch --no-tags origin results | Out-Null
    & git -C $ResultsCheckout checkout -B results origin/results | Out-Null
    $UpdatedReport = Get-Content -Path $GateReportPath -Raw | ConvertFrom-Json
    if ([string]$UpdatedReport.status -ne "passed") {
        throw "The corrected candidate still did not pass. Review the updated gate report."
    }

    Write-Host "Corrected revision candidate passed." -ForegroundColor Green
    Write-Host "Product publication remains disabled."
}
finally {
    foreach ($TaskName in $TasksToRestart) {
        Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
}
