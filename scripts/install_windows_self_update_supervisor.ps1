[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$SupervisorRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder-supervisor"),
    [string]$RepoUrl = "https://github.com/DanielTabakman/msos-autobuilder.git",
    [string]$Repository = "DanielTabakman/msos-autobuilder",
    [string]$ManifestUrl = "https://raw.githubusercontent.com/DanielTabakman/msos-autobuilder/updates/updates/approved/latest.yaml",
    [string]$EvidenceBranch = "results",
    [int]$UpdatePollMinutes = 15,
    [string]$MachineId = $env:COMPUTERNAME,
    [string]$TaskNamespace = ""
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

$script:MaxScheduledTaskNameLength = 238
$script:ProductionManagedTaskRoles = @(
    @{ service = "host"; role = "Host" },
    @{ service = "relay"; role = "Result Relay" },
    @{ service = "gate"; role = "Candidate Gate" },
    @{ service = "revision"; role = "Revision Loop" },
    @{ service = "publisher"; role = "Controlled Publisher" },
    @{ service = "refill"; role = "Capacity-One Refill" }
)
$script:UpdateSupervisorRole = "Update Supervisor"
$script:ProtectedProductionTaskNames = @(
    "MSOS Autobuilder Host",
    "MSOS Autobuilder Result Relay",
    "MSOS Autobuilder Candidate Gate",
    "MSOS Autobuilder Revision Loop",
    "MSOS Autobuilder Controlled Publisher",
    "MSOS Autobuilder Capacity-One Refill",
    "MSOS Autobuilder Update Supervisor"
)
$script:ProtectedProductionHostRoot = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".msos-autobuilder"))
$script:ProtectedProductionSupervisorRoot = [System.IO.Path]::GetFullPath((Join-Path $env:USERPROFILE ".msos-autobuilder-supervisor"))

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

function Get-NormalizedTaskNamespace {
    param([AllowNull()][string]$Namespace)
    if ($null -eq $Namespace) { return "" }
    return $Namespace.Trim()
}

function Get-NamespacedTaskName {
    param(
        [Parameter(Mandatory = $true)][string]$Role,
        [string]$Namespace = ""
    )
    if ([string]::IsNullOrWhiteSpace($Namespace)) {
        return "MSOS Autobuilder $Role"
    }
    return "MSOS Autobuilder $($Namespace.Trim()) $Role"
}

function Get-ProductionTaskNames {
    return @($script:ProtectedProductionTaskNames)
}

function Resolve-InstallerTaskNames {
    param([string]$Namespace = "")
    $Normalized = Get-NormalizedTaskNamespace -Namespace $Namespace
    $Managed = New-Object System.Collections.Generic.List[object]
    foreach ($Entry in $script:ProductionManagedTaskRoles) {
        [void]$Managed.Add(@{
            service = $Entry.service
            role = $Entry.role
            task = (Get-NamespacedTaskName -Role $Entry.role -Namespace $Normalized)
        })
    }
    return @{
        namespace = $Normalized
        isolated = -not [string]::IsNullOrWhiteSpace($Normalized)
        managed_tasks = $Managed.ToArray()
        update_task_name = (Get-NamespacedTaskName -Role $script:UpdateSupervisorRole -Namespace $Normalized)
    }
}

