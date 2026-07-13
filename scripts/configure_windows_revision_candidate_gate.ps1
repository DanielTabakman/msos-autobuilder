[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$TaskName = "MSOS Autobuilder Candidate Gate",
    [string]$ResultsRepoUrl = "https://github.com/DanielTabakman/msos-autobuilder.git",
    [string]$ResultsBranch = "results",
    [string]$MachineId = $env:COMPUTERNAME,
    [int]$PollSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($ResultsBranch -in @("main", "master")) {
    throw "The candidate gate may not target main or master."
}

function Convert-ToYamlQuoted {
    param([Parameter(Mandatory = $true)][string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function Convert-ToForwardSlash {
    param([Parameter(Mandatory = $true)][string]$Value)
    return $Value.Replace("\", "/")
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Bootstrap = Join-Path $PSScriptRoot "bootstrap_windows_codex_host_auto.ps1"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$HostConfig = Join-Path $HostRoot "host.yaml"
$GateConfig = Join-Path $HostRoot "candidate-gate.yaml"
$LogRoot = Join-Path $HostRoot "logs"
$LogFile = Join-Path $LogRoot "candidate-gate.log"
$RunnerScript = Join-Path $HostRoot "run-candidate-gate.ps1"
$IdentityWitness = Join-Path $RepoRoot "scripts\check_frozen_evaluation_candidate.py"
$SchemaWitness = Join-Path $RepoRoot "scripts\check_frozen_evaluation_schema_compatibility.py"

if (-not (Test-Path $VenvPython)) {
    & $Bootstrap -HostRoot $HostRoot
    if ($LASTEXITCODE -ne 0) { throw "Autobuilder bootstrap failed." }
}
if (-not (Test-Path $HostConfig)) { throw "Codex host config not found at $HostConfig" }
if (-not (Test-Path $IdentityWitness)) { throw "Identity witness not found at $IdentityWitness" }
if (-not (Test-Path $SchemaWitness)) { throw "Schema witness not found at $SchemaWitness" }

$EditableSpec = "$RepoRoot[dev]"
& $VenvPython -m pip install -e $EditableSpec | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Could not install the updated Autobuilder package." }

New-Item -ItemType Directory -Force -Path $HostRoot, $LogRoot | Out-Null

$HostRootYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $HostRoot)
$ResultsRepoYaml = Convert-ToYamlQuoted $ResultsRepoUrl
$ResultsBranchYaml = Convert-ToYamlQuoted $ResultsBranch
$MachineIdYaml = Convert-ToYamlQuoted $MachineId
$PythonYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $VenvPython)
$IdentityYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $IdentityWitness)
$SchemaYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $SchemaWitness)

$GateYaml = @"
version: 1
publication_enabled: false
host_root: $HostRootYaml
results_repo_url: $ResultsRepoYaml
results_branch: $ResultsBranchYaml
machine_id: $MachineIdYaml
poll_seconds: $PollSeconds
plans:
  mcd-boundary-and-frozen-contract-v1:
    checks:
      - name: focused-product-tests
        argv:
          - $PythonYaml
          - -m
          - pytest
          - -q
          - apps/msos-web/tests/test_strategy_lab_explainer_strip.py
          - tests/test_frozen_evaluation_contract.py
          - tests/test_frozen_evaluation_record.py
        cwd: .
        timeout_seconds: 900
      - name: frozen-evaluation-snapshot-identity
        argv:
          - $PythonYaml
          - $IdentityYaml
          - .
        cwd: .
        timeout_seconds: 120
      - name: frozen-evaluation-schema-compatibility
        argv:
          - $PythonYaml
          - $SchemaYaml
          - .
        cwd: .
        timeout_seconds: 120
revision_plans:
  ppe-frozen-evaluation-contract-v1:
    checks:
      - name: focused-ppe-contract-tests
        argv:
          - $PythonYaml
          - -m
          - pytest
          - -q
          - tests/test_frozen_evaluation_record.py
          - tests/test_frozen_evaluation_store.py
          - tests/test_frozen_review_store.py
        cwd: .
        timeout_seconds: 900
      - name: frozen-evaluation-snapshot-identity
        argv:
          - $PythonYaml
          - $IdentityYaml
          - .
        cwd: .
        timeout_seconds: 120
      - name: frozen-evaluation-schema-compatibility
        argv:
          - $PythonYaml
          - $SchemaYaml
          - .
        cwd: .
        timeout_seconds: 120
"@
Set-Content -Path $GateConfig -Value $GateYaml -Encoding UTF8

$RunnerContent = @"
Set-StrictMode -Version Latest
`$ErrorActionPreference = "Continue"
`$env:PYTHONUTF8 = "1"
`$env:GIT_TERMINAL_PROMPT = "0"
`$env:GIT_CONFIG_COUNT = "1"
`$env:GIT_CONFIG_KEY_0 = "core.autocrlf"
`$env:GIT_CONFIG_VALUE_0 = "false"
New-Item -ItemType Directory -Force -Path "$LogRoot" | Out-Null
& "$VenvPython" -m msos_autobuilder.candidate_gate_revisions --config "$GateConfig" *>> "$LogFile"
exit `$LASTEXITCODE
"@
Set-Content -Path $RunnerScript -Value $RunnerContent -Encoding UTF8

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Write-Host "Running the revision-aware candidate gate once in the foreground..." -ForegroundColor Cyan
$PreviousPrompt = $env:GIT_TERMINAL_PROMPT
$env:GIT_TERMINAL_PROMPT = "1"
try {
    & $VenvPython -m msos_autobuilder.candidate_gate_revisions --config $GateConfig --once | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "The foreground candidate gate failed." }
}
finally {
    if ($null -eq $PreviousPrompt) {
        Remove-Item Env:\GIT_TERMINAL_PROMPT -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_TERMINAL_PROMPT = $PreviousPrompt
    }
}

$PowerShellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
$TaskArgument = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunnerScript`""
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $TaskArgument
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Revision-aware disposable MSOS candidate integration gate" `
    -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Revision-aware candidate gate installed and started." -ForegroundColor Green
Write-Host "Task: $TaskName"
Write-Host "Config: $GateConfig"
Write-Host "Log: $LogFile"
Write-Host "Product publication remains disabled."
