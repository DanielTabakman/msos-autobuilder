[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{40}$")]
    [string]$Commit,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{40}$")]
    [string]$ExpectedOldBootstrapCommit,

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$SupervisorRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder-supervisor"),
    [string]$UpdateTaskName = "MSOS Autobuilder Update Supervisor",
    [string]$BootstrapPython = (Join-Path $SupervisorRoot "bootstrap-venv\Scripts\python.exe")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ManagedTaskNames = @(
    "MSOS Autobuilder Host",
    "MSOS Autobuilder Result Relay",
    "MSOS Autobuilder Candidate Gate",
    "MSOS Autobuilder Revision Loop",
    "MSOS Autobuilder Controlled Publisher",
    "MSOS Autobuilder Capacity-One Refill"
)

$ExistingManagedTaskNames = @(
    "MSOS Autobuilder Host",
    "MSOS Autobuilder Result Relay",
    "MSOS Autobuilder Candidate Gate",
    "MSOS Autobuilder Revision Loop",
    "MSOS Autobuilder Controlled Publisher"
)

$RefillTaskName = "MSOS Autobuilder Capacity-One Refill"
$RefillServiceName = "refill"

$BootstrapFileMap = @(
    @{ source = "src/msos_autobuilder/self_update_supervisor.py"; target = "self_update_supervisor.py" },
    @{ source = "src/msos_autobuilder/self_update_evidence_relay.py"; target = "self_update_evidence_relay.py" },
    @{ source = "scripts/managed_release_health_probe.py"; target = "managed_release_health_probe.py" },
    @{ source = "scripts/windows_self_update_task_control.ps1"; target = "windows_self_update_task_control.ps1" },
    @{ source = "scripts/run_windows_managed_service.ps1"; target = "run_windows_managed_service.ps1" },
    @{ source = "scripts/invoke_windows_self_update.ps1"; target = "invoke_windows_self_update.ps1" },
    @{ source = "scripts/rollback_windows_self_update.ps1"; target = "rollback_windows_self_update.ps1" }
)

function Write-Utf8NoBom {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Value)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $Encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Value, $Encoding)
}

function Write-ImmutableJson {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][hashtable]$Value)
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    if (Test-Path $Path) { throw "Immutable report already exists: $Path" }
    Write-Utf8NoBom -Path $Path -Value (($Value | ConvertTo-Json -Depth 40) + [Environment]::NewLine)
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Command,
        [Parameter(Mandatory = $true)][string]$Name,
        [System.Collections.ArrayList]$Results
    )
    $Started = Get-Date
    $Output = @()
    $ExitCode = 0
    try {
        $Output = @(& $Command 2>&1 | Out-String)
        $ExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
    }
    catch {
        $Output += $_.Exception.Message
        $ExitCode = 1
    }
    $Result = @{
        name = $Name
        exit_code = $ExitCode
        duration_seconds = [Math]::Round(((Get-Date) - $Started).TotalSeconds, 3)
        output = ($Output | Out-String).Trim()
    }
    [void]$Results.Add($Result)
    if ($ExitCode -ne 0) { throw "$Name failed. $($Result.output)" }
}

function Get-FileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return Get-ByteArraySha256 -Bytes ([System.IO.File]::ReadAllBytes($Path))
}

function Get-TextFileEvidence {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path $Path -PathType Leaf)) {
        return @{ exists = $false; path = $Path }
    }
    $Bytes = [System.IO.File]::ReadAllBytes($Path)
    return @{
        exists = $true
        path = $Path
        sha256 = Get-ByteArraySha256 -Bytes $Bytes
        canonical_sha256 = Get-CrlfCanonicalSha256 -Bytes $Bytes
        length = $Bytes.Length
    }
}

function Get-ScheduledTaskEvidence {
    param([Parameter(Mandatory = $true)][string]$TaskName)
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $Task) {
        return @{ exists = $false; task_name = $TaskName }
    }
    $Xml = Export-ScheduledTask -TaskName $TaskName
    $XmlBytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Xml)
    $ActionEvidence = @(
        $Task.Actions | ForEach-Object {
            @{
                execute = [string]$_.Execute
                arguments = [string]$_.Arguments
                working_directory = [string]$_.WorkingDirectory
            }
        }
    )
    return @{
        exists = $true
        task_name = $TaskName
        xml_sha256 = Get-ByteArraySha256 -Bytes $XmlBytes
        xml_canonical_sha256 = Get-CrlfCanonicalSha256 -Bytes $XmlBytes
        state = [string]$Task.State
        task_path = [string]$Task.TaskPath
        actions = $ActionEvidence
        trigger_count = @($Task.Triggers).Count
        user_id = [string]$Task.Principal.UserId
        logon_type = [string]$Task.Principal.LogonType
        run_level = [string]$Task.Principal.RunLevel
        multiple_instances = [string]$Task.Settings.MultipleInstances
        restart_count = [string]$Task.Settings.RestartCount
        restart_interval = [string]$Task.Settings.RestartInterval
        description = [string]$Task.Description
        durable_enabled = ([string]$Task.State -ne "Disabled")
    }
}

function Get-UpdaterEnabledContract {
    param([Parameter(Mandatory = $true)][string]$State)
    if ($State -eq "Disabled") { return "disabled" }
    return "enabled"
}

function Get-ScheduledTaskBackupXml {
    param([Parameter(Mandatory = $true)][string]$BackupXmlPath)
    return (Get-Content -Raw $BackupXmlPath).TrimStart([char]0xFEFF)
}

function Restore-UpdaterTaskXmlAndEnabledContract {
    param(
        [Parameter(Mandatory = $true)][string]$BackupXmlPath,
        [Parameter(Mandatory = $true)][string]$EnabledContract
    )
    $Existing = Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue
    if ($Existing) {
        Stop-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $UpdateTaskName -Confirm:$false
    }
    Register-ScheduledTask -TaskName $UpdateTaskName -Xml (Get-ScheduledTaskBackupXml -BackupXmlPath $BackupXmlPath) -Force | Out-Null
    if ($EnabledContract -eq "disabled") {
        Disable-ScheduledTask -TaskName $UpdateTaskName -ErrorAction Stop | Out-Null
    }
    else {
        Enable-ScheduledTask -TaskName $UpdateTaskName -ErrorAction Stop | Out-Null
    }
}

function Get-ActiveReleaseEvidence {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Evidence = Get-TextFileEvidence -Path $Path
    if (-not $Evidence.exists) { return $Evidence }
    $Active = Get-Content -Path $Path -Raw | ConvertFrom-Json
    $Evidence["commit"] = [string]$Active.commit
    $Evidence["release_path"] = [string]$Active.release_path
    if ($Evidence["commit"] -notmatch "^[0-9a-f]{40}$") {
        throw "active-release.json does not contain a valid exact commit."
    }
    if (-not $Evidence["release_path"]) {
        throw "active-release.json does not contain a release_path."
    }
    if (-not (Test-Path $Evidence["release_path"] -PathType Container)) {
        throw "active-release.json release_path does not exist."
    }
    return $Evidence
}