function Test-RootPathOverlap {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    $LeftFull = [System.IO.Path]::GetFullPath($Left).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $RightFull = [System.IO.Path]::GetFullPath($Right).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    if ($LeftFull.Equals($RightFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    $LeftPrefix = $LeftFull + [System.IO.Path]::DirectorySeparatorChar
    $RightPrefix = $RightFull + [System.IO.Path]::DirectorySeparatorChar
    return (
        $LeftFull.StartsWith($RightPrefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        $RightFull.StartsWith($LeftPrefix, [System.StringComparison]::OrdinalIgnoreCase)
    )
}

function Assert-PathHasNoReparsePoints {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Label
    )
    $Full = [System.IO.Path]::GetFullPath($Path)
    $Root = [System.IO.Path]::GetPathRoot($Full)
    if ([string]::IsNullOrWhiteSpace($Root)) {
        throw "$Label path is malformed: $Path"
    }
    $Current = $Root.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    if ($Root.EndsWith([System.IO.Path]::DirectorySeparatorChar) -or $Root.EndsWith([System.IO.Path]::AltDirectorySeparatorChar)) {
        $Current = $Root
    }
    $Relative = $Full.Substring($Root.Length).TrimStart([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $Parts = @()
    if (-not [string]::IsNullOrWhiteSpace($Relative)) {
        $Parts = $Relative.Split([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    }
    $Probe = $Current
    if (Test-Path -LiteralPath $Probe) {
        $Item = Get-Item -LiteralPath $Probe -Force
        if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label path resolves through a reparse point: $Path"
        }
    }
    foreach ($Part in $Parts) {
        if ([string]::IsNullOrWhiteSpace($Part)) { continue }
        $Probe = Join-Path $Probe $Part
        if (-not (Test-Path -LiteralPath $Probe)) { break }
        $Item = Get-Item -LiteralPath $Probe -Force
        if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "$Label path resolves through a reparse point: $Path"
        }
    }
}

function Assert-InstallerTaskNamespaceReady {
    param(
        [Parameter(Mandatory = $true)]$ResolvedNames,
        [Parameter(Mandatory = $true)][string]$HostRootPath,
        [Parameter(Mandatory = $true)][string]$SupervisorRootPath,
        [string]$ProtectedHostRoot = $script:ProtectedProductionHostRoot,
        [string]$ProtectedSupervisorRoot = $script:ProtectedProductionSupervisorRoot
    )
    $Namespace = [string]$ResolvedNames.namespace
    if ($ResolvedNames.isolated) {
        if ($Namespace -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9 ._:-]{0,78}[A-Za-z0-9])?$') {
            throw "TaskNamespace is malformed. Use 1-80 characters of letters, digits, spaces, '.', '_', ':', or '-'."
        }
    }

    $AllNames = New-Object System.Collections.Generic.List[string]
    foreach ($Managed in @($ResolvedNames.managed_tasks)) {
        if ([string]::IsNullOrWhiteSpace([string]$Managed.task)) {
            throw "Managed task name resolved empty for service $($Managed.service)."
        }
        [void]$AllNames.Add([string]$Managed.task)
    }
    if ([string]::IsNullOrWhiteSpace([string]$ResolvedNames.update_task_name)) {
        throw "Update supervisor task name resolved empty."
    }
    [void]$AllNames.Add([string]$ResolvedNames.update_task_name)

    $Seen = @{}
    foreach ($Name in $AllNames) {
        if ($Name.Length -gt $script:MaxScheduledTaskNameLength) {
            throw "Scheduled task name exceeds Windows limit ($script:MaxScheduledTaskNameLength): $Name"
        }
        if ($Name -match '[\u0000-\u001F\\/<>|"?*]') {
            throw "Scheduled task name contains illegal characters: $Name"
        }
        $Key = $Name.ToLowerInvariant()
        if ($Seen.ContainsKey($Key)) {
            throw "Duplicate scheduled task name resolved: $Name"
        }
        $Seen[$Key] = $true
    }

    if ($ResolvedNames.isolated) {
        $ProtectedNames = @(Get-ProductionTaskNames)
        foreach ($Name in $AllNames) {
            foreach ($Protected in $ProtectedNames) {
                if ($Name.Equals($Protected, [System.StringComparison]::OrdinalIgnoreCase)) {
                    throw "Isolated task name collides with protected production task name: $Name"
                }
            }
        }

        $HostFull = [System.IO.Path]::GetFullPath($HostRootPath)
        $SupervisorFull = [System.IO.Path]::GetFullPath($SupervisorRootPath)
        $ProtectedHostFull = [System.IO.Path]::GetFullPath($ProtectedHostRoot)
        $ProtectedSupervisorFull = [System.IO.Path]::GetFullPath($ProtectedSupervisorRoot)

        if (Test-RootPathOverlap -Left $HostFull -Right $SupervisorFull) {
            throw "Isolated HostRoot and SupervisorRoot must not overlap."
        }
        foreach ($Candidate in @(@{ label = "HostRoot"; path = $HostFull }, @{ label = "SupervisorRoot"; path = $SupervisorFull })) {
            if (Test-RootPathOverlap -Left $Candidate.path -Right $ProtectedHostFull) {
                throw "Isolated $($Candidate.label) overlaps protected Issue #50 host root: $ProtectedHostFull"
            }
            if (Test-RootPathOverlap -Left $Candidate.path -Right $ProtectedSupervisorFull) {
                throw "Isolated $($Candidate.label) overlaps protected Issue #50 supervisor root: $ProtectedSupervisorFull"
            }
            Assert-PathHasNoReparsePoints -Path $Candidate.path -Label $Candidate.label
        }
        Assert-PathHasNoReparsePoints -Path $ProtectedHostFull -Label "Protected host root"
        Assert-PathHasNoReparsePoints -Path $ProtectedSupervisorFull -Label "Protected supervisor root"
    }
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

# Resolve and validate all seven task names before any Scheduled Task mutation path can execute.
$ResolvedTaskNames = Resolve-InstallerTaskNames -Namespace $TaskNamespace
Assert-InstallerTaskNamespaceReady -ResolvedNames $ResolvedTaskNames -HostRootPath $HostRoot -SupervisorRootPath $SupervisorRoot
$ManagedTasks = @(
    foreach ($Managed in @($ResolvedTaskNames.managed_tasks)) {
        @{ task = [string]$Managed.task; service = [string]$Managed.service }
    }
)
$UpdateTaskName = [string]$ResolvedTaskNames.update_task_name
$IsolatedTaskNamespace = [bool]$ResolvedTaskNames.isolated

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

# Preserve current operational plans for production installs. Isolated namespaces get fresh
# pilot-owned configs instead of copying historical mutable Issue #50 state.
$CurrentPythonForward = (Join-Path $RepoRoot ".venv\Scripts\python.exe").Replace("\", "/")
$RepoRootForward = $RepoRoot.Replace("\", "/")
$HostRootForward = $HostRoot.Replace("\", "/")
$HostRootYamlForConfig = Convert-ToYamlQuoted $HostRoot
$RepoUrlYamlForConfig = Convert-ToYamlQuoted $RepoUrl
$EvidenceBranchYamlForConfig = Convert-ToYamlQuoted $EvidenceBranch
$MachineIdYamlForConfig = Convert-ToYamlQuoted $MachineId
if ($IsolatedTaskNamespace) {
    New-Item -ItemType Directory -Force -Path (Join-Path $HostRoot "logs"), (Join-Path $HostRoot "state"), (Join-Path $HostRoot "workspaces"), (Join-Path $HostRoot "runtime"), (Join-Path $HostRoot "artifacts") | Out-Null
    $FreshGateYaml = @"
version: 1
publication_enabled: false
host_root: $HostRootYamlForConfig
results_repo_url: $RepoUrlYamlForConfig
results_branch: $EvidenceBranchYamlForConfig
machine_id: $MachineIdYamlForConfig
poll_seconds: 30
plans: {}
"@
    $FreshPublisherYaml = @"
version: 1
draft_pr_publication_enabled: false
merge_enabled: false
main_write_enabled: false
host_root: $HostRootYamlForConfig
evidence_repo_url: $RepoUrlYamlForConfig
results_branch: $EvidenceBranchYamlForConfig
product_repo_url: 'https://github.com/DanielTabakman/Probability-prediction-engine.git'
product_repo_full_name: 'DanielTabakman/Probability-prediction-engine'
product_base_branch: 'main'
machine_id: $MachineIdYamlForConfig
poll_seconds: 30
plans: {}
"@
    $FreshGateTemplate = $FreshGateYaml.Replace($HostRootForward, "{host_root}")
    $FreshPublisherTemplate = $FreshPublisherYaml.Replace($HostRootForward, "{host_root}")
    Write-Utf8NoBom -Path (Join-Path $HostRoot "candidate-gate.yaml") -Value $FreshGateYaml
    Write-Utf8NoBom -Path (Join-Path $HostRoot "controlled-publisher.yaml") -Value $FreshPublisherYaml
    Write-Utf8NoBom -Path (Join-Path $TemplatesRoot "candidate-gate.yaml") -Value $FreshGateTemplate
    Write-Utf8NoBom -Path (Join-Path $TemplatesRoot "controlled-publisher.yaml") -Value $FreshPublisherTemplate
    Write-Utf8AtomicJson -Path (Join-Path $HostRoot "state\refill-policy.json") -Value @{
        version = 1
        enabled = $false
        desired_capacity = 0
        resume_desired_capacity = 1
        status = "PAUSED"
        message = "Isolated pilot refill remains paused until separately authorized."
    }
}
else {
    foreach ($TemplateName in @("candidate-gate.yaml", "controlled-publisher.yaml")) {
        $SourceConfig = Join-Path $HostRoot $TemplateName
        if (-not (Test-Path $SourceConfig -PathType Leaf)) { throw "Required managed config not found: $SourceConfig" }
        $Template = (Get-Content -Path $SourceConfig -Raw).Replace($CurrentPythonForward, "{managed_python}").Replace($RepoRootForward, "{managed_release_root}")
        Write-Utf8NoBom -Path (Join-Path $TemplatesRoot $TemplateName) -Value $Template
    }
}

$SupervisorRootYaml = Convert-ToYamlQuoted $SupervisorRoot
$HostRootYaml = Convert-ToYamlQuoted $HostRoot
$RepoUrlYaml = Convert-ToYamlQuoted $RepoUrl
$RepositoryYaml = Convert-ToYamlQuoted $Repository
$EvidenceBranchYaml = Convert-ToYamlQuoted $EvidenceBranch
$MachineIdYaml = Convert-ToYamlQuoted $MachineId
$TaskControlYaml = Convert-ToYamlQuoted (Join-Path $BootstrapRoot "windows_self_update_task_control.ps1")
$ReleaseProbeYaml = Convert-ToYamlQuoted (Join-Path $BootstrapRoot "managed_release_health_probe.py")
$ManagedTaskYamlLines = foreach ($Managed in $ManagedTasks) {
    $TaskNameYaml = Convert-ToYamlQuoted $Managed.task
    "  - service: $($Managed.service)`n    task_name: $TaskNameYaml"
}
$ManagedTasksYaml = ($ManagedTaskYamlLines -join "`n")
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
$ManagedTasksYaml
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
foreach ($Managed in $ManagedTasks) {
    New-ManagedTask -TaskName $Managed.task -ServiceName $Managed.service -RunnerScript $Runner -PowerShellExe $PowerShellExe -UserId $UserId
}

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
