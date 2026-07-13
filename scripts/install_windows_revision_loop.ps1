[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$TaskName = "MSOS Autobuilder Revision Loop",
    [string]$RepoUrl = "https://github.com/DanielTabakman/msos-autobuilder.git",
    [string]$ResultsBranch = "results",
    [string]$JobsBranch = "jobs",
    [string]$MachineId = $env:COMPUTERNAME,
    [int]$PollSeconds = 30,
    [int]$MaxRevisionDepth = 3
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($ResultsBranch -in @("main", "master", $JobsBranch)) {
    throw "The revision loop requires a dedicated results branch."
}
if ($JobsBranch -in @("main", "master", $ResultsBranch)) {
    throw "The revision loop requires a dedicated jobs branch."
}

function Convert-ToYamlQuoted {
    param([Parameter(Mandatory = $true)][string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function Convert-ToForwardSlash {
    param([Parameter(Mandatory = $true)][string]$Value)
    return $Value.Replace("\", "/")
}

function Invoke-GitChecked {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $Git.Source @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed ($LASTEXITCODE): git $($Arguments -join ' ')"
    }
}

function Prepare-BranchCheckout {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Branch
    )
    if (Test-Path (Join-Path $Path ".git")) {
        Invoke-GitChecked -Arguments @("-C", $Path, "config", "core.autocrlf", "false")
        Invoke-GitChecked -Arguments @("-C", $Path, "fetch", "--no-tags", "origin", $Branch)
        Invoke-GitChecked -Arguments @("-C", $Path, "checkout", "-B", $Branch, "origin/$Branch")
        Invoke-GitChecked -Arguments @("-C", $Path, "reset", "--hard", "origin/$Branch")
        Invoke-GitChecked -Arguments @("-C", $Path, "clean", "-fd")
    }
    else {
        if (Test-Path $Path) {
            Remove-Item -Recurse -Force $Path
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
        Invoke-GitChecked -Arguments @(
            "-c", "core.autocrlf=false", "clone", "--single-branch", "--branch", $Branch,
            "--no-tags", $RepoUrl, $Path
        )
    }
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Bootstrap = Join-Path $PSScriptRoot "bootstrap_windows_codex_host_auto.ps1"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ConfigPath = Join-Path $HostRoot "revision-loop.yaml"
$LogRoot = Join-Path $HostRoot "logs"
$LogFile = Join-Path $LogRoot "revision-loop.log"
$RunnerScript = Join-Path $HostRoot "run-revision-loop.ps1"
$StateRoot = Join-Path $HostRoot "state"
$LedgerPath = Join-Path $StateRoot "revision-loop-seen.json"
$ResultsCheckout = Join-Path $StateRoot "revision-loop-results-repo"
$JobsCheckout = Join-Path $StateRoot "revision-loop-jobs-repo"
$Git = Get-Command git -ErrorAction Stop

if (-not (Test-Path $VenvPython)) {
    & $Bootstrap -HostRoot $HostRoot
    if ($LASTEXITCODE -ne 0) { throw "Autobuilder bootstrap failed." }
}

$EditableSpec = "$RepoRoot[dev]"
& $VenvPython -m pip install -e $EditableSpec | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the updated Autobuilder package."
}

New-Item -ItemType Directory -Force -Path $HostRoot, $LogRoot, $StateRoot | Out-Null

$HostRootYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $HostRoot)
$RepoUrlYaml = Convert-ToYamlQuoted $RepoUrl
$ResultsBranchYaml = Convert-ToYamlQuoted $ResultsBranch
$JobsBranchYaml = Convert-ToYamlQuoted $JobsBranch
$MachineIdYaml = Convert-ToYamlQuoted $MachineId

$ConfigYaml = @"
version: 1
publication_enabled: false
host_root: $HostRootYaml
repo_url: $RepoUrlYaml
results_branch: $ResultsBranchYaml
jobs_branch: $JobsBranchYaml
jobs_path: jobs/approved
machine_id: $MachineIdYaml
poll_seconds: $PollSeconds
max_revision_depth: $MaxRevisionDepth
plans:
  mcd-boundary-and-frozen-contract-v1:
    revision_job_prefix: ppe-frozen-evaluation-contract-v1
    target_task_ids:
      - ppe-frozen-evaluation-contract-v1
    instruction_prefix: >-
      Preserve ppe_frozen_eval_v1, reject mismatched outer/header snapshot IDs,
      exclude owner_email from review-facing headers, and keep the correction bounded.
"@
Set-Content -Path $ConfigPath -Value $ConfigYaml -Encoding UTF8

# Adopt the one manually queued bridge job so installation does not emit a duplicate
# revision-1. Future failed gate reports are generated automatically by the service.
Prepare-BranchCheckout -Path $ResultsCheckout -Branch $ResultsBranch
Prepare-BranchCheckout -Path $JobsCheckout -Branch $JobsBranch
$BridgeRootJob = "mcd-boundary-and-frozen-contract-v1"
$BridgeRevisionJob = "ppe-frozen-evaluation-contract-v1-revision-1"
$GatePath = Join-Path $ResultsCheckout "results\$MachineId\$BridgeRootJob\gate-report.json"
$BridgeJobPath = Join-Path $JobsCheckout "jobs\approved\$BridgeRevisionJob.yaml"
if ((Test-Path $GatePath) -and (Test-Path $BridgeJobPath)) {
    $Ledger = @{}
    if (Test-Path $LedgerPath) {
        $Existing = Get-Content -Path $LedgerPath -Raw | ConvertFrom-Json
        if ($null -ne $Existing) {
            foreach ($Property in $Existing.PSObject.Properties) {
                $Ledger[$Property.Name] = $Property.Value
            }
        }
    }
    $LedgerKey = "$MachineId/$BridgeRootJob"
    if (-not $Ledger.ContainsKey($LedgerKey)) {
        $GateHash = (Get-FileHash -Algorithm SHA256 -Path $GatePath).Hash.ToLowerInvariant()
        $JobsCommit = (& $Git.Source -C $JobsCheckout rev-parse HEAD).Trim()
        $Ledger[$LedgerKey] = @{
            gate_report_sha256 = $GateHash
            revision_job_id = $BridgeRevisionJob
            jobs_commit = $JobsCommit
        }
        $LedgerJson = ($Ledger | ConvertTo-Json -Depth 20) + [Environment]::NewLine
        $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($LedgerPath, $LedgerJson, $Utf8NoBom)
        Write-Host "Adopted the existing revision-1 bridge without duplicating it." -ForegroundColor Yellow
    }
}

$RunnerContent = @"
Set-StrictMode -Version Latest
`$ErrorActionPreference = "Continue"
`$env:PYTHONUTF8 = "1"
`$env:GIT_TERMINAL_PROMPT = "0"
`$env:GIT_CONFIG_COUNT = "1"
`$env:GIT_CONFIG_KEY_0 = "core.autocrlf"
`$env:GIT_CONFIG_VALUE_0 = "false"
New-Item -ItemType Directory -Force -Path "$LogRoot" | Out-Null
& "$VenvPython" -m msos_autobuilder.revision_loop --config "$ConfigPath" *>> "$LogFile"
exit `$LASTEXITCODE
"@
Set-Content -Path $RunnerScript -Value $RunnerContent -Encoding UTF8

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Write-Host "Running the revision loop once in the foreground..." -ForegroundColor Cyan
& $VenvPython -m msos_autobuilder.revision_loop --config $ConfigPath --once | Out-Host
if ($LASTEXITCODE -ne 0) { throw "The foreground revision loop failed." }

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
    -Description "Publication-disabled MSOS gate-failure revision loop" `
    -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Autobuilder revision loop installed and started." -ForegroundColor Green
Write-Host "Task: $TaskName"
Write-Host "Config: $ConfigPath"
Write-Host "Log: $LogFile"
Write-Host "Product publication remains disabled."