function Get-ByteArraySha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $Sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        return [System.BitConverter]::ToString($Sha256.ComputeHash($Bytes)).
            Replace("-", "").
            ToLowerInvariant()
    }
    finally {
        $Sha256.Dispose()
    }
}

function Get-CrlfCanonicalSha256 {
    param([Parameter(Mandatory = $true)][byte[]]$Bytes)
    $Canonical = New-Object System.IO.MemoryStream
    try {
        for ($Index = 0; $Index -lt $Bytes.Length; $Index++) {
            if ($Bytes[$Index] -eq 13 -and $Index + 1 -lt $Bytes.Length -and $Bytes[$Index + 1] -eq 10) {
                [void]$Canonical.WriteByte(10)
                $Index++
            }
            else {
                [void]$Canonical.WriteByte($Bytes[$Index])
            }
        }
        return Get-ByteArraySha256 -Bytes $Canonical.ToArray()
    }
    finally {
        $Canonical.Dispose()
    }
}

function Get-GitBlobBytes {
    param(
        [Parameter(Mandatory = $true)][string]$Git,
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$Commit,
        [Parameter(Mandatory = $true)][string]$RepositoryPath
    )
    $ObjectName = "{0}:{1}" -f $Commit, $RepositoryPath
    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $Git
    $StartInfo.Arguments = ('-C "{0}" cat-file blob {1}' -f $RepoRoot.Replace('"', '\"'), $ObjectName)
    $StartInfo.UseShellExecute = $false
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    $Output = New-Object System.IO.MemoryStream
    try {
        if (-not $Process.Start()) { throw "Could not start git to read $ObjectName." }
        $Process.StandardOutput.BaseStream.CopyTo($Output)
        $ErrorOutput = $Process.StandardError.ReadToEnd()
        $Process.WaitForExit()
        if ($Process.ExitCode -ne 0) {
            throw "Could not read exact Git blob $ObjectName. $ErrorOutput"
        }
        return ,$Output.ToArray()
    }
    finally {
        $Output.Dispose()
        $Process.Dispose()
    }
}

