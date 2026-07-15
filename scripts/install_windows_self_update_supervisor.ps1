[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$SupervisorRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder-supervisor"),
    [string]$RepoUrl = "https://github.com/DanielTabakman/msos-autobuilder.git",
    [string]$Repository = "DanielTabakman/msos-autobuilder",
    [string]$ManifestUrl = "https://raw.githubusercontent.com/DanielTabakman/msos-autobuilder/updates/updates/approved/latest.yaml",
    [string]$EvidenceBranch = "results",
    [int]$UpdatePollMinutes = 15,
    [string]$MachineId = $env:COMPUTERNAME
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
if ($UpdatePollMinutes -lt 5) { throw "UpdatePollMinutes must be at least 5." }
if ($RepoUrl -match '^[A-Za-z][A-Za-z0-9+.-]*://[^/]*@') {
    throw "RepoUrl must not embed credentials."
}
if (-not $EvidenceBranch -or $EvidenceBranch -in @("main", "master")) {
    throw "EvidenceBranch must be a dedicated non-default branch."
}

function Write-Utf8NoBom {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Value)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $Utf8NoBom)
}

function Write-Utf8AtomicJson {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][hashtable]$Value)
    $Parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $Parent | Out-Null
    $Temporary = Join-Path $Parent ("." + [IO.Path]::GetFileName($Path) + "." + [Guid]::NewGuid().ToString("N") + ".tmp")
    Write-Utf8NoBom -Path $Temporary -Value (($Value | ConvertTo-Json -Depth 20) + [Environment]::NewLine)
    Move-Item -Force -Path $Temporary -Destination $Path
}

function Invoke-Checked {
    param([Parameter(Mandatory = $true)][scriptblock]$Command, [Parameter(Mandatory = $true)][string]$Failure)
    & $Command | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "$Failure (exit $LASTEXITCODE)" }
}

