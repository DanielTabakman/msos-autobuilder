[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{40}$")]
    [string]$Commit,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-f]{40}$")]
    [string]$ExpectedOldBootstrapCommit,

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
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
    "MSOS Autobuilder Controlled Publisher"
)

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
    $Stream = [System.IO.File]::OpenRead($Path)
    try {
        $Sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            return [System.BitConverter]::ToString($Sha256.ComputeHash($Stream)).
                Replace("-", "").
                ToLowerInvariant()
        }
        finally {
            $Sha256.Dispose()
        }
    }
    finally {
        $Stream.Dispose()
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
    $LockPath = Join-Path $SupervisorRoot "state\update.lock"
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
        [Parameter(Mandatory = $true)][string]$DestinationRoot
    )
    foreach ($Entry in $BootstrapFileMap) {
        $Source = Join-Path $RepoRoot $Entry.source
        $Destination = Join-Path $DestinationRoot $Entry.target
        if (-not (Test-Path $Source -PathType Leaf)) { throw "Required source file missing: $($Entry.source)" }
        Copy-Item -Force -Path $Source -Destination $Destination
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
        [Parameter(Mandatory = $true)][string]$OldCommit
    )
    $Git = (Get-Command git -ErrorAction Stop).Source
    $OldSourceRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-old-" + [Guid]::NewGuid().ToString("N"))
    $Evidence = @{}
    try {
        & $Git -c core.autocrlf=false clone --quiet --no-hardlinks --no-checkout $RepoRoot $OldSourceRoot
        if ($LASTEXITCODE -ne 0) { throw "Could not clone source repository for old bootstrap verification." }
        & $Git -C $OldSourceRoot -c core.autocrlf=false checkout --quiet --detach $OldCommit
        if ($LASTEXITCODE -ne 0) { throw "Could not check out expected old bootstrap commit." }
        foreach ($Entry in $BootstrapFileMap) {
            $Installed = Join-Path $BootstrapRoot $Entry.target
            if (-not (Test-Path $Installed -PathType Leaf)) {
                throw "Installed bootstrap file missing: $($Entry.target)"
            }
            $OldSource = Join-Path $OldSourceRoot $Entry.source
            if (-not (Test-Path $OldSource -PathType Leaf)) {
                throw "Could not export $($Entry.source) from expected old commit $OldCommit."
            }
            $OldExpected = Get-FileSha256 -Path $OldSource
            $InstalledHash = Get-FileSha256 -Path $Installed
            $NewSource = Join-Path $RepoRoot $Entry.source
            $NewHash = Get-FileSha256 -Path $NewSource
            $Evidence[$Entry.target] = @{
                source = $Entry.source
                installed_sha256 = $InstalledHash
                expected_old_commit_sha256 = $OldExpected
                new_commit_sha256 = $NewHash
            }
            if ($InstalledHash -ne $OldExpected) {
                throw "Installed bootstrap file $($Entry.target) does not match expected old commit $OldCommit."
            }
        }
    }
    finally {
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $OldSourceRoot
    }
    return $Evidence
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
        $ActivatedHash = Get-FileSha256 -Path $Live
        $Evidence[$Entry.target]["activated_sha256"] = $ActivatedHash
        if ($ActivatedHash -ne $Evidence[$Entry.target]["new_commit_sha256"]) {
            throw "Activated bootstrap file $($Entry.target) does not match reviewed source hash."
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
    $Probe = @"
import importlib.util
import json
import pathlib
import sys

bootstrap = pathlib.Path(sys.argv[1])
task_names = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
module_path = bootstrap / "self_update_supervisor.py"
spec = importlib.util.spec_from_file_location("staged_self_update_supervisor", module_path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
controller = module.PowerShellTaskController(bootstrap / "windows_self_update_task_control.ps1")
states = controller.states(task_names)
print(json.dumps(states, sort_keys=True))
"@
    try {
        Write-Utf8NoBom -Path $TaskNamesPath -Value (($ManagedTaskNames | ConvertTo-Json -Compress) + [Environment]::NewLine)
        Write-Utf8NoBom -Path $ProbePath -Value $Probe
        $Output = & $BootstrapPython $ProbePath $BootstrapRoot $TaskNamesPath
        if ($LASTEXITCODE -ne 0) { throw "Staged Python to PowerShell task-name transport failed." }
        $States = $Output | ConvertFrom-Json
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
    }
}

$RepoRoot = (Resolve-Path $RepoRoot).Path
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
    activation = @{ performed = $false }
    rollback = @{ performed = $false }
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
    Test-ReportPathWritable -Path $ReportPath
    $Report.file_hashes = Get-BootstrapHashEvidence -RepoRoot $RepoRoot -BootstrapRoot $BootstrapRoot -OldCommit $ExpectedOldBootstrapCommit

    New-Item -ItemType Directory -Force -Path $StageParent, $RollbackParent, $ReportsRoot | Out-Null
    Copy-Item -Recurse -Force -Path $BootstrapRoot -Destination $StagedBootstrap
    Copy-BootstrapSourceFiles -RepoRoot $RepoRoot -DestinationRoot $StagedBootstrap

    Invoke-Checked -Name "staged PowerShell parser" -Results $ValidationResults -Command {
        Test-PowerShellScriptsParse -BootstrapRoot $StagedBootstrap
    }
    Invoke-Checked -Name "staged Python to PowerShell states transport" -Results $ValidationResults -Command {
        Test-StagedTaskTransport -BootstrapRoot $StagedBootstrap -BootstrapPython $BootstrapPython | ConvertTo-Json -Compress
    }

    $Task = Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction Stop
    $Report.update_task["initial_state"] = [string]$Task.State
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

    Copy-Item -Recurse -Force -Path $BootstrapRoot -Destination $RollbackBootstrap
    $ActivationBackup = Join-Path $RollbackParent ("activation-backup-" + [Guid]::NewGuid().ToString("N"))
    Move-Item -Path $BootstrapRoot -Destination $ActivationBackup
    try {
        Move-Item -Path $StagedBootstrap -Destination $BootstrapRoot
        if ($env:MSOS_STABLE_BOOTSTRAP_HANDOFF_TEST_CORRUPT_ACTIVATED_FILE -eq "1") {
            Add-Content -Path (Join-Path $BootstrapRoot $BootstrapFileMap[0].target) -Value "test-only-corruption"
        }
        Confirm-ActivatedBootstrapHashes -BootstrapRoot $BootstrapRoot -Evidence $Report.file_hashes
        $Report.activation = @{
            performed = $true
            activated_bootstrap = $BootstrapRoot
            activation_backup = $ActivationBackup
            activated_hashes_verified = $true
        }
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $ActivationBackup
        $ActivationBackup = $null
        $Report.outcome = "success"
    }
    catch {
        if (Test-Path $BootstrapRoot) {
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $BootstrapRoot
        }
        if ($null -ne $ActivationBackup -and (Test-Path $ActivationBackup)) {
            Move-Item -Path $ActivationBackup -Destination $BootstrapRoot
            $Report.rollback = @{ performed = $true; restored_from = $ActivationBackup; reason = $_.Exception.Message }
            $ActivationBackup = $null
        }
        throw
    }
}
catch {
    $Report.errors += $_.Exception.Message
    if ($Report.outcome -eq "started") { $Report.outcome = "failed" }
    if ($null -ne $ActivationBackup -and (Test-Path $ActivationBackup) -and -not (Test-Path $BootstrapRoot)) {
        Move-Item -Path $ActivationBackup -Destination $BootstrapRoot
        $Report.rollback = @{ performed = $true; restored_from = $ActivationBackup; reason = "outer catch restore" }
    }
    throw
}
finally {
    try {
        if (Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue) {
            Enable-ScheduledTask -TaskName $UpdateTaskName -ErrorAction Stop | Out-Null
            $Report.update_task["restored"] = $true
            $Report.update_task["final_state"] = [string](Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction Stop).State
        }
    }
    catch {
        $Report.errors += "Failed to re-enable updater Scheduled Task: $($_.Exception.Message)"
        if ($Report.outcome -eq "success") { $Report.outcome = "failed_after_activation" }
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
        if ($Report.outcome -eq "success") { $Report.outcome = "failed_evidence_missing" }
        throw "Could not write stable bootstrap update report: $($_.Exception.Message)"
    }
    if ($Report.outcome -ne "success") {
        throw "Stable supervisor bootstrap handoff finished with outcome $($Report.outcome)."
    }
    Write-Host "Stable supervisor bootstrap updated to $Commit" -ForegroundColor Green
}
