[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$TaskName = "MSOS Autobuilder Candidate Gate",
    [string]$WitnessJobId = "mcd-boundary-and-frozen-contract-v1"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Installer = Join-Path $PSScriptRoot "install_windows_candidate_gate.ps1"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$GateConfig = Join-Path $HostRoot "candidate-gate.yaml"
$LedgerPath = Join-Path $HostRoot "state\candidate-gate-seen.json"
$RunnerScript = Join-Path $HostRoot "run-candidate-gate.ps1"
$LogRoot = Join-Path $HostRoot "logs"
$LogFile = Join-Path $LogRoot "candidate-gate.log"

& $Installer -HostRoot $HostRoot
if ($LASTEXITCODE -ne 0) {
    throw "Candidate gate installer failed."
}

if (-not (Test-Path $VenvPython)) {
    throw "Autobuilder Python environment not found at $VenvPython"
}
if (-not (Test-Path $GateConfig)) {
    throw "Candidate gate config not found at $GateConfig"
}

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

if (Test-Path $LedgerPath) {
    $Ledger = Get-Content -Path $LedgerPath -Raw | ConvertFrom-Json
    if ($Ledger.PSObject.Properties.Name -contains $WitnessJobId) {
        $Ledger.PSObject.Properties.Remove($WitnessJobId)
        $Json = ($Ledger | ConvertTo-Json -Depth 20) + [Environment]::NewLine
        $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($LedgerPath, $Json, $Utf8NoBom)
        Write-Host "Cleared only the first witness ledger entry for safe reprocessing." -ForegroundColor Yellow
    }
}

Write-Host "Re-running the first candidate with canonical patch bytes..." -ForegroundColor Cyan
& $VenvPython -m msos_autobuilder.candidate_gate_windows --config $GateConfig --once | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "The repaired candidate gate failed."
}

$RunnerContent = @"
Set-StrictMode -Version Latest
`$ErrorActionPreference = "Continue"
`$env:PYTHONUTF8 = "1"
`$env:GIT_TERMINAL_PROMPT = "0"
New-Item -ItemType Directory -Force -Path "$LogRoot" | Out-Null
& "$VenvPython" -m msos_autobuilder.candidate_gate_windows --config "$GateConfig" *>> "$LogFile"
exit `$LASTEXITCODE
"@
Set-Content -Path $RunnerScript -Value $RunnerContent -Encoding UTF8

if ($ExistingTask) {
    Start-ScheduledTask -TaskName $TaskName
}

Write-Host "Windows candidate gate byte-preservation repair installed." -ForegroundColor Green
Write-Host "Product publication remains disabled."