function Convert-ToYamlQuoted {
    param([Parameter(Mandatory = $true)][string]$Value)
    return "'" + ($Value.Replace("\", "/") -replace "'", "''") + "'"
}

function New-ManagedTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$ServiceName,
        [Parameter(Mandatory = $true)][string]$RunnerScript,
        [Parameter(Mandatory = $true)][string]$PowerShellExe,
        [Parameter(Mandatory = $true)][string]$UserId
    )
    $Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($Existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    $Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunnerScript`" -ServiceName `"$ServiceName`" -SupervisorRoot `"$SupervisorRoot`" -HostRoot `"$HostRoot`""
    $Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $Arguments
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
    $Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Version-routed MSOS Autobuilder service: $ServiceName" -Force | Out-Null
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Git = (Get-Command git -ErrorAction Stop).Source
$PowerShellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$CurrentCommit = (& $Git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $CurrentCommit -notmatch "^[0-9a-f]{40}$") {
    throw "Could not resolve the exact current Autobuilder commit."
}
$DirtyPaths = @(& $Git -C $RepoRoot status --porcelain --untracked-files=all)
if ($LASTEXITCODE -ne 0) { throw "Could not verify the bootstrap checkout state." }
if ($DirtyPaths.Count -gt 0) {
    throw "The bootstrap checkout must be clean so the stable supervisor matches the exact commit."
}

$BootstrapRoot = Join-Path $SupervisorRoot "bootstrap"
$BootstrapVenv = Join-Path $SupervisorRoot "bootstrap-venv"
$BootstrapPython = Join-Path $BootstrapVenv "Scripts\python.exe"
$VersionsRoot = Join-Path $SupervisorRoot "versions"
$StateRoot = Join-Path $SupervisorRoot "state"
$ReportsRoot = Join-Path $SupervisorRoot "reports"
$NotificationsRoot = Join-Path $SupervisorRoot "notifications"
$LogsRoot = Join-Path $SupervisorRoot "logs"
$TemplatesRoot = Join-Path $BootstrapRoot "config-templates"
$VersionPath = Join-Path $VersionsRoot $CurrentCommit
$ActivePointer = Join-Path $StateRoot "active-release.json"
$BootstrapAttemptId = "bootstrap-$CurrentCommit"
$BootstrapReport = Join-Path $ReportsRoot ($BootstrapAttemptId + ".json")
$BootstrapNotification = Join-Path $NotificationsRoot ($BootstrapAttemptId + ".json")

if (Test-Path $ActivePointer -PathType Leaf) {
    $ExistingActive = Get-Content -Path $ActivePointer -Raw | ConvertFrom-Json
    if ([string]$ExistingActive.commit -ne $CurrentCommit) {
        throw "A different managed release is already active; use the stable supervisor, not the bootstrap installer."
    }
    if (-not (Test-Path (Join-Path $VersionPath "release.json") -PathType Leaf)) {
        throw "The active release directory is incomplete; do not replace it through the bootstrap installer."
    }
}

New-Item -ItemType Directory -Force -Path $BootstrapRoot, $VersionsRoot, $StateRoot, $ReportsRoot, $NotificationsRoot, $LogsRoot, $TemplatesRoot | Out-Null

$SourcePython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $SourcePython -PathType Leaf)) {
    $PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $PythonCommand) { throw "Python is required to install the stable supervisor." }
    $SourcePython = $PythonCommand.Source
}
if (-not (Test-Path $BootstrapPython -PathType Leaf)) {
    Invoke-Checked -Failure "Could not create the stable supervisor environment" -Command { & $SourcePython -m venv $BootstrapVenv }
}
Invoke-Checked -Failure "Could not install stable supervisor dependencies" -Command { & $BootstrapPython -m pip install --disable-pip-version-check "PyYAML>=6.0" }

Copy-Item -Force (Join-Path $RepoRoot "src\msos_autobuilder\self_update_supervisor.py") (Join-Path $BootstrapRoot "self_update_supervisor.py")
Copy-Item -Force (Join-Path $RepoRoot "src\msos_autobuilder\self_update_evidence_relay.py") (Join-Path $BootstrapRoot "self_update_evidence_relay.py")
Copy-Item -Force (Join-Path $RepoRoot "scripts\managed_release_health_probe.py") (Join-Path $BootstrapRoot "managed_release_health_probe.py")
Copy-Item -Force (Join-Path $RepoRoot "scripts\windows_self_update_task_control.ps1") (Join-Path $BootstrapRoot "windows_self_update_task_control.ps1")
Copy-Item -Force (Join-Path $RepoRoot "scripts\run_windows_managed_service.ps1") (Join-Path $BootstrapRoot "run_windows_managed_service.ps1")
Copy-Item -Force (Join-Path $RepoRoot "scripts\invoke_windows_self_update.ps1") (Join-Path $BootstrapRoot "invoke_windows_self_update.ps1")
Copy-Item -Force (Join-Path $RepoRoot "scripts\rollback_windows_self_update.ps1") (Join-Path $BootstrapRoot "rollback_windows_self_update.ps1")

if (-not (Test-Path (Join-Path $VersionPath "release.json") -PathType Leaf)) {
    if (Test-Path $VersionPath) { Remove-Item -Recurse -Force $VersionPath }
    try {
        Invoke-Checked -Failure "Could not clone the current exact commit into its version directory" -Command { & $Git -c core.autocrlf=false clone --quiet --no-hardlinks --no-checkout $RepoRoot $VersionPath }
        Invoke-Checked -Failure "Could not check out the current exact commit" -Command { & $Git -C $VersionPath -c core.autocrlf=false checkout --quiet --detach $CurrentCommit }
        $StagedHead = (& $Git -C $VersionPath rev-parse HEAD).Trim()
        if ($StagedHead -ne $CurrentCommit) { throw "Bootstrap version HEAD does not match the exact current commit." }
        $VersionPython = Join-Path $VersionPath ".venv\Scripts\python.exe"
        Invoke-Checked -Failure "Could not create the versioned release environment" -Command { & $SourcePython -m venv (Join-Path $VersionPath ".venv") }
        Invoke-Checked -Failure "Could not install the versioned release" -Command { & $VersionPython -m pip install --disable-pip-version-check -e "$VersionPath[dev]" }
        Invoke-Checked -Failure "Ruff failed for the initial versioned release" -Command { & $VersionPython -m ruff check $VersionPath }
        Invoke-Checked -Failure "Pytest failed for the initial versioned release" -Command { & $VersionPython -m pytest -q $VersionPath }
        Invoke-Checked -Failure "Managed release health probe failed" -Command { & $VersionPython (Join-Path $BootstrapRoot "managed_release_health_probe.py") $VersionPath }
        $ParserFailures = @()
        Get-ChildItem -Path $VersionPath -Recurse -Filter *.ps1 | ForEach-Object {
            $Tokens = $null; $Errors = $null
            [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$Tokens, [ref]$Errors) | Out-Null
            if ($Errors.Count -gt 0) { $ParserFailures += $_.FullName }
        }
        if ($ParserFailures.Count -gt 0) { throw "PowerShell parser checks failed: $($ParserFailures -join ', ')" }
        Write-Utf8NoBom -Path (Join-Path $VersionPath "release.json") -Value ((@{
            version = 1; commit = $CurrentCommit; release_id = "bootstrap-$CurrentCommit"; staged_at = [DateTimeOffset]::UtcNow.ToString("o")
        } | ConvertTo-Json -Depth 10) + [Environment]::NewLine)
    }
    catch {
        if (Test-Path $VersionPath) { Remove-Item -Recurse -Force $VersionPath }
        throw
    }
}

# Preserve current operational plans, but replace release-coupled absolute paths with stable tokens.
$CurrentPythonForward = (Join-Path $RepoRoot ".venv\Scripts\python.exe").Replace("\", "/")
$RepoRootForward = $RepoRoot.Replace("\", "/")
foreach ($TemplateName in @("candidate-gate.yaml", "controlled-publisher.yaml")) {
    $SourceConfig = Join-Path $HostRoot $TemplateName
    if (-not (Test-Path $SourceConfig -PathType Leaf)) { throw "Required managed config not found: $SourceConfig" }
    $Template = (Get-Content -Path $SourceConfig -Raw).Replace($CurrentPythonForward, "{managed_python}").Replace($RepoRootForward, "{managed_release_root}")
    Write-Utf8NoBom -Path (Join-Path $TemplatesRoot $TemplateName) -Value $Template
}

$SupervisorRootYaml = Convert-ToYamlQuoted $SupervisorRoot
$HostRootYaml = Convert-ToYamlQuoted $HostRoot
$RepoUrlYaml = Convert-ToYamlQuoted $RepoUrl
$RepositoryYaml = Convert-ToYamlQuoted $Repository
$EvidenceBranchYaml = Convert-ToYamlQuoted $EvidenceBranch
$MachineIdYaml = Convert-ToYamlQuoted $MachineId
$TaskControlYaml = Convert-ToYamlQuoted (Join-Path $BootstrapRoot "windows_self_update_task_control.ps1")
$ReleaseProbeYaml = Convert-ToYamlQuoted (Join-Path $BootstrapRoot "managed_release_health_probe.py")
$SupervisorYaml = @"
version: 1
supervisor_root: $SupervisorRootYaml
host_root: $HostRootYaml
repo_url: $RepoUrlYaml
repository: $RepositoryYaml
evidence_repo_url: $RepoUrlYaml
evidence_branch: $EvidenceBranchYaml
machine_id: $MachineIdYaml
task_controller_script: $TaskControlYaml
release_probe_script: $ReleaseProbeYaml
health_timeout_seconds: 90
health_poll_seconds: 2
health_stability_seconds: 10
managed_tasks:
  - service: host
    task_name: 'MSOS Autobuilder Host'
  - service: relay
    task_name: 'MSOS Autobuilder Result Relay'
  - service: gate
    task_name: 'MSOS Autobuilder Candidate Gate'
  - service: revision
    task_name: 'MSOS Autobuilder Revision Loop'
  - service: publisher
    task_name: 'MSOS Autobuilder Controlled Publisher'
  - service: refill
    task_name: 'MSOS Autobuilder Capacity-One Refill'
"@
$SupervisorConfigPath = Join-Path $BootstrapRoot "supervisor.yaml"
Write-Utf8NoBom -Path $SupervisorConfigPath -Value $SupervisorYaml

$Services = @{
    version = 1
    services = @{
        host = @{ argv = @("-m", "msos_autobuilder", "host-run", "--service-config", "{host_root}/service.yaml"); log_file = "{host_root}/logs/persistent-host.log" }
        relay = @{ argv = @("-m", "msos_autobuilder.results_relay", "--host-root", "{host_root}", "--repo-url", $RepoUrl, "--branch", $EvidenceBranch, "--machine-id", "{machine_id}", "--poll-seconds", "30"); log_file = "{host_root}/logs/results-relay.log" }
        gate = @{ argv = @("-m", "msos_autobuilder.candidate_gate_revisions", "--config", "{runtime_config}"); config_template = (Join-Path $TemplatesRoot "candidate-gate.yaml"); log_file = "{host_root}/logs/candidate-gate.log" }
        revision = @{ argv = @("-m", "msos_autobuilder.revision_loop", "--config", "{host_root}/revision-loop.yaml"); log_file = "{host_root}/logs/revision-loop.log" }
        publisher = @{ argv = @("-m", "msos_autobuilder.controlled_publisher", "--config", "{runtime_config}"); config_template = (Join-Path $TemplatesRoot "controlled-publisher.yaml"); log_file = "{host_root}/logs/controlled-publisher.log" }
        refill = @{ argv = @("-m", "msos_autobuilder", "refill-run", "--service-config", "{host_root}/service.yaml", "--interval-seconds", "30"); log_file = "{host_root}/logs/capacity-one-refill.log" }
    }
}
Write-Utf8NoBom -Path (Join-Path $BootstrapRoot "managed-services.json") -Value (($Services | ConvertTo-Json -Depth 20) + [Environment]::NewLine)
Write-Utf8AtomicJson -Path $ActivePointer -Value @{ version = 1; commit = $CurrentCommit; release_path = $VersionPath; activated_at = [DateTimeOffset]::UtcNow.ToString("o") }

$Runner = Join-Path $BootstrapRoot "run_windows_managed_service.ps1"
$ManagedTasks = @(
    @{ task = "MSOS Autobuilder Host"; service = "host" },
    @{ task = "MSOS Autobuilder Result Relay"; service = "relay" },
    @{ task = "MSOS Autobuilder Candidate Gate"; service = "gate" },
    @{ task = "MSOS Autobuilder Revision Loop"; service = "revision" },
    @{ task = "MSOS Autobuilder Controlled Publisher"; service = "publisher" },
    @{ task = "MSOS Autobuilder Capacity-One Refill"; service = "refill" }
)
foreach ($Managed in $ManagedTasks) {
    New-ManagedTask -TaskName $Managed.task -ServiceName $Managed.service -RunnerScript $Runner -PowerShellExe $PowerShellExe -UserId $UserId
}

$UpdateTaskName = "MSOS Autobuilder Update Supervisor"
$ExistingUpdateTask = Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue
if ($ExistingUpdateTask) {
    Stop-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $UpdateTaskName -Confirm:$false
}
$UpdateRunner = Join-Path $BootstrapRoot "invoke_windows_self_update.ps1"
$UpdateArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$UpdateRunner`" -SupervisorRoot `"$SupervisorRoot`" -ManifestUrl `"$ManifestUrl`""
$UpdateAction = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $UpdateArguments
$UpdateTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $UserId),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes $UpdatePollMinutes) -RepetitionDuration (New-TimeSpan -Days 3650))
)
$UpdatePrincipal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
$UpdateSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $UpdateTaskName -Action $UpdateAction -Trigger $UpdateTriggers -Principal $UpdatePrincipal -Settings $UpdateSettings -Description "External exact-commit MSOS Autobuilder update supervisor" -Force | Out-Null