function Test-PidRunning {
    param([Parameter(Mandatory = $true)][int]$Pid)
    try {
        Get-Process -Id $Pid -ErrorAction Stop | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Assert-NoActiveUpdateAttempt {
    param([Parameter(Mandatory = $true)][string]$SupervisorRoot)
    $LockPath = Join-Path (Join-Path $SupervisorRoot "state") "update.lock"
    if (-not (Test-Path $LockPath -PathType Leaf)) { return }
    try {
        $Lock = Get-Content -Path $LockPath -Raw | ConvertFrom-Json
        $Pid = [int]$Lock.pid
        if (Test-PidRunning -Pid $Pid) {
            throw "A self-update supervisor attempt is active with PID $Pid."
        }
    }
    catch {
        if ($_.Exception.Message -like "A self-update supervisor attempt is active*") { throw }
        throw "Found an unreadable update lock at $LockPath; refusing bootstrap replacement."
    }
}

function Copy-BootstrapSourceFiles {
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$DestinationRoot,
        [Parameter(Mandatory = $true)][hashtable]$Evidence
    )
    foreach ($Entry in $BootstrapFileMap) {
        $Source = Join-Path $RepoRoot $Entry.source
        $Destination = Join-Path $DestinationRoot $Entry.target
        if (-not (Test-Path $Source -PathType Leaf)) { throw "Required source file missing: $($Entry.source)" }
        Copy-Item -Force -Path $Source -Destination $Destination
        $StagedBytes = [System.IO.File]::ReadAllBytes($Destination)
        $Evidence[$Entry.target]["staged_sha256"] = Get-ByteArraySha256 -Bytes $StagedBytes
        $Evidence[$Entry.target]["staged_canonical_sha256"] = Get-CrlfCanonicalSha256 -Bytes $StagedBytes
        if ($Evidence[$Entry.target]["staged_sha256"] -ne $Evidence[$Entry.target]["new_checkout_sha256"]) {
            throw "Staged bootstrap file $($Entry.target) does not match the reviewed checkout bytes."
        }
    }
}

function Add-StagedServiceConfiguration {
    param(
        [Parameter(Mandatory = $true)][string]$InstalledBootstrap,
        [Parameter(Mandatory = $true)][string]$StagedBootstrap,
        [Parameter(Mandatory = $true)][string]$BootstrapPython,
        [Parameter(Mandatory = $true)][hashtable]$Evidence
    )
    if (-not (Test-Path $BootstrapPython -PathType Leaf)) {
        throw "Stable supervisor Python not found at $BootstrapPython"
    }
    $ProbePath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-service-config-" + [Guid]::NewGuid().ToString("N") + ".py")
    $StdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-service-config-stdout-" + [Guid]::NewGuid().ToString("N") + ".txt")
    $StderrPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-service-config-stderr-" + [Guid]::NewGuid().ToString("N") + ".txt")
    $Probe = @"
import copy
import json
import pathlib
import sys
import traceback

import yaml

installed = pathlib.Path(sys.argv[1])
staged = pathlib.Path(sys.argv[2])
stdout_path = pathlib.Path(sys.argv[3])
stderr_path = pathlib.Path(sys.argv[4])

old_tasks = [
    ("host", "MSOS Autobuilder Host"),
    ("relay", "MSOS Autobuilder Result Relay"),
    ("gate", "MSOS Autobuilder Candidate Gate"),
    ("revision", "MSOS Autobuilder Revision Loop"),
    ("publisher", "MSOS Autobuilder Controlled Publisher"),
]
refill_task = {"service": "refill", "task_name": "MSOS Autobuilder Capacity-One Refill"}
six_tasks = [*old_tasks, (refill_task["service"], refill_task["task_name"])]
refill_service = {
    "argv": [
        "-m",
        "msos_autobuilder",
        "refill-run",
        "--service-config",
        "{host_root}/service.yaml",
        "--interval-seconds",
        "30",
    ],
    "log_file": "{host_root}/logs/capacity-one-refill.log",
}


def load_yaml(path):
    with path.open("r", encoding="utf-8-sig") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain a mapping")
    return value


def load_json(path):
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain a mapping")
    return value


def main(stdout_file):
    supervisor = load_yaml(installed / "supervisor.yaml")
    host_root = supervisor.get("host_root")
    if not isinstance(host_root, str) or not host_root:
        raise RuntimeError("supervisor.yaml host_root must be a non-empty string")
    managed_tasks = supervisor.get("managed_tasks")
    if not isinstance(managed_tasks, list):
        raise RuntimeError("supervisor.yaml managed_tasks must be a list")
    task_pairs = [
        (str(item.get("service")), str(item.get("task_name")))
        for item in managed_tasks
        if isinstance(item, dict)
    ]
    if task_pairs == old_tasks:
        supervisor["managed_tasks"] = [*managed_tasks, refill_task]
        task_shape = "five-to-six"
    elif task_pairs == six_tasks:
        supervisor["managed_tasks"] = managed_tasks
        task_shape = "six-to-six"
    else:
        raise RuntimeError(
            "installed supervisor.yaml must contain exactly the reviewed five tasks "
            "or the accepted six-task repair baseline"
        )

    services = load_json(installed / "managed-services.json")
    service_map = services.get("services")
    if not isinstance(service_map, dict):
        raise RuntimeError("managed-services.json services must be a mapping")
    old_service_names = [name for name, _task_name in old_tasks]
    service_map = copy.deepcopy(service_map)
    if sorted(service_map) == sorted(old_service_names):
        service_map["refill"] = refill_service
        service_shape = "five-to-six"
    elif sorted(service_map) == sorted([*old_service_names, "refill"]):
        service_shape = "six-to-six"
    else:
        raise RuntimeError(
            "installed managed-services.json must contain exactly the reviewed five services "
            "or the accepted six-service repair baseline"
        )
    services["services"] = service_map

    (staged / "supervisor.yaml").write_text(
        yaml.safe_dump(supervisor, sort_keys=False),
        encoding="utf-8",
    )
    (staged / "managed-services.json").write_text(
        json.dumps(services, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    stdout_file.write(json.dumps({
        "host_root": host_root,
        "managed_tasks": supervisor["managed_tasks"],
        "semantic_change": {
            "mode": "preserve-six-service-baseline" if task_shape == "six-to-six" else "add-refill-service",
            "added_service": None if service_shape == "six-to-six" else refill_service,
            "added_task": None if task_shape == "six-to-six" else refill_task,
        },
        "services": sorted(service_map),
    }, sort_keys=True))
    stdout_file.write("\n")


with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
    "w",
    encoding="utf-8",
) as stderr_file:
    try:
        main(stdout_file)
    except BaseException:
        traceback.print_exc(file=stderr_file)
        stderr_file.flush()
        sys.exit(1)
"@
    try {
        Write-Utf8NoBom -Path $ProbePath -Value $Probe
        & $BootstrapPython $ProbePath $InstalledBootstrap $StagedBootstrap $StdoutPath $StderrPath
        $ExitCode = $LASTEXITCODE
        $Stdout = if (Test-Path $StdoutPath) { Get-Content -Raw -Encoding UTF8 $StdoutPath } else { "" }
        $Stderr = if (Test-Path $StderrPath) { Get-Content -Raw -Encoding UTF8 $StderrPath } else { "" }
        if ($ExitCode -ne 0) {
            throw "Staged service configuration generation failed with exit $ExitCode. stdout:`n$Stdout`nstderr:`n$Stderr"
        }
        $Evidence["staged_generation"] = $Stdout | ConvertFrom-Json
        foreach ($Name in @("supervisor.yaml", "managed-services.json")) {
            $Path = Join-Path $StagedBootstrap $Name
            if (-not $Evidence.ContainsKey($Name)) {
                $Evidence[$Name] = @{}
            }
            $Evidence[$Name]["staged"] = Get-TextFileEvidence -Path $Path
        }
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $ProbePath
        Remove-Item -Force -ErrorAction SilentlyContinue $StdoutPath
        Remove-Item -Force -ErrorAction SilentlyContinue $StderrPath
    }
}

function Register-DisabledRefillTask {
    param(
        [Parameter(Mandatory = $true)][string]$SupervisorRoot,
        [Parameter(Mandatory = $true)][string]$HostRoot,
        [string]$BackupXmlPath
    )
    $RunnerScript = Join-Path $SupervisorRoot "bootstrap\run_windows_managed_service.ps1"
    $AuthorityTask = Get-ScheduledTask -TaskName $ExistingManagedTaskNames[0] -ErrorAction Stop
    $AuthorityAction = @($AuthorityTask.Actions)[0]
    if ($null -eq $AuthorityAction -or -not [string]$AuthorityAction.Execute) {
        throw "Managed host Scheduled Task does not expose an executable for refill derivation."
    }
    $PowerShellExe = [string]$AuthorityAction.Execute
    $UserId = [string]$AuthorityTask.Principal.UserId
    if (-not $UserId) {
        $UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    }
    $LogonType = [string]$AuthorityTask.Principal.LogonType
    if (-not $LogonType) { $LogonType = "Interactive" }
    $RunLevel = [string]$AuthorityTask.Principal.RunLevel
    if (-not $RunLevel) { $RunLevel = "Limited" }
    $Touched = $false
    try {
        $Existing = Get-ScheduledTask -TaskName $RefillTaskName -ErrorAction SilentlyContinue
        if ($Existing) {
            Stop-ScheduledTask -TaskName $RefillTaskName -ErrorAction SilentlyContinue
            $Touched = $true
            Unregister-ScheduledTask -TaskName $RefillTaskName -Confirm:$false
        }
        $Arguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunnerScript`" -ServiceName `"$RefillServiceName`" -SupervisorRoot `"$SupervisorRoot`" -HostRoot `"$HostRoot`""
        $Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $Arguments
        $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
        $Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType $LogonType -RunLevel $RunLevel
        $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        $Touched = $true
        Register-ScheduledTask -TaskName $RefillTaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Version-routed MSOS Autobuilder service: $RefillServiceName" -Force | Out-Null
        Disable-ScheduledTask -TaskName $RefillTaskName -ErrorAction Stop | Out-Null
        $Refill = Get-ScheduledTask -TaskName $RefillTaskName -ErrorAction Stop
        if ([string]$Refill.State -ne "Disabled") {
            throw "Refill Scheduled Task postcondition failed: expected Disabled, found $($Refill.State)."
        }
    }
    catch {
        if ($Touched) {
            Restore-RefillTask -BackupXmlPath $BackupXmlPath
        }
        throw
    }
}

function Assert-InstalledScheduledTaskBaseline {
    param([Parameter(Mandatory = $true)][hashtable]$Evidence)
    foreach ($TaskName in $ExistingManagedTaskNames) {
        if (-not $Evidence[$TaskName].exists) {
            throw "Installed Scheduled Task baseline is incomplete: missing $TaskName."
        }
    }
    if ($Evidence[$RefillTaskName].exists) {
        if ([string]$Evidence[$RefillTaskName].state -ne "Disabled") {
            throw "Installed Scheduled Task baseline requires refill to be absent or exactly Disabled."
        }
        return "six-task-disabled-refill"
    }
    return "five-task-refill-absent"
}

function Restore-RefillTask {
    param([string]$BackupXmlPath)
    $Existing = Get-ScheduledTask -TaskName $RefillTaskName -ErrorAction SilentlyContinue
    if ($Existing) {
        Stop-ScheduledTask -TaskName $RefillTaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $RefillTaskName -Confirm:$false
    }
    if ($BackupXmlPath -and (Test-Path $BackupXmlPath -PathType Leaf)) {
        Register-ScheduledTask -TaskName $RefillTaskName -Xml (Get-Content -Raw $BackupXmlPath) -Force | Out-Null
    }
}

function Test-CompleteBootstrapRestoreSource {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path $Path -PathType Container)) { return $false }
    foreach ($Entry in $BootstrapFileMap) {
        if (-not (Test-Path (Join-Path $Path $Entry.target) -PathType Leaf)) { return $false }
    }
    foreach ($Name in @("supervisor.yaml", "managed-services.json")) {
        if (-not (Test-Path (Join-Path $Path $Name) -PathType Leaf)) { return $false }
    }
    return $true
}

function Restore-UpdaterTask {
    param(
        [Parameter(Mandatory = $true)][string]$BackupXmlPath,
        [Parameter(Mandatory = $true)][hashtable]$PreflightEvidence
    )
    $RestoreError = $null
    $PreflightState = [string]$PreflightEvidence.state
    $EnabledContract = Get-UpdaterEnabledContract -State $PreflightState
    $ExpectedFinalState = if ($EnabledContract -eq "disabled") { "Disabled" } else { "Ready" }
    $Report.update_task["restore_enabled_contract"] = $EnabledContract
    $Report.update_task["expected_final_state"] = $ExpectedFinalState
    try {
        Restore-UpdaterTaskXmlAndEnabledContract -BackupXmlPath $BackupXmlPath -EnabledContract $EnabledContract
    }
    catch {
        $RestoreError = $_.Exception.Message
        Restore-UpdaterTaskXmlAndEnabledContract -BackupXmlPath $BackupXmlPath -EnabledContract $EnabledContract
    }
    $Final = Get-ScheduledTaskEvidence -TaskName $UpdateTaskName
    $Report.update_task["final_state"] = [string]$Final.state
    $Report.update_task["final_xml_sha256"] = [string]$Final.xml_sha256
    $Report.update_task["final_enabled_contract"] = Get-UpdaterEnabledContract -State ([string]$Final.state)
    if (-not $Final.exists) {
        throw "Updater Scheduled Task restoration failed: task is missing."
    }
    if ([string]$Final.xml_sha256 -ne [string]$PreflightEvidence.xml_sha256) {
        throw "Updater Scheduled Task restoration failed: final XML does not match preflight."
    }
    if ([string]$Final.state -ne $ExpectedFinalState) {
        throw "Updater Scheduled Task restoration failed: final state $($Final.state) does not match restored $EnabledContract contract $ExpectedFinalState from preflight $PreflightState."
    }
    $Report.update_task["restored"] = $true
    if ($RestoreError) {
        $Report.update_task["normal_restore_error"] = $RestoreError
        $Report.update_task["reregistered_from_preflight_xml"] = $true
        throw "Updater Scheduled Task normal restore failed and was transactionally restored from preflight XML: $RestoreError"
    }
}

function ConvertTo-ComparablePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    return $Path.TrimEnd("\", "/").Replace("\", "/").ToLowerInvariant()
}

function Restore-HandoffState {
    param([Parameter(Mandatory = $true)][string]$Reason)
    $BootstrapRestored = $false
    if ($null -ne $ActivationBackup -and (Test-CompleteBootstrapRestoreSource -Path $ActivationBackup)) {
        if (Test-Path $BootstrapRoot) {
            Remove-Item -Recurse -Force -ErrorAction Stop $BootstrapRoot
        }
        Move-Item -Path $ActivationBackup -Destination $BootstrapRoot
        $Report.rollback = @{ performed = $true; restored_from = $ActivationBackup; reason = $Reason; refill_task_restored = $false }
        $script:ActivationBackup = $null
        $BootstrapRestored = $true
    }
    elseif (Test-CompleteBootstrapRestoreSource -Path $RollbackBootstrap) {
        if (Test-Path $BootstrapRoot) {
            Remove-Item -Recurse -Force -ErrorAction Stop $BootstrapRoot
        }
        Copy-Item -Recurse -Force -Path $RollbackBootstrap -Destination $BootstrapRoot
        $Report.rollback = @{ performed = $true; restored_from = $RollbackBootstrap; reason = $Reason; refill_task_restored = $false }
        $BootstrapRestored = $true
    }
    elseif ($null -ne $ActivationBackup -and (Test-Path $ActivationBackup)) {
        throw "Refusing to remove BootstrapRoot without a complete validated rollback bootstrap source."
    }
    if (-not $BootstrapRestored -and -not $Report.rollback.ContainsKey("refill_task_restored")) {
        $Report.rollback["refill_task_restored"] = $false
    }
    if ($RefillTaskTouched -and -not $Report.rollback["refill_task_restored"]) {
        Restore-RefillTask -BackupXmlPath $RefillTaskBackupXml
        $Report.rollback["refill_task_restored"] = $true
        $Report.scheduled_tasks["rollback_refill"] = Get-ScheduledTaskEvidence -TaskName $RefillTaskName
    }
}

function Test-ReportPathWritable {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (Test-Path $Path) { throw "Immutable report already exists: $Path" }
    $Parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $Parent | Out-Null
    $Probe = Join-Path $Parent (".report-write-probe-" + [Guid]::NewGuid().ToString("N") + ".tmp")
    try {
        Write-Utf8NoBom -Path $Probe -Value "probe"
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $Probe
    }
}

function Get-BootstrapHashEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$RepoRoot,
        [Parameter(Mandatory = $true)][string]$BootstrapRoot,
        [Parameter(Mandatory = $true)][string]$OldCommit,
        [Parameter(Mandatory = $true)][string]$NewCommit,
        [Parameter(Mandatory = $true)][hashtable]$Evidence
    )
    $Git = (Get-Command git -ErrorAction Stop).Source
    foreach ($Entry in $BootstrapFileMap) {
        $FileEvidence = @{ source = $Entry.source; canonicalization = "CRLF-to-LF byte pairs only" }
        $Evidence[$Entry.target] = $FileEvidence
        $OldBlobBytes = Get-GitBlobBytes -Git $Git -RepoRoot $RepoRoot -Commit $OldCommit -RepositoryPath $Entry.source
        $NewBlobBytes = Get-GitBlobBytes -Git $Git -RepoRoot $RepoRoot -Commit $NewCommit -RepositoryPath $Entry.source
        $FileEvidence["expected_old_commit_sha256"] = Get-ByteArraySha256 -Bytes $OldBlobBytes
        $FileEvidence["expected_old_commit_canonical_sha256"] = Get-CrlfCanonicalSha256 -Bytes $OldBlobBytes
        $FileEvidence["new_commit_sha256"] = Get-ByteArraySha256 -Bytes $NewBlobBytes
        $FileEvidence["new_commit_canonical_sha256"] = Get-CrlfCanonicalSha256 -Bytes $NewBlobBytes

        $NewSource = Join-Path $RepoRoot $Entry.source
        if (-not (Test-Path $NewSource -PathType Leaf)) {
            throw "Required reviewed source file missing: $($Entry.source)"
        }
        $NewCheckoutBytes = [System.IO.File]::ReadAllBytes($NewSource)
        $FileEvidence["new_checkout_sha256"] = Get-ByteArraySha256 -Bytes $NewCheckoutBytes
        $FileEvidence["new_checkout_canonical_sha256"] = Get-CrlfCanonicalSha256 -Bytes $NewCheckoutBytes
        if ($FileEvidence["new_checkout_canonical_sha256"] -ne $FileEvidence["new_commit_canonical_sha256"]) {
            throw "Reviewed checkout file $($Entry.source) does not match exact Git blob content for $NewCommit."
        }

        $Installed = Join-Path $BootstrapRoot $Entry.target
        if (-not (Test-Path $Installed -PathType Leaf)) {
            throw "Installed bootstrap file missing: $($Entry.target)"
        }
        $InstalledBytes = [System.IO.File]::ReadAllBytes($Installed)
        $FileEvidence["installed_sha256"] = Get-ByteArraySha256 -Bytes $InstalledBytes
        $FileEvidence["installed_canonical_sha256"] = Get-CrlfCanonicalSha256 -Bytes $InstalledBytes
        if ($FileEvidence["installed_canonical_sha256"] -ne $FileEvidence["expected_old_commit_canonical_sha256"]) {
            throw "Installed bootstrap file $($Entry.target) does not match expected old commit $OldCommit."
        }
    }
}

function Confirm-ActivatedBootstrapHashes {
    param(
        [Parameter(Mandatory = $true)][string]$BootstrapRoot,
        [Parameter(Mandatory = $true)][hashtable]$Evidence
    )
    foreach ($Entry in $BootstrapFileMap) {
        $Live = Join-Path $BootstrapRoot $Entry.target
        if (-not (Test-Path $Live -PathType Leaf)) {
            throw "Activated bootstrap file missing: $($Entry.target)"
        }
        $ActivatedBytes = [System.IO.File]::ReadAllBytes($Live)
        $Evidence[$Entry.target]["activated_sha256"] = Get-ByteArraySha256 -Bytes $ActivatedBytes
        $Evidence[$Entry.target]["activated_canonical_sha256"] = Get-CrlfCanonicalSha256 -Bytes $ActivatedBytes
        if ($Evidence[$Entry.target]["activated_sha256"] -ne $Evidence[$Entry.target]["staged_sha256"]) {
            throw "Activated bootstrap file $($Entry.target) does not match staged reviewed checkout bytes."
        }
    }
}

function Test-PowerShellScriptsParse {
    param([Parameter(Mandatory = $true)][string]$BootstrapRoot)
    $Failures = @()
    foreach ($Script in Get-ChildItem -Path $BootstrapRoot -Filter *.ps1 -File) {
        $Tokens = $null
        $Errors = $null
        [System.Management.Automation.Language.Parser]::ParseFile($Script.FullName, [ref]$Tokens, [ref]$Errors) | Out-Null
        if ($Errors.Count -gt 0) { $Failures += $Script.FullName }
    }
    if ($Failures.Count -gt 0) {
        throw "PowerShell parser checks failed: $($Failures -join ', ')"
    }
}

function Test-StagedTaskTransport {
    param(
        [Parameter(Mandatory = $true)][string]$BootstrapRoot,
        [Parameter(Mandatory = $true)][string]$BootstrapPython
    )
    if (-not (Test-Path $BootstrapPython -PathType Leaf)) {
        throw "Stable supervisor Python not found at $BootstrapPython"
    }
    $TaskNamesPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-task-names-" + [Guid]::NewGuid().ToString("N") + ".json")
    $ProbePath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-task-transport-" + [Guid]::NewGuid().ToString("N") + ".py")
    $StdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-task-transport-stdout-" + [Guid]::NewGuid().ToString("N") + ".txt")
    $StderrPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-task-transport-stderr-" + [Guid]::NewGuid().ToString("N") + ".txt")
    $Probe = @"
import importlib.util
import json
import pathlib
import sys
import traceback

bootstrap = pathlib.Path(sys.argv[1])
task_names_path = pathlib.Path(sys.argv[2])
stdout_path = pathlib.Path(sys.argv[3])
stderr_path = pathlib.Path(sys.argv[4])


def main(stdout_file):
    task_names = json.loads(task_names_path.read_text(encoding="utf-8-sig"))
    module_path = bootstrap / "self_update_supervisor.py"
    spec = importlib.util.spec_from_file_location("staged_self_update_supervisor", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    module_name = spec.name
    previous = sys.modules.get(module_name)
    had_previous = module_name in sys.modules
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if had_previous:
            sys.modules[module_name] = previous
        else:
            sys.modules.pop(module_name, None)
    controller = module.PowerShellTaskController(
        bootstrap / "windows_self_update_task_control.ps1"
    )
    states = controller.states(task_names)
    stdout_file.write(json.dumps(states, sort_keys=True))
    stdout_file.write("\n")
    stdout_file.flush()


with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
    "w",
    encoding="utf-8",
) as stderr_file:
    try:
        main(stdout_file)
    except BaseException:
        traceback.print_exc(file=stderr_file)
        stderr_file.flush()
        sys.exit(1)
"@
    try {
        Write-Utf8NoBom -Path $TaskNamesPath -Value (($ManagedTaskNames | ConvertTo-Json -Compress) + [Environment]::NewLine)
        Write-Utf8NoBom -Path $ProbePath -Value $Probe
        & $BootstrapPython `
            $ProbePath `
            $BootstrapRoot `
            $TaskNamesPath `
            $StdoutPath `
            $StderrPath
        $ExitCode = $LASTEXITCODE
        $Stdout = if (Test-Path $StdoutPath) { Get-Content -Raw -Encoding UTF8 $StdoutPath } else { "" }
        $Stderr = if (Test-Path $StderrPath) { Get-Content -Raw -Encoding UTF8 $StderrPath } else { "" }
        if ($ExitCode -ne 0) {
            throw "Staged Python to PowerShell task-name transport failed with exit $ExitCode. stdout:`n$Stdout`nstderr:`n$Stderr"
        }
        $States = $Stdout | ConvertFrom-Json
        foreach ($TaskName in $ManagedTaskNames) {
            if (-not ($States.PSObject.Properties.Name -contains $TaskName)) {
                throw "Staged task transport omitted task name: $TaskName"
            }
            if ([string]$States.$TaskName -eq "Missing") {
                throw "Scheduled task is missing during staged validation: $TaskName"
            }
        }
        return $States
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $TaskNamesPath
        Remove-Item -Force -ErrorAction SilentlyContinue $ProbePath
        Remove-Item -Force -ErrorAction SilentlyContinue $StdoutPath
        Remove-Item -Force -ErrorAction SilentlyContinue $StderrPath
    }
}

function Test-ActivatedLegacyRestartWitness {
    param(
        [Parameter(Mandatory = $true)][string]$BootstrapRoot,
        [Parameter(Mandatory = $true)][string]$BootstrapPython
    )
    if (-not (Test-Path $BootstrapPython -PathType Leaf)) {
        throw "Stable supervisor Python not found at $BootstrapPython"
    }
    $ProbePath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-legacy-restart-" + [Guid]::NewGuid().ToString("N") + ".py")
    $StdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-legacy-restart-stdout-" + [Guid]::NewGuid().ToString("N") + ".txt")
    $StderrPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-legacy-restart-stderr-" + [Guid]::NewGuid().ToString("N") + ".txt")
    $Probe = @"
import datetime
import importlib.util
import json
import pathlib
import sys
import traceback

bootstrap = pathlib.Path(sys.argv[1])
stdout_path = pathlib.Path(sys.argv[2])
stderr_path = pathlib.Path(sys.argv[3])


def load_module():
    module_path = bootstrap / "self_update_supervisor.py"
    spec = importlib.util.spec_from_file_location("activated_self_update_supervisor", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    module_name = spec.name
    previous = sys.modules.get(module_name)
    had_previous = module_name in sys.modules
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if had_previous:
            sys.modules[module_name] = previous
        else:
            sys.modules.pop(module_name, None)
    return module


def main(stdout_file):
    module = load_module()
    config = module.load_supervisor_config(bootstrap / "supervisor.yaml")
    active = module._read_active_pointer(config)
    release_path = pathlib.Path(str(active["release_path"]))
    selected, disabled = module._release_managed_tasks(config, release_path)
    controller = module.PowerShellTaskController(config.task_controller_script)
    health = module.FileHealthVerifier(config, controller)
    selected_names = [task.task_name for task in selected]
    disabled_names = [task.task_name for task in disabled]
    started = datetime.datetime.now(datetime.UTC)
    controller.stop(selected_names)
    controller.start(selected_names)
    if disabled_names:
        controller.disable(disabled_names)
    evidence = health.wait_for(str(active["commit"]), started, selected, disabled)
    stdout_file.write(json.dumps({
        "active_commit": str(active["commit"]),
        "selected_services": [task.service for task in selected],
        "disabled_services": [task.service for task in disabled],
        "health": evidence,
    }, sort_keys=True))
    stdout_file.write("\n")
    stdout_file.flush()


with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
    "w",
    encoding="utf-8",
) as stderr_file:
    try:
        main(stdout_file)
    except BaseException:
        traceback.print_exc(file=stderr_file)
        stderr_file.flush()
        sys.exit(1)
"@
    try {
        Write-Utf8NoBom -Path $ProbePath -Value $Probe
        & $BootstrapPython `
            $ProbePath `
            $BootstrapRoot `
            $StdoutPath `
            $StderrPath
        $ExitCode = $LASTEXITCODE
        $Stdout = if (Test-Path $StdoutPath) { Get-Content -Raw -Encoding UTF8 $StdoutPath } else { "" }
        $Stderr = if (Test-Path $StderrPath) { Get-Content -Raw -Encoding UTF8 $StderrPath } else { "" }
        if ($ExitCode -ne 0) {
            throw "Activated legacy restart witness failed with exit $ExitCode. stdout:`n$Stdout`nstderr:`n$Stderr"
        }
        return ($Stdout | ConvertFrom-Json)
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $ProbePath
        Remove-Item -Force -ErrorAction SilentlyContinue $StdoutPath
        Remove-Item -Force -ErrorAction SilentlyContinue $StderrPath
    }
}

function Test-RestoredLegacyRecoveryWitness {
    param([Parameter(Mandatory = $true)][string]$SupervisorRoot)
    $StateRoot = Join-Path $SupervisorRoot "state"
    $ActivePointerPath = Join-Path $StateRoot "active-release.json"
    $WitnessRoot = Join-Path $StateRoot "service-witnesses"
    if (-not (Test-Path $ActivePointerPath -PathType Leaf)) {
        throw "Active release pointer not found during rollback service recovery."
    }
    $Active = Get-Content -Path $ActivePointerPath -Raw | ConvertFrom-Json
    $ReleaseCommit = [string]$Active.commit
    $ReleasePath = [string]$Active.release_path
    if ($ReleaseCommit -notmatch "^[0-9a-f]{40}$" -or -not (Test-Path $ReleasePath -PathType Container)) {
        throw "Active release pointer is malformed during rollback service recovery."
    }
    $Selected = @(
        @{ service = "host"; task_name = "MSOS Autobuilder Host" },
        @{ service = "relay"; task_name = "MSOS Autobuilder Result Relay" },
        @{ service = "gate"; task_name = "MSOS Autobuilder Candidate Gate" },
        @{ service = "revision"; task_name = "MSOS Autobuilder Revision Loop" },
        @{ service = "publisher"; task_name = "MSOS Autobuilder Controlled Publisher" }
    )
    $Disabled = @()
    $RefillModule = Join-Path (Join-Path (Join-Path $ReleasePath "src") "msos_autobuilder") "refill_controller.py"
    if (Test-Path $RefillModule -PathType Leaf) {
        $Selected += @{ service = "refill"; task_name = $RefillTaskName }
    }
    else {
        $Disabled += @{ service = "refill"; task_name = $RefillTaskName }
    }
    $Started = [DateTimeOffset]::UtcNow
    foreach ($Task in $Selected) {
        Stop-ScheduledTask -TaskName $Task.task_name -ErrorAction SilentlyContinue
    }
    foreach ($Task in $Selected) {
        Enable-ScheduledTask -TaskName $Task.task_name -ErrorAction Stop | Out-Null
        Start-ScheduledTask -TaskName $Task.task_name -ErrorAction Stop
    }
    foreach ($Task in $Disabled) {
        Stop-ScheduledTask -TaskName $Task.task_name -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $Task.task_name -ErrorAction Stop | Out-Null
    }

    $Deadline = (Get-Date).AddSeconds(30)
    $LastDetail = @{}
    while ((Get-Date) -lt $Deadline) {
        $Healthy = $true
        $TaskStates = @{}
        $Witnesses = @{}
        $DisabledStates = @{}
        foreach ($Task in $Selected) {
            $State = [string](Get-ScheduledTask -TaskName $Task.task_name -ErrorAction Stop).State
            $TaskStates[$Task.task_name] = $State
            if ($State -ne "Running") { $Healthy = $false }
            $WitnessPath = Join-Path $WitnessRoot ($Task.service + ".json")
            if (-not (Test-Path $WitnessPath -PathType Leaf)) {
                $Healthy = $false
                continue
            }
            try {
                $Witness = Get-Content -Path $WitnessPath -Raw | ConvertFrom-Json
                $Witnesses[$Task.service] = $Witness
                $WitnessStarted = [DateTimeOffset]::Parse([string]$Witness.started_at)
                if (
                    [string]$Witness.state -ne "running" -or
                    [string]$Witness.release_commit -ne $ReleaseCommit -or
                    $WitnessStarted -lt $Started
                ) {
                    $Healthy = $false
                }
            }
            catch {
                $Healthy = $false
                $Witnesses[$Task.service] = @{ error = $_.Exception.Message }
            }
        }
        foreach ($Task in $Disabled) {
            $State = [string](Get-ScheduledTask -TaskName $Task.task_name -ErrorAction Stop).State
            $TaskStates[$Task.task_name] = $State
            $DisabledStates[$Task.service] = $State
            if ($State -ne "Disabled") { $Healthy = $false }
        }
        $LastDetail = @{
            active_commit = $ReleaseCommit
            selected_services = @($Selected | ForEach-Object { $_.service })
            disabled_services = @($Disabled | ForEach-Object { $_.service })
            task_states = $TaskStates
            disabled_task_states = $DisabledStates
            witnesses = $Witnesses
        }
        if ($Healthy) { return $LastDetail }
        Start-Sleep -Milliseconds 500
    }
    throw "Rollback service recovery did not produce fresh healthy witnesses: $($LastDetail | ConvertTo-Json -Depth 20 -Compress)"
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
$HostRoot = $HostRoot.TrimEnd("\", "/")
$SupervisorRoot = $SupervisorRoot.TrimEnd("\", "/")
$BootstrapRoot = Join-Path $SupervisorRoot "bootstrap"
$ReportsRoot = Join-Path $SupervisorRoot "reports"
$StageParent = Join-Path $SupervisorRoot "bootstrap-updates"
$RollbackParent = Join-Path $SupervisorRoot "bootstrap-rollbacks"
$AttemptId = "stable-bootstrap-update-$Commit-" + (Get-Date -Format "yyyyMMddTHHmmss.fffffffZ")
$StageRoot = Join-Path $StageParent $AttemptId
$StagedBootstrap = Join-Path $StageRoot "bootstrap"
$RollbackBootstrap = Join-Path $RollbackParent ("bootstrap-$ExpectedOldBootstrapCommit-" + (Get-Date -Format "yyyyMMddTHHmmss.fffffffZ"))
$ActivationBackup = $null
$RefillTaskBackupXml = $null
$RefillTaskTouched = $false
$LegacyRestartWitnessStarted = $false
$UpdateTaskBackupXml = $null
$ReportPath = Join-Path $ReportsRoot ($AttemptId + ".json")
$ValidationResults = New-Object System.Collections.ArrayList
$Report = @{
    version = 1
    type = "stable-bootstrap-update-handoff"
    attempt_id = $AttemptId
    old_bootstrap_commit = $ExpectedOldBootstrapCommit
    new_bootstrap_commit = $Commit
    supervisor_root = $SupervisorRoot
    staged_bootstrap = $StagedBootstrap
    rollback_bootstrap = $RollbackBootstrap
    report_path = $ReportPath
    validation_results = @()
    file_hashes = @{}
    service_configuration = @{}
    scheduled_tasks = @{}
    activation = @{ performed = $false }
    rollback = @{ performed = $false; refill_task_restored = $false }
    update_task = @{ name = $UpdateTaskName; restored = $false }
    outcome = "started"
    errors = @()
    recorded_at = $null
}

try {
    $ScriptPath = (Resolve-Path $PSCommandPath).Path
    $ResolvedBootstrapRoot = Resolve-Path -LiteralPath $BootstrapRoot -ErrorAction SilentlyContinue
    if ($null -ne $ResolvedBootstrapRoot -and $ScriptPath.StartsWith($ResolvedBootstrapRoot.Path, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to run the handoff from inside the installed stable bootstrap."
    }

    $Git = (Get-Command git -ErrorAction Stop).Source
    $Head = (& $Git -C $RepoRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $Head -ne $Commit) {
        throw "Checkout HEAD $Head does not match requested commit $Commit."
    }
    & $Git -C $RepoRoot cat-file -e "$ExpectedOldBootstrapCommit^{commit}"
    if ($LASTEXITCODE -ne 0) { throw "Expected old bootstrap commit is not present locally: $ExpectedOldBootstrapCommit" }
    $Dirty = @(& $Git -C $RepoRoot status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) { throw "Could not verify checkout cleanliness." }
    if ($Dirty.Count -gt 0) { throw "Checkout must be clean before replacing the stable bootstrap." }

    if (-not (Test-Path $BootstrapRoot -PathType Container)) {
        throw "Installed stable bootstrap not found at $BootstrapRoot"
    }
    $SupervisorConfigPath = Join-Path $BootstrapRoot "supervisor.yaml"
    $ManagedServicesPath = Join-Path $BootstrapRoot "managed-services.json"
    $ActivePointerPath = Join-Path (Join-Path $SupervisorRoot "state") "active-release.json"
    $Report.service_configuration["preflight"] = @{
        supervisor = Get-TextFileEvidence -Path $SupervisorConfigPath
        managed_services = Get-TextFileEvidence -Path $ManagedServicesPath
        active_release = Get-ActiveReleaseEvidence -Path $ActivePointerPath
    }
    if (-not $Report.service_configuration["preflight"]["active_release"].exists) {
        throw "Installed managed release pointer is missing; refusing an unbound bootstrap handoff."
    }
    $TaskEvidence = @{}
    foreach ($TaskName in @($ExistingManagedTaskNames + $RefillTaskName)) {
        $TaskEvidence[$TaskName] = Get-ScheduledTaskEvidence -TaskName $TaskName
    }
    $Report.scheduled_tasks["preflight"] = $TaskEvidence
    $InstalledTaskBaselineMode = Assert-InstalledScheduledTaskBaseline -Evidence $TaskEvidence
    $Report.scheduled_tasks["baseline_mode"] = $InstalledTaskBaselineMode

    Test-ReportPathWritable -Path $ReportPath
    Get-BootstrapHashEvidence -RepoRoot $RepoRoot -BootstrapRoot $BootstrapRoot -OldCommit $ExpectedOldBootstrapCommit -NewCommit $Commit -Evidence $Report.file_hashes

    New-Item -ItemType Directory -Force -Path $StageParent, $RollbackParent, $ReportsRoot | Out-Null
    Copy-Item -Recurse -Force -Path $BootstrapRoot -Destination $StagedBootstrap
    Copy-BootstrapSourceFiles -RepoRoot $RepoRoot -DestinationRoot $StagedBootstrap -Evidence $Report.file_hashes
    Add-StagedServiceConfiguration -InstalledBootstrap $BootstrapRoot -StagedBootstrap $StagedBootstrap -BootstrapPython $BootstrapPython -Evidence $Report.service_configuration
    $ConfiguredHostRoot = [string]$Report.service_configuration["staged_generation"].host_root
    if ((ConvertTo-ComparablePath -Path $ConfiguredHostRoot) -ne (ConvertTo-ComparablePath -Path $HostRoot)) {
        throw "HostRoot parameter does not match installed supervisor.yaml host_root."
    }

    Invoke-Checked -Name "staged PowerShell parser" -Results $ValidationResults -Command {
        Test-PowerShellScriptsParse -BootstrapRoot $StagedBootstrap
    }

    $Task = Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction Stop
    $UpdateTaskBackupXml = Join-Path $StageRoot "update-task-before.xml"
    $UpdateTaskPreflightXml = [string](Export-ScheduledTask -TaskName $UpdateTaskName)
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($UpdateTaskBackupXml, $UpdateTaskPreflightXml, $Utf8NoBom)
    $UpdateTaskPreflightEvidence = Get-ScheduledTaskEvidence -TaskName $UpdateTaskName
    $Report.update_task["preflight"] = $UpdateTaskPreflightEvidence
    $Report.update_task["initial_state"] = [string]$Task.State
    $Report.update_task["preflight_state"] = [string]$UpdateTaskPreflightEvidence.state
    $Report.update_task["preflight_enabled_contract"] = Get-UpdaterEnabledContract -State ([string]$UpdateTaskPreflightEvidence.state)
    $Report.update_task["preflight_durable_enabled"] = ([string]$UpdateTaskPreflightEvidence.state -ne "Disabled")
    $Report.update_task["preflight_xml_sha256"] = [string]$UpdateTaskPreflightEvidence.xml_sha256
    Stop-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue
    $Deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $Deadline) {
        $TaskState = [string](Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction Stop).State
        if ($TaskState -ne "Running") { break }
        Start-Sleep -Milliseconds 500
    }
    if ([string](Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction Stop).State -eq "Running") {
        throw "Updater Scheduled Task did not stop within the bounded handoff window."
    }
    Disable-ScheduledTask -TaskName $UpdateTaskName -ErrorAction Stop | Out-Null
    $Report.update_task["disabled_for_handoff"] = $true

    Assert-NoActiveUpdateAttempt -SupervisorRoot $SupervisorRoot

    $ExistingRefillTask = Get-ScheduledTask -TaskName $RefillTaskName -ErrorAction SilentlyContinue
    if ($ExistingRefillTask) {
        $RefillTaskBackupXml = Join-Path $StageRoot "refill-task-before.xml"
        Export-ScheduledTask -TaskName $RefillTaskName | Set-Content -Path $RefillTaskBackupXml -Encoding UTF8
    }
    if ($InstalledTaskBaselineMode -eq "five-task-refill-absent") {
        $RefillTaskTouched = $true
        Register-DisabledRefillTask -SupervisorRoot $SupervisorRoot -HostRoot $HostRoot -BackupXmlPath $RefillTaskBackupXml
    }
    $Report.scheduled_tasks["staged_refill"] = Get-ScheduledTaskEvidence -TaskName $RefillTaskName
    if ([string]$Report.scheduled_tasks["staged_refill"].state -ne "Disabled") {
        throw "Refill task must remain exactly Disabled until a later managed-release cutover."
    }
    $RefillAction = @($Report.scheduled_tasks["staged_refill"].actions)[0]
    if ($null -eq $RefillAction -or [string]$RefillAction.arguments -notlike "*-HostRoot `"$HostRoot`"*") {
        throw "Refill task HostRoot does not match the installed supervisor configuration."
    }
    Invoke-Checked -Name "staged Python to PowerShell states transport" -Results $ValidationResults -Command {
        Test-StagedTaskTransport -BootstrapRoot $StagedBootstrap -BootstrapPython $BootstrapPython | ConvertTo-Json -Compress
    }

    Copy-Item -Recurse -Force -Path $BootstrapRoot -Destination $RollbackBootstrap
    $ActivationBackup = Join-Path $RollbackParent ("activation-backup-" + [Guid]::NewGuid().ToString("N"))
    Move-Item -Path $BootstrapRoot -Destination $ActivationBackup
    try {
        Move-Item -Path $StagedBootstrap -Destination $BootstrapRoot
        if ($env:MSOS_STABLE_BOOTSTRAP_HANDOFF_TEST_CORRUPT_ACTIVATED_FILE -eq "1") {
            Add-Content -Path (Join-Path $BootstrapRoot $BootstrapFileMap[0].target) -Value "test-only-corruption"
        }
        Confirm-ActivatedBootstrapHashes -BootstrapRoot $BootstrapRoot -Evidence $Report.file_hashes
        foreach ($Name in @("supervisor.yaml", "managed-services.json")) {
            $Report.service_configuration[$Name]["activated"] = Get-TextFileEvidence -Path (Join-Path $BootstrapRoot $Name)
            if ($Report.service_configuration[$Name]["activated"].sha256 -ne $Report.service_configuration[$Name]["staged"].sha256) {
                throw "Activated service configuration $Name does not match the staged reviewed handoff content."
            }
        }
        $LegacyRestartWitnessStarted = $true
        $RestartWitness = Test-ActivatedLegacyRestartWitness -BootstrapRoot $BootstrapRoot -BootstrapPython $BootstrapPython
        $Report.activation = @{
            performed = $true
            activated_bootstrap = $BootstrapRoot
            activation_backup = $ActivationBackup
            activated_hashes_verified = $true
            service_configuration_verified = $true
            refill_task_state = [string](Get-ScheduledTask -TaskName $RefillTaskName -ErrorAction Stop).State
            legacy_restart_witness = $RestartWitness
        }
        $Report.outcome = "success"
    }
    catch {
        $ActivationError = $_.Exception.Message
        Restore-HandoffState -Reason $ActivationError
        if ($LegacyRestartWitnessStarted) {
            try {
                $Recovery = Test-RestoredLegacyRecoveryWitness -SupervisorRoot $SupervisorRoot
                $Report.rollback["service_recovery"] = $Recovery
                $Report.rollback["service_recovery_passed"] = $true
                $Report.outcome = "rolled_back"
            }
            catch {
                $Report.rollback["service_recovery_passed"] = $false
                $Report.rollback["service_recovery_error"] = $_.Exception.Message
                $Report.outcome = "rollback_failed"
                $Report.errors += "Rollback service recovery failed: $($_.Exception.Message)"
            }
        }
        throw
    }
}
catch {
    $Report.errors += $_.Exception.Message
    if ($Report.outcome -eq "started") { $Report.outcome = "failed" }
    if (
        ($null -ne $ActivationBackup -and (Test-Path $ActivationBackup)) -or
        ($RefillTaskTouched -and -not $Report.rollback["refill_task_restored"])
    ) {
        Restore-HandoffState -Reason "outer catch restore"
    }
    throw
}
finally {
    try {
        if ($UpdateTaskBackupXml -and (Test-Path $UpdateTaskBackupXml -PathType Leaf)) {
            Restore-UpdaterTask -BackupXmlPath $UpdateTaskBackupXml -PreflightEvidence $Report.update_task["preflight"]
        }
    }
    catch {
        $Report.errors += "Failed to restore updater Scheduled Task: $($_.Exception.Message)"
        if ($Report.outcome -eq "success") {
            $Report.outcome = "failed_after_activation"
            Restore-HandoffState -Reason "updater Scheduled Task restoration failed"
        }
    }
    $Report.validation_results = @($ValidationResults)
    $Report.recorded_at = [DateTimeOffset]::UtcNow.ToString("o")
    try {
        if ($env:MSOS_STABLE_BOOTSTRAP_HANDOFF_TEST_REPORT_WRITE_FAILURE -eq "1") {
            throw "Simulated report write failure."
        }
        Write-ImmutableJson -Path $ReportPath -Value $Report
        Write-Host "Stable supervisor bootstrap handoff report: $ReportPath"
    }
    catch {
        $Report.errors += "Could not write stable bootstrap update report: $($_.Exception.Message)"
        if ($Report.outcome -eq "success") {
            $Report.outcome = "failed_evidence_missing"
            Restore-HandoffState -Reason "immutable report persistence failed"
        }
        throw "Could not write stable bootstrap update report: $($_.Exception.Message)"
    }
    if ($Report.outcome -ne "success") {
        throw "Stable supervisor bootstrap handoff finished with outcome $($Report.outcome)."
    }
    if ($null -ne $ActivationBackup -and (Test-Path $ActivationBackup)) {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $ActivationBackup
        $ActivationBackup = $null
    }
    Write-Host "Stable supervisor bootstrap updated to $Commit" -ForegroundColor Green
}