$WitnessRoot = Join-Path $StateRoot "service-witnesses"
New-Item -ItemType Directory -Force -Path $WitnessRoot | Out-Null
foreach ($Managed in $ManagedTasks) {
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $WitnessRoot ($Managed.service + ".json"))
}
$BootstrapStartedAt = [DateTimeOffset]::UtcNow
foreach ($Managed in $ManagedTasks) { Start-ScheduledTask -TaskName $Managed.task }

$HealthDeadline = (Get-Date).AddSeconds(90)
$TaskStates = @{}
$ServiceWitnesses = @{}
$Healthy = $false
$StableHealthy = $false
$HealthySince = $null
while ((Get-Date) -lt $HealthDeadline) {
    $Healthy = $true
    $TaskStates = @{}
    $ServiceWitnesses = @{}
    foreach ($Managed in $ManagedTasks) {
        $State = [string](Get-ScheduledTask -TaskName $Managed.task -ErrorAction Stop).State
        $TaskStates[$Managed.task] = $State
        $WitnessPath = Join-Path $WitnessRoot ($Managed.service + ".json")
        if ($State -ne "Running" -or -not (Test-Path $WitnessPath -PathType Leaf)) {
            $Healthy = $false
            continue
        }
        try {
            $Witness = Get-Content -Path $WitnessPath -Raw | ConvertFrom-Json
            $ServiceWitnesses[$Managed.service] = $Witness
            $StartedAt = [DateTimeOffset]::Parse([string]$Witness.started_at)
            if (
                [string]$Witness.state -ne "running" -or
                [string]$Witness.release_commit -ne $CurrentCommit -or
                $StartedAt -lt $BootstrapStartedAt
            ) {
                $Healthy = $false
            }
        }
        catch {
            $Healthy = $false
            $ServiceWitnesses[$Managed.service] = @{ error = $_.Exception.Message }
        }
    }
    if ($Healthy) {
        if ($null -eq $HealthySince) { $HealthySince = Get-Date }
        if (((Get-Date) - $HealthySince).TotalSeconds -ge 10) {
            $StableHealthy = $true
            break
        }
    }
    else {
        $HealthySince = $null
    }
    Start-Sleep -Seconds 2
}
if (-not $StableHealthy) {
    throw "Initial managed release did not remain healthy for the required stability window."
}

Write-Utf8NoBom -Path $BootstrapReport -Value ((@{
    version = 1
    type = "initial-bootstrap"
    attempt_id = $BootstrapAttemptId
    outcome = "success"
    requested_commit = $CurrentCommit
    commit = $CurrentCommit
    version_path = $VersionPath
    stable_supervisor_root = $SupervisorRoot
    task_states = $TaskStates
    service_witnesses = $ServiceWitnesses
    manifest_url = $ManifestUrl
    recorded_at = [DateTimeOffset]::UtcNow.ToString("o")
    note = "Future managed releases cannot replace the executing stable supervisor in the same transaction."
} | ConvertTo-Json -Depth 20) + [Environment]::NewLine)
Write-Utf8NoBom -Path $BootstrapNotification -Value ((@{
    version = 1
    type = "autobuilder-self-update"
    attempt_id = $BootstrapAttemptId
    outcome = "success"
    requested_commit = $CurrentCommit
    report_path = $BootstrapReport
    requires_founder_attention = $false
    recorded_at = [DateTimeOffset]::UtcNow.ToString("o")
} | ConvertTo-Json -Depth 10) + [Environment]::NewLine)

$EvidenceRelayModule = Join-Path $BootstrapRoot "self_update_evidence_relay.py"
& $BootstrapPython $EvidenceRelayModule --config $SupervisorConfigPath | Out-Host
if ($LASTEXITCODE -ne 0) {
    Write-Warning "The supervisor is installed and healthy, but its bootstrap evidence has not reached the results branch yet. The scheduled updater will retry the durable local evidence automatically."
}

Write-Host "Fail-safe Autobuilder self-update supervisor installed." -ForegroundColor Green
Write-Host "Stable supervisor: $SupervisorRoot"
Write-Host "Active exact commit: $CurrentCommit"
Write-Host "Bootstrap report: $BootstrapReport"
Write-Host "Evidence branch: $EvidenceBranch"
Write-Host "One-command rollback: $BootstrapRoot\rollback_windows_self_update.ps1"
Write-Host "The updater task is separate from all five managed Autobuilder tasks."
