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
    [string]$TaskNamespace = "",
    [string]$BootstrapPython = (Join-Path $SupervisorRoot "bootstrap-venv\Scripts\python.exe")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Omitted -TaskNamespace keeps production names. An explicitly bound blank value is rejected
# before any filesystem, Scheduled Task, or report mutation.
$TaskNamespaceWasBound = $PSBoundParameters.ContainsKey("TaskNamespace")
$UpdateTaskNameWasBound = $PSBoundParameters.ContainsKey("UpdateTaskName")
if ($TaskNamespaceWasBound -and ($null -eq $TaskNamespace -or [string]::IsNullOrWhiteSpace([string]$TaskNamespace))) {
    throw "TaskNamespace was explicitly supplied but is blank. Omit -TaskNamespace for production names, or provide a valid nonblank namespace."
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
$RefillServiceName = "refill"

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

function Resolve-HandoffTaskNames {
    param(
        [string]$Namespace = "",
        [string]$UpdateTaskNameOverride = "",
        [bool]$UpdateTaskNameWasBound = $false
    )
    $Normalized = Get-NormalizedTaskNamespace -Namespace $Namespace
    $Managed = New-Object System.Collections.Generic.List[object]
    foreach ($Entry in $script:ProductionManagedTaskRoles) {
        [void]$Managed.Add(@{
            service = $Entry.service
            role = $Entry.role
            task = (Get-NamespacedTaskName -Role $Entry.role -Namespace $Normalized)
        })
    }
    $ResolvedUpdateTaskName = Get-NamespacedTaskName -Role $script:UpdateSupervisorRole -Namespace $Normalized
    $Isolated = -not [string]::IsNullOrWhiteSpace($Normalized)
    if ($UpdateTaskNameWasBound) {
        # The pre-namespace -UpdateTaskName surface stays usable on its own. Combining it with a
        # namespace may not silently produce a task set that spans two namespaces.
        if ($Isolated -and $UpdateTaskNameOverride -ne $ResolvedUpdateTaskName) {
            throw "UpdateTaskName '$UpdateTaskNameOverride' conflicts with the namespaced update supervisor task '$ResolvedUpdateTaskName'. Supply only one, or supply the matching name."
        }
        if (-not $Isolated) {
            $ResolvedUpdateTaskName = $UpdateTaskNameOverride
        }
    }
    return @{
        namespace = $Normalized
        isolated = $Isolated
        managed_tasks = $Managed.ToArray()
        update_task_name = $ResolvedUpdateTaskName
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

function Assert-HandoffTaskNamespaceReady {
    param(
        [Parameter(Mandatory = $true)]$ResolvedNames,
        [Parameter(Mandatory = $true)][string]$HostRootPath,
        [Parameter(Mandatory = $true)][string]$SupervisorRootPath
    )
    $Namespace = [string]$ResolvedNames.namespace
    if ($ResolvedNames.isolated) {
        if ($Namespace -notmatch '^[A-Za-z0-9](?:[A-Za-z0-9 ._-]{0,78}[A-Za-z0-9])?$') {
            throw "TaskNamespace is malformed. Use 1-80 characters of letters, digits, spaces, '.', '_', or '-'."
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
    $InvalidFileNameChars = [System.IO.Path]::GetInvalidFileNameChars()
    foreach ($Name in $AllNames) {
        if ($Name.Length -gt $script:MaxScheduledTaskNameLength) {
            throw "Scheduled task name exceeds Windows limit ($script:MaxScheduledTaskNameLength): $Name"
        }
        foreach ($Character in $Name.ToCharArray()) {
            if ($InvalidFileNameChars -contains $Character) {
                throw "Scheduled task name contains illegal characters: $Name"
            }
        }
        $Key = $Name.ToLowerInvariant()
        if ($Seen.ContainsKey($Key)) {
            throw "Duplicate scheduled task name resolved: $Name"
        }
        $Seen[$Key] = $true
    }

    if (-not $ResolvedNames.isolated) { return }

    # An isolated handoff may not name, or transact through, a production task or root.
    foreach ($Name in $AllNames) {
        foreach ($Protected in @(Get-ProductionTaskNames)) {
            if ($Name.Equals($Protected, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "Isolated task name collides with protected production task name: $Name"
            }
        }
    }

    $UserProfile = [string]$env:USERPROFILE
    if ([string]::IsNullOrWhiteSpace($UserProfile)) {
        throw "USERPROFILE is required to prove an isolated handoff does not overlap the protected production roots."
    }
    $HostFull = [System.IO.Path]::GetFullPath($HostRootPath)
    $SupervisorFull = [System.IO.Path]::GetFullPath($SupervisorRootPath)
    $ProtectedHostFull = [System.IO.Path]::GetFullPath((Join-Path $UserProfile ".msos-autobuilder"))
    $ProtectedSupervisorFull = [System.IO.Path]::GetFullPath((Join-Path $UserProfile ".msos-autobuilder-supervisor"))
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
}

# Resolve and validate all seven task names before any evidence, filesystem, or task mutation.
$ResolvedTaskNames = Resolve-HandoffTaskNames `
    -Namespace $TaskNamespace `
    -UpdateTaskNameOverride $UpdateTaskName `
    -UpdateTaskNameWasBound $UpdateTaskNameWasBound
Assert-HandoffTaskNamespaceReady -ResolvedNames $ResolvedTaskNames -HostRootPath $HostRoot -SupervisorRootPath $SupervisorRoot

$IsolatedTaskNamespace = [bool]$ResolvedTaskNames.isolated
$EffectiveTaskNamespace = [string]$ResolvedTaskNames.namespace
$UpdateTaskName = [string]$ResolvedTaskNames.update_task_name
$ResolvedManagedTasks = @($ResolvedTaskNames.managed_tasks)
$RefillTaskName = [string](
    $ResolvedManagedTasks | Where-Object { $_.service -eq $RefillServiceName } | Select-Object -First 1
).task
$ManagedTaskNames = @($ResolvedManagedTasks | ForEach-Object { [string]$_.task })
$ExistingManagedTaskNames = @(
    $ResolvedManagedTasks |
        Where-Object { $_.service -ne $RefillServiceName } |
        ForEach-Object { [string]$_.task }
)
$ExpectedBaselineTaskNames = @{
    managed_tasks = @(
        foreach ($Entry in $ResolvedManagedTasks) {
            if ($Entry.service -eq $RefillServiceName) { continue }
            @{ service = [string]$Entry.service; task_name = [string]$Entry.task }
        }
    )
    refill_task = @{ service = $RefillServiceName; task_name = $RefillTaskName }
}
$RefillWitnessMaxAgeSeconds = 600
$RefillWitnessMaxFutureSkewSeconds = 120

$ProtectedRuntimeRelativePaths = @(
    "queue/pending",
    "queue/running",
    "state/jobs",
    "state/feed-seen.json",
    "state/refill-generation.json",
    "state/refill-generation-history",
    "state/refill-generation-supersessions",
    "state/refill-evidence/sources/dispatch-prepared",
    "state/refill-evidence/dispatch/prepared",
    "state/refill-evidence/dispatch/submitted",
    "state/refill-evidence/heads/dispatch/prepared",
    "state/refill-evidence/heads/dispatch/submitted",
    "state/results-relay-seen.json",
    "state/candidate-gate-seen.json",
    "state/revision-loop-seen.json",
    "state/controlled-publisher-seen.json",
    "state/host-evidence",
    "state/relay-evidence",
    "state/gate-evidence",
    "state/revision-evidence",
    "state/publisher-evidence"
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
    $Bytes = [System.IO.File]::ReadAllBytes($Path)
    return Get-ByteArraySha256 -Bytes $Bytes
}

function Get-TextFileEvidence {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path $Path -PathType Leaf)) {
        # Keep a stable comparison contract under Set-StrictMode: absent files must
        # expose the same properties as present files so unchanged-absent checks work.
        return @{
            exists = $false
            path = $Path
            sha256 = $null
            canonical_sha256 = $null
            length = 0
        }
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

function Assert-TextFileEvidenceUnchanged {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Before,
        [Parameter(Mandatory = $true)][hashtable]$After,
        [Parameter(Mandatory = $true)][string]$Label
    )
    if ([bool]$Before.exists -ne [bool]$After.exists) {
        throw "$Label changed during policy-paused bootstrap handoff."
    }
    if (-not [bool]$Before.exists) {
        return
    }
    if ([string]$Before.sha256 -ne [string]$After.sha256) {
        throw "$Label changed during policy-paused bootstrap handoff."
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

function Get-JsonFileEvidence {
    param([Parameter(Mandatory = $true)][string]$Path)
    $Evidence = Get-TextFileEvidence -Path $Path
    if (-not $Evidence.exists) { return $Evidence }
    try {
        $Value = Get-Content -Path $Path -Raw | ConvertFrom-Json
    }
    catch {
        throw "JSON file is malformed: $Path"
    }
    if ($null -eq $Value -or $Value.GetType().Name -notin @("PSCustomObject", "OrderedDictionary", "Hashtable")) {
        throw "JSON file must contain an object: $Path"
    }
    $Evidence["json"] = $Value
    return $Evidence
}

function Get-ByteArraySha256 {
    param(
        [AllowEmptyCollection()]
        [byte[]]$Bytes = @()
    )
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
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [byte[]]$Bytes
    )
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
        [Parameter(Mandatory = $true)][hashtable]$ExpectedTaskNames,
        [Parameter(Mandatory = $true)][hashtable]$Evidence
    )
    if (-not (Test-Path $BootstrapPython -PathType Leaf)) {
        throw "Stable supervisor Python not found at $BootstrapPython"
    }
    $ProbePath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-service-config-" + [Guid]::NewGuid().ToString("N") + ".py")
    $ExpectedPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-service-config-expected-" + [Guid]::NewGuid().ToString("N") + ".json")
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
expected_path = pathlib.Path(sys.argv[3])
stdout_path = pathlib.Path(sys.argv[4])
stderr_path = pathlib.Path(sys.argv[5])

expected = json.loads(expected_path.read_text(encoding="utf-8-sig"))
old_tasks = [
    (str(entry["service"]), str(entry["task_name"]))
    for entry in expected["managed_tasks"]
]
refill_task = {
    "service": str(expected["refill_task"]["service"]),
    "task_name": str(expected["refill_task"]["task_name"]),
}
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
        Write-Utf8NoBom -Path $ExpectedPath -Value (($ExpectedTaskNames | ConvertTo-Json -Depth 6) + [Environment]::NewLine)
        & $BootstrapPython $ProbePath $InstalledBootstrap $StagedBootstrap $ExpectedPath $StdoutPath $StderrPath
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
        Remove-Item -Force -ErrorAction SilentlyContinue $ExpectedPath
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

function Get-ApprovedPowerShellExecutable {
    param([Parameter(Mandatory = $true)][hashtable]$Evidence)
    $AuthorityActions = @($Evidence[$ExistingManagedTaskNames[0]].actions)
    if ($AuthorityActions.Count -ne 1) {
        throw "Managed host Scheduled Task must expose exactly one authority action."
    }
    $Executable = [string]$AuthorityActions[0].execute
    if (-not $Executable) {
        throw "Managed host Scheduled Task does not expose an approved PowerShell executable."
    }
    $ExecutableName = [System.IO.Path]::GetFileName($Executable).ToLowerInvariant()
    if ($ExecutableName -notin @("powershell.exe", "powershell", "pwsh.exe", "pwsh")) {
        throw "Managed host Scheduled Task authority executable is not an approved PowerShell executable."
    }
    return $Executable
}

function Split-StrictTaskArguments {
    param([Parameter(Mandatory = $true)][string]$Arguments)
    if ($Arguments.IndexOf([char]0) -ge 0 -or $Arguments.Contains("`r") -or $Arguments.Contains("`n")) {
        throw "Running refill task action contains an unsupported command form."
    }
    $Tokens = New-Object System.Collections.Generic.List[string]
    $Current = New-Object System.Text.StringBuilder
    $InQuotes = $false
    for ($Index = 0; $Index -lt $Arguments.Length; $Index++) {
        $Character = $Arguments[$Index]
        if ($Character -eq '"') {
            $InQuotes = -not $InQuotes
            continue
        }
        if (-not $InQuotes -and [char]::IsWhiteSpace($Character)) {
            if ($Current.Length -gt 0) {
                $Tokens.Add($Current.ToString())
                [void]$Current.Clear()
            }
            continue
        }
        if ($Character -eq '`') {
            throw "Running refill task action contains an unsupported escape sequence."
        }
        [void]$Current.Append($Character)
    }
    if ($InQuotes) {
        throw "Running refill task action contains an unterminated quoted value."
    }
    if ($Current.Length -gt 0) {
        $Tokens.Add($Current.ToString())
    }
    return $Tokens.ToArray()
}

function Get-ProtectedPathEvidence {
    param(
        [Parameter(Mandatory = $true)][string]$HostRoot,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )
    $HostFull = [System.IO.Path]::GetFullPath($HostRoot).TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $NativeRelative = $RelativePath.Replace("/", [System.IO.Path]::DirectorySeparatorChar)
    $AbsolutePath = [System.IO.Path]::GetFullPath((Join-Path $HostFull $NativeRelative))
    $HostPrefix = $HostFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $AbsolutePath.StartsWith($HostPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Protected runtime path escaped HostRoot: $RelativePath"
    }

    $Current = $HostFull
    if (Test-Path -LiteralPath $Current) {
        $HostItem = Get-Item -LiteralPath $Current -Force
        if (($HostItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Protected runtime path traverses a reparse point: ."
        }
    }
    foreach ($Part in $RelativePath.Split('/')) {
        $Current = Join-Path $Current $Part
        if (-not (Test-Path -LiteralPath $Current)) { break }
        $Item = Get-Item -LiteralPath $Current -Force
        if (($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Protected runtime path traverses a reparse point: $RelativePath"
        }
    }

    if (-not (Test-Path -LiteralPath $AbsolutePath)) {
        return @{
            relative_path = $RelativePath
            exists = $false
            kind = "absent"
            byte_length = $null
            sha256 = $null
            inventory = @()
        }
    }

    $RootItem = Get-Item -LiteralPath $AbsolutePath -Force
    if (($RootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Protected runtime path is a reparse point: $RelativePath"
    }
    if ($RootItem.PSIsContainer) {
        $Inventory = New-Object System.Collections.ArrayList
        $Pending = New-Object System.Collections.Stack
        $Pending.Push(@{ absolute = $AbsolutePath; relative = "" })
        while ($Pending.Count -gt 0) {
            $Directory = $Pending.Pop()
            $Children = @(Get-ChildItem -LiteralPath $Directory.absolute -Force | Sort-Object Name)
            foreach ($Child in $Children) {
                $ChildRelative = if ($Directory.relative) { "$($Directory.relative)/$($Child.Name)" } else { $Child.Name }
                if (($Child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw "Protected runtime inventory contains a reparse point: $RelativePath/$ChildRelative"
                }
                if ($Child.PSIsContainer) {
                    [void]$Inventory.Add(@{
                        relative_path = $ChildRelative.Replace("\\", "/")
                        kind = "directory"
                        byte_length = $null
                        sha256 = $null
                    })
                    $Pending.Push(@{ absolute = $Child.FullName; relative = $ChildRelative })
                }
                elseif ($Child -is [System.IO.FileInfo]) {
                    [void]$Inventory.Add(@{
                        relative_path = $ChildRelative.Replace("\\", "/")
                        kind = "file"
                        byte_length = [long]$Child.Length
                        sha256 = Get-FileSha256 -Path $Child.FullName
                    })
                }
                else {
                    throw "Protected runtime inventory contains an unsupported filesystem object: $RelativePath/$ChildRelative"
                }
            }
        }
        $SortedInventory = @($Inventory | Sort-Object relative_path)
        $Records = @($SortedInventory | ForEach-Object {
            "{0}`t{1}`t{2}`t{3}" -f $_.relative_path, $_.kind, $_.byte_length, $_.sha256
        })
        $InventoryBytes = [System.Text.Encoding]::UTF8.GetBytes(($Records -join "`n"))
        return @{
            relative_path = $RelativePath
            exists = $true
            kind = "directory"
            byte_length = $null
            sha256 = Get-ByteArraySha256 -Bytes $InventoryBytes
            inventory = $SortedInventory
        }
    }
    if ($RootItem -is [System.IO.FileInfo]) {
        return @{
            relative_path = $RelativePath
            exists = $true
            kind = "file"
            byte_length = [long]$RootItem.Length
            sha256 = Get-FileSha256 -Path $AbsolutePath
            inventory = @()
        }
    }
    throw "Protected runtime path has an unsupported filesystem type: $RelativePath"
}

function Get-ProtectedRuntimeStateSnapshot {
    param([Parameter(Mandatory = $true)][string]$HostRoot)
    $Paths = @(
        foreach ($RelativePath in $ProtectedRuntimeRelativePaths) {
            Get-ProtectedPathEvidence -HostRoot $HostRoot -RelativePath $RelativePath
        }
    )
    return @{
        version = 1
        host_root = $HostRoot
        captured_at = [DateTimeOffset]::UtcNow.ToString("o")
        paths = $Paths
    }
}

function Compare-ProtectedRuntimeStateSnapshots {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Before,
        [Parameter(Mandatory = $true)][hashtable]$After
    )
    $Differences = New-Object System.Collections.ArrayList
    $BeforeByPath = @{}
    $AfterByPath = @{}
    foreach ($Entry in @($Before.paths)) { $BeforeByPath[[string]$Entry.relative_path] = $Entry }
    foreach ($Entry in @($After.paths)) { $AfterByPath[[string]$Entry.relative_path] = $Entry }
    foreach ($RelativePath in $ProtectedRuntimeRelativePaths) {
        $Old = $BeforeByPath[$RelativePath]
        $New = $AfterByPath[$RelativePath]
        if ([bool]$Old.exists -ne [bool]$New.exists) {
            [void]$Differences.Add(@{
                relative_path = $RelativePath
                change = if ($New.exists) { "appeared" } else { "disappeared" }
                before = $Old
                after = $New
            })
            continue
        }
        if (-not $Old.exists) { continue }
        if ([string]$Old.kind -ne [string]$New.kind) {
            [void]$Differences.Add(@{ relative_path = $RelativePath; change = "type_changed"; before = $Old; after = $New })
            continue
        }
        if ($Old.kind -eq "file") {
            if ([long]$Old.byte_length -ne [long]$New.byte_length -or [string]$Old.sha256 -ne [string]$New.sha256) {
                [void]$Differences.Add(@{ relative_path = $RelativePath; change = "content_changed"; before = $Old; after = $New })
            }
            continue
        }
        $OldChildren = @{}
        $NewChildren = @{}
        foreach ($Child in @($Old.inventory)) { $OldChildren[[string]$Child.relative_path] = $Child }
        foreach ($Child in @($New.inventory)) { $NewChildren[[string]$Child.relative_path] = $Child }
        $ChildNames = @(@($OldChildren.Keys) + @($NewChildren.Keys) | Sort-Object -Unique)
        foreach ($ChildName in $ChildNames) {
            $OldChild = $OldChildren[$ChildName]
            $NewChild = $NewChildren[$ChildName]
            if ($null -eq $OldChild) {
                [void]$Differences.Add(@{ relative_path = "$RelativePath/$ChildName"; change = "child_appeared"; before = $null; after = $NewChild })
            }
            elseif ($null -eq $NewChild) {
                [void]$Differences.Add(@{ relative_path = "$RelativePath/$ChildName"; change = "child_disappeared"; before = $OldChild; after = $null })
            }
            elseif ([string]$OldChild.kind -ne [string]$NewChild.kind) {
                [void]$Differences.Add(@{ relative_path = "$RelativePath/$ChildName"; change = "child_type_changed"; before = $OldChild; after = $NewChild })
            }
            elseif ($OldChild.kind -eq "file" -and ([long]$OldChild.byte_length -ne [long]$NewChild.byte_length -or [string]$OldChild.sha256 -ne [string]$NewChild.sha256)) {
                [void]$Differences.Add(@{ relative_path = "$RelativePath/$ChildName"; change = "child_content_changed"; before = $OldChild; after = $NewChild })
            }
        }
    }
    return $Differences.ToArray()
}

function Assert-InstalledScheduledTaskBaseline {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Evidence,
        [Parameter(Mandatory = $true)][hashtable]$ActiveRelease,
        [Parameter(Mandatory = $true)][string]$HostRoot,
        [Parameter(Mandatory = $true)][string]$SupervisorRoot,
        [string]$ApprovedPowerShellExecutable
    )
    foreach ($TaskName in $ExistingManagedTaskNames) {
        if (-not $Evidence[$TaskName].exists) {
            throw "Installed Scheduled Task baseline is incomplete: missing $TaskName."
        }
    }
    if ($Evidence[$RefillTaskName].exists) {
        $RefillState = [string]$Evidence[$RefillTaskName].state
        if ($RefillState -eq "Disabled") {
            return "six-task-disabled-refill"
        }
        if ($RefillState -ne "Running") {
            throw "Installed Scheduled Task baseline requires refill to be absent, exactly Disabled, or exactly Running with a proved paused policy."
        }
        if (-not $ApprovedPowerShellExecutable) {
            throw "Running refill baseline requires an approved PowerShell executable."
        }
        Assert-RunningPolicyPausedRefillBaseline -Evidence $Evidence -ActiveRelease $ActiveRelease -HostRoot $HostRoot -SupervisorRoot $SupervisorRoot -ApprovedPowerShellExecutable $ApprovedPowerShellExecutable
        return "six-task-running-policy-paused"
    }
    return "five-task-refill-absent"
}

function Assert-RefillTaskActionMatchesStableRunner {
    param(
        [Parameter(Mandatory = $true)][hashtable]$RefillEvidence,
        [Parameter(Mandatory = $true)][string]$HostRoot,
        [Parameter(Mandatory = $true)][string]$SupervisorRoot,
        [Parameter(Mandatory = $true)][string]$ApprovedPowerShellExecutable
    )
    $Actions = @($RefillEvidence.actions)
    if ($Actions.Count -ne 1) {
        throw "Running refill task must expose exactly one stable runner action."
    }
    $Action = $Actions[0]
    if ((ConvertTo-ComparablePath -Path ([string]$Action.execute)) -ne (ConvertTo-ComparablePath -Path $ApprovedPowerShellExecutable)) {
        throw "Running refill task executable does not match the approved PowerShell executable."
    }

    $Tokens = @(Split-StrictTaskArguments -Arguments ([string]$Action.arguments))
    $Switches = @{}
    $Values = @{}
    $ValueParameters = @("windowstyle", "executionpolicy", "file", "servicename", "supervisorroot", "hostroot")
    for ($Index = 0; $Index -lt $Tokens.Count; $Index++) {
        $Token = [string]$Tokens[$Index]
        if (-not $Token.StartsWith("-")) {
            throw "Running refill task action contains an unsupported positional command token."
        }
        $Name = $Token.TrimStart("-").ToLowerInvariant()
        if ($Name -eq "command") {
            throw "Running refill task action may not use PowerShell -Command."
        }
        if ($Name -eq "noprofile") {
            if ($Switches.ContainsKey($Name) -or $Values.ContainsKey($Name)) {
                throw "Running refill task action contains duplicate parameter -NoProfile."
            }
            $Switches[$Name] = $true
            continue
        }
        if ($Name -notin $ValueParameters) {
            throw "Running refill task action contains unsupported parameter $Token."
        }
        if ($Values.ContainsKey($Name) -or $Switches.ContainsKey($Name)) {
            throw "Running refill task action contains duplicate parameter $Token."
        }
        if ($Index + 1 -ge $Tokens.Count -or ([string]$Tokens[$Index + 1]).StartsWith("-")) {
            throw "Running refill task action parameter $Token is missing its value."
        }
        $Index += 1
        $Values[$Name] = [string]$Tokens[$Index]
    }
    if (-not $Switches.ContainsKey("noprofile")) {
        throw "Running refill task action requires -NoProfile."
    }
    if ($Values.ContainsKey("windowstyle") -and [string]$Values["windowstyle"] -ne "Hidden") {
        throw "Running refill task action -WindowStyle must be exactly Hidden."
    }
    if ($Values.ContainsKey("executionpolicy") -and [string]$Values["executionpolicy"] -ne "Bypass") {
        throw "Running refill task action -ExecutionPolicy must be exactly Bypass."
    }
    foreach ($Required in @("file", "servicename", "supervisorroot", "hostroot")) {
        if (-not $Values.ContainsKey($Required)) {
            throw "Running refill task action is missing required parameter -$Required."
        }
    }

    $ExpectedRunner = Join-Path $SupervisorRoot "bootstrap\run_windows_managed_service.ps1"
    if ((ConvertTo-ComparablePath -Path ([string]$Values["file"])) -ne (ConvertTo-ComparablePath -Path $ExpectedRunner)) {
        throw "Running refill task action -File target is not the approved stable runner."
    }
    if (-not ([string]$Values["servicename"]).Equals($RefillServiceName, [System.StringComparison]::Ordinal)) {
        throw "Running refill task action -ServiceName must be exactly refill."
    }
    if ((ConvertTo-ComparablePath -Path ([string]$Values["supervisorroot"])) -ne (ConvertTo-ComparablePath -Path $SupervisorRoot)) {
        throw "Running refill task action SupervisorRoot does not match the installed supervisor configuration."
    }
    if ((ConvertTo-ComparablePath -Path ([string]$Values["hostroot"])) -ne (ConvertTo-ComparablePath -Path $HostRoot)) {
        throw "Running refill task action HostRoot does not match the installed supervisor configuration."
    }
}

function Assert-RunningPolicyPausedRefillBaseline {
    param(
        [Parameter(Mandatory = $true)][hashtable]$Evidence,
        [Parameter(Mandatory = $true)][hashtable]$ActiveRelease,
        [Parameter(Mandatory = $true)][string]$HostRoot,
        [Parameter(Mandatory = $true)][string]$SupervisorRoot,
        [Parameter(Mandatory = $true)][string]$ApprovedPowerShellExecutable
    )
    Assert-RefillTaskActionMatchesStableRunner -RefillEvidence $Evidence[$RefillTaskName] -HostRoot $HostRoot -SupervisorRoot $SupervisorRoot -ApprovedPowerShellExecutable $ApprovedPowerShellExecutable

    $ActiveCommit = [string]$ActiveRelease.commit
    $ReleasePath = [string]$ActiveRelease.release_path
    if ($ActiveCommit -notmatch "^[0-9a-f]{40}$") {
        throw "Running refill baseline requires a valid exact active release commit."
    }
    if (-not (Test-Path (Join-Path $ReleasePath "src\msos_autobuilder\refill_controller.py") -PathType Leaf)) {
        throw "Running refill baseline requires active release refill-controller support."
    }

    $PolicyPath = Join-Path (Join-Path $HostRoot "state") "refill-policy.json"
    $PolicyEvidence = Get-JsonFileEvidence -Path $PolicyPath
    if (-not $PolicyEvidence.exists) {
        throw "Running refill baseline requires an existing paused refill policy."
    }
    $Policy = $PolicyEvidence.json
    if ([int]$Policy.version -ne 1) {
        throw "Running refill baseline has unsupported refill policy version."
    }
    if ($Policy.enabled -ne $false) {
        throw "Running refill baseline requires refill policy enabled exactly false."
    }
    if ([int]$Policy.desired_capacity -ne 0) {
        throw "Running refill baseline requires refill policy desired_capacity exactly 0."
    }
    if ($Policy.PSObject.Properties.Name -contains "status" -and [string]$Policy.status -notin @("", "PAUSED", "paused")) {
        throw "Running refill baseline has contradictory paused-policy state."
    }

    $WitnessPath = Join-Path (Join-Path (Join-Path $SupervisorRoot "state") "service-witnesses") "$RefillServiceName.json"
    $WitnessEvidence = Get-JsonFileEvidence -Path $WitnessPath
    if (-not $WitnessEvidence.exists) {
        throw "Running refill baseline requires a fresh refill service witness."
    }
    $Witness = $WitnessEvidence.json
    if ([string]$Witness.state -ne "running") {
        throw "Running refill baseline requires refill witness state running."
    }
    if ([string]$Witness.release_commit -ne $ActiveCommit) {
        throw "Running refill baseline requires refill witness to match the active release commit."
    }
    try {
        $StartedAt = [DateTimeOffset]::Parse([string]$Witness.started_at)
    }
    catch {
        throw "Running refill baseline requires a parseable refill witness started_at."
    }
    $ValidationTime = [DateTimeOffset]::UtcNow
    $StartedAtUtc = $StartedAt.ToUniversalTime()
    $WitnessAge = $ValidationTime - $StartedAtUtc
    if ($WitnessAge.TotalSeconds -lt -$RefillWitnessMaxFutureSkewSeconds) {
        throw "Running refill baseline witness timestamp exceeds the permitted future clock skew."
    }
    if ($WitnessAge.TotalSeconds -gt $RefillWitnessMaxAgeSeconds) {
        throw "Running refill baseline requires a fresh refill service witness."
    }
    $WitnessEvidence["started_at_utc"] = $StartedAtUtc.ToString("o")
    $WitnessEvidence["validated_at_utc"] = $ValidationTime.ToString("o")
    $WitnessEvidence["age_seconds"] = [Math]::Round($WitnessAge.TotalSeconds, 3)
    $WitnessEvidence["max_age_seconds"] = $RefillWitnessMaxAgeSeconds
    $WitnessEvidence["max_future_skew_seconds"] = $RefillWitnessMaxFutureSkewSeconds
    $Report.service_configuration["refill_policy_preflight"] = $PolicyEvidence
    $Report.service_configuration["refill_witness_preflight"] = $WitnessEvidence
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

function Disable-RestoredRefillTaskForRecovery {
    $Existing = Get-ScheduledTask -TaskName $RefillTaskName -ErrorAction SilentlyContinue
    if ($null -eq $Existing) { return "Missing" }
    Stop-ScheduledTask -TaskName $RefillTaskName -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $RefillTaskName -ErrorAction Stop | Out-Null
    return [string](Get-ScheduledTask -TaskName $RefillTaskName -ErrorAction Stop).State
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
    stdout_file.write(json.dumps({
        "phase": "attempt",
        "active_commit": str(active["commit"]),
        "selected_services": [task.service for task in selected],
        "disabled_services": [task.service for task in disabled],
        "health_timeout_seconds": config.health_timeout_seconds,
        "health_poll_seconds": config.health_poll_seconds,
        "configured_stability_seconds": config.health_stability_seconds,
    }, sort_keys=True))
    stdout_file.write("\n")
    stdout_file.flush()
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
        $Lines = @($Stdout -split "`r?`n" | Where-Object { $_.Trim() })
        if ($Lines.Count -gt 0) {
            try { $script:LegacyRestartWitnessAttempt = ($Lines[0] | ConvertFrom-Json) } catch {}
        }
        if ($ExitCode -ne 0) {
            throw "Activated legacy restart witness failed with exit $ExitCode. stdout:`n$Stdout`nstderr:`n$Stderr"
        }
        return ($Lines[-1] | ConvertFrom-Json)
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $ProbePath
        Remove-Item -Force -ErrorAction SilentlyContinue $StdoutPath
        Remove-Item -Force -ErrorAction SilentlyContinue $StderrPath
    }
}

function Test-RestoredLegacyRecoveryWitness {
    param(
        [Parameter(Mandatory = $true)][string]$BootstrapRoot,
        [Parameter(Mandatory = $true)][string]$BootstrapPython
    )
    if (-not (Test-Path $BootstrapPython -PathType Leaf)) {
        throw "Stable supervisor Python not found at $BootstrapPython"
    }
    $ProbePath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-restored-recovery-" + [Guid]::NewGuid().ToString("N") + ".py")
    $StdoutPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-restored-recovery-stdout-" + [Guid]::NewGuid().ToString("N") + ".txt")
    $StderrPath = Join-Path ([System.IO.Path]::GetTempPath()) ("msos-bootstrap-restored-recovery-stderr-" + [Guid]::NewGuid().ToString("N") + ".txt")
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
    spec = importlib.util.spec_from_file_location("restored_self_update_supervisor", module_path)
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
    release_supports_refill = (release_path / "src" / "msos_autobuilder" / "refill_controller.py").is_file()
    selected = []
    disabled = []
    for task in config.managed_tasks:
        if task.service == "refill" and not release_supports_refill:
            disabled.append(task)
        else:
            selected.append(task)
    selected = tuple(selected)
    disabled = tuple(disabled)
    verifier_config = config
    if disabled:
        try:
            import dataclasses
            verifier_config = dataclasses.replace(config, managed_tasks=selected)
        except BaseException:
            verifier_config = config
    controller = module.PowerShellTaskController(config.task_controller_script)
    health = module.FileHealthVerifier(verifier_config, controller)
    selected_names = [task.task_name for task in selected]
    disabled_names = [task.task_name for task in disabled]
    started = datetime.datetime.now(datetime.UTC)
    controller.stop(selected_names)
    controller.start(selected_names)
    health_evidence = health.wait_for(str(active["commit"]), started)
    task_states = dict(health_evidence.get("task_states", {}))
    disabled_service_states = {}
    if disabled_names:
        disabled_task_states = dict(controller.states(disabled_names))
        task_states.update(disabled_task_states)
        for task in disabled:
            state = str(disabled_task_states.get(task.task_name, "Missing"))
            disabled_service_states[task.service] = state
            if state.lower() != "disabled":
                raise RuntimeError(
                    "restored disabled task is not Disabled: "
                    + json.dumps({task.service: state}, sort_keys=True)
                )
    configured_stability = config.health_stability_seconds
    achieved_stability = health_evidence.get("achieved_stability_seconds", configured_stability)
    stdout_file.write(json.dumps({
        "active_commit": str(active["commit"]),
        "selected_services": [task.service for task in selected],
        "disabled_services": [task.service for task in disabled],
        "health_timeout_seconds": config.health_timeout_seconds,
        "health_poll_seconds": config.health_poll_seconds,
        "configured_stability_seconds": config.health_stability_seconds,
        "achieved_stability_seconds": achieved_stability,
        "task_states": task_states,
        "disabled_task_states": disabled_service_states,
        "witnesses": health_evidence.get("witnesses", {}),
        "health": health_evidence,
        "legacy_interface": {
            "release_managed_tasks_helper": hasattr(module, "_release_managed_tasks"),
            "task_controller_disable": hasattr(controller, "disable"),
            "wait_for_arguments": 2,
        },
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
            throw "Restored legacy recovery witness failed with exit $ExitCode. stdout:`n$Stdout`nstderr:`n$Stderr"
        }
        return ($Stdout | ConvertFrom-Json)
    }
    finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $ProbePath
        Remove-Item -Force -ErrorAction SilentlyContinue $StdoutPath
        Remove-Item -Force -ErrorAction SilentlyContinue $StderrPath
    }
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
$LegacyRestartWitnessAttempt = $null
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
    host_root = $HostRoot
    task_namespace = @{
        supplied = $TaskNamespaceWasBound
        namespace = $EffectiveTaskNamespace
        isolated = $IsolatedTaskNamespace
        managed_tasks = @(
            foreach ($Entry in $ResolvedManagedTasks) {
                @{ service = [string]$Entry.service; task_name = [string]$Entry.task }
            }
        )
        update_task_name = $UpdateTaskName
    }
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
    protected_runtime_state = @{ mode = "not_applicable"; allowlist = @($ProtectedRuntimeRelativePaths); before = $null; after = $null; differences = @() }
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
    $SupervisorStateRoot = Join-Path $SupervisorRoot "state"
    $ActivePointerPath = Join-Path $SupervisorStateRoot "active-release.json"
    $PreviousPointerPath = Join-Path $SupervisorStateRoot "previous-release.json"
    $RefillPolicyPath = Join-Path (Join-Path $HostRoot "state") "refill-policy.json"
    $Report.service_configuration["preflight"] = @{
        supervisor = Get-TextFileEvidence -Path $SupervisorConfigPath
        managed_services = Get-TextFileEvidence -Path $ManagedServicesPath
        active_release = Get-ActiveReleaseEvidence -Path $ActivePointerPath
        previous_release = Get-TextFileEvidence -Path $PreviousPointerPath
        refill_policy = Get-TextFileEvidence -Path $RefillPolicyPath
    }
    if (-not $Report.service_configuration["preflight"]["active_release"].exists) {
        throw "Installed managed release pointer is missing; refusing an unbound bootstrap handoff."
    }
    $TaskEvidence = @{}
    foreach ($TaskName in @($ExistingManagedTaskNames + $RefillTaskName)) {
        $TaskEvidence[$TaskName] = Get-ScheduledTaskEvidence -TaskName $TaskName
    }
    $Report.scheduled_tasks["preflight"] = $TaskEvidence
    $ApprovedPowerShellExecutable = $null
    if ($TaskEvidence[$RefillTaskName].exists -and [string]$TaskEvidence[$RefillTaskName].state -eq "Running") {
        $ApprovedPowerShellExecutable = Get-ApprovedPowerShellExecutable -Evidence $TaskEvidence
    }
    $InstalledTaskBaselineMode = Assert-InstalledScheduledTaskBaseline -Evidence $TaskEvidence -ActiveRelease $Report.service_configuration["preflight"]["active_release"] -HostRoot $HostRoot -SupervisorRoot $SupervisorRoot -ApprovedPowerShellExecutable $ApprovedPowerShellExecutable
    $Report.scheduled_tasks["baseline_mode"] = $InstalledTaskBaselineMode
    if ($InstalledTaskBaselineMode -eq "six-task-running-policy-paused") {
        $Report.protected_runtime_state["mode"] = $InstalledTaskBaselineMode
        $Report.protected_runtime_state["before"] = Get-ProtectedRuntimeStateSnapshot -HostRoot $HostRoot
    }

    Test-ReportPathWritable -Path $ReportPath
    Get-BootstrapHashEvidence -RepoRoot $RepoRoot -BootstrapRoot $BootstrapRoot -OldCommit $ExpectedOldBootstrapCommit -NewCommit $Commit -Evidence $Report.file_hashes

    New-Item -ItemType Directory -Force -Path $StageParent, $RollbackParent, $ReportsRoot | Out-Null
    Copy-Item -Recurse -Force -Path $BootstrapRoot -Destination $StagedBootstrap
    Copy-BootstrapSourceFiles -RepoRoot $RepoRoot -DestinationRoot $StagedBootstrap -Evidence $Report.file_hashes
    Add-StagedServiceConfiguration -InstalledBootstrap $BootstrapRoot -StagedBootstrap $StagedBootstrap -BootstrapPython $BootstrapPython -ExpectedTaskNames $ExpectedBaselineTaskNames -Evidence $Report.service_configuration
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
    $ExpectedRefillState = if ($InstalledTaskBaselineMode -eq "six-task-running-policy-paused") { "Running" } else { "Disabled" }
    if ([string]$Report.scheduled_tasks["staged_refill"].state -ne $ExpectedRefillState) {
        throw "Refill task must remain exactly $ExpectedRefillState for $InstalledTaskBaselineMode."
    }
    if ($InstalledTaskBaselineMode -ne "six-task-running-policy-paused" -and [string]$Report.scheduled_tasks["staged_refill"].state -ne "Disabled") {
        throw "Refill task must remain exactly Disabled until a later managed-release cutover."
    }
    if ($InstalledTaskBaselineMode -eq "six-task-running-policy-paused") {
        Assert-RefillTaskActionMatchesStableRunner -RefillEvidence $Report.scheduled_tasks["staged_refill"] -HostRoot $HostRoot -SupervisorRoot $SupervisorRoot -ApprovedPowerShellExecutable $ApprovedPowerShellExecutable
    }
    else {
        $RefillAction = @($Report.scheduled_tasks["staged_refill"].actions)[0]
        if ($null -eq $RefillAction -or (ConvertTo-ComparablePath -Path ([string]$RefillAction.arguments)) -notlike ("*" + (ConvertTo-ComparablePath -Path $HostRoot) + "*")) {
            throw "Refill task HostRoot does not match the installed supervisor configuration."
        }
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
        $Report.activation = @{
            attempted = $true
            performed = $true
            activated_bootstrap = $BootstrapRoot
            activation_backup = $ActivationBackup
            activated_hashes_verified = $true
            service_configuration_verified = $true
            refill_task_state = [string](Get-ScheduledTask -TaskName $RefillTaskName -ErrorAction Stop).State
            selected_services = @()
            disabled_services = @()
            legacy_restart_witness_started = $false
            live_task_mutation_touched = $false
        }
        $LegacyRestartWitnessStarted = $true
        $Report.activation["legacy_restart_witness_started"] = $true
        $Report.activation["live_task_mutation_touched"] = $true
        $RestartWitness = Test-ActivatedLegacyRestartWitness -BootstrapRoot $BootstrapRoot -BootstrapPython $BootstrapPython
        $Report.activation["selected_services"] = @($RestartWitness.selected_services)
        $Report.activation["disabled_services"] = @($RestartWitness.disabled_services)
        $Report.activation["legacy_restart_witness"] = $RestartWitness
        if ($InstalledTaskBaselineMode -eq "six-task-running-policy-paused") {
            $PostRefillEvidence = Get-ScheduledTaskEvidence -TaskName $RefillTaskName
            $Report.scheduled_tasks["post_handoff_refill"] = $PostRefillEvidence
            Assert-RefillTaskActionMatchesStableRunner -RefillEvidence $PostRefillEvidence -HostRoot $HostRoot -SupervisorRoot $SupervisorRoot -ApprovedPowerShellExecutable $ApprovedPowerShellExecutable
            try {
                $ProtectedAfter = Get-ProtectedRuntimeStateSnapshot -HostRoot $HostRoot
                $Report.protected_runtime_state["after"] = $ProtectedAfter
                $ProtectedDifferences = @(Compare-ProtectedRuntimeStateSnapshots -Before $Report.protected_runtime_state["before"] -After $ProtectedAfter)
                $Report.protected_runtime_state["differences"] = $ProtectedDifferences
            }
            catch {
                $Report.protected_runtime_state["snapshot_error"] = $_.Exception.Message
                throw
            }
            if ($ProtectedDifferences.Count -gt 0) {
                throw "Protected runtime state changed during policy-paused bootstrap handoff."
            }
            $PostActiveRelease = Get-TextFileEvidence -Path $ActivePointerPath
            $PostPreviousRelease = Get-TextFileEvidence -Path $PreviousPointerPath
            $PostRefillPolicy = Get-TextFileEvidence -Path $RefillPolicyPath
            Assert-TextFileEvidenceUnchanged `
                -Before $Report.service_configuration["preflight"]["active_release"] `
                -After $PostActiveRelease `
                -Label "active-release.json"
            Assert-TextFileEvidenceUnchanged `
                -Before $Report.service_configuration["preflight"]["previous_release"] `
                -After $PostPreviousRelease `
                -Label "previous-release.json"
            Assert-TextFileEvidenceUnchanged `
                -Before $Report.service_configuration["preflight"]["refill_policy"] `
                -After $PostRefillPolicy `
                -Label "refill-policy.json"
            $Report.service_configuration["post_handoff_invariants"] = @{
                active_release = $PostActiveRelease
                previous_release = $PostPreviousRelease
                refill_policy = $PostRefillPolicy
            }
            if ([string](Get-ScheduledTask -TaskName $RefillTaskName -ErrorAction Stop).State -ne "Running") {
                throw "Policy-paused refill task did not return to Running."
            }
            $RefillWitness = $RestartWitness.health.witnesses.refill
            if ($null -eq $RefillWitness -or [string]$RefillWitness.release_commit -ne [string]$Report.service_configuration["preflight"]["active_release"].commit -or [string]$RefillWitness.state -ne "running") {
                throw "Policy-paused refill witness is not fresh and bound to the unchanged active release."
            }
        }
        $Report.activation["status"] = "restart_witness_passed"
        $Report.outcome = "success"
    }
    catch {
        $ActivationError = $_.Exception.Message
        if ($LegacyRestartWitnessStarted) {
            $Report.activation["legacy_restart_witness_error"] = $ActivationError
            $Report.activation["status"] = "restart_witness_failed"
            if ($null -ne $LegacyRestartWitnessAttempt) {
                $Report.activation["active_commit"] = [string]$LegacyRestartWitnessAttempt.active_commit
                $Report.activation["selected_services"] = @($LegacyRestartWitnessAttempt.selected_services)
                $Report.activation["disabled_services"] = @($LegacyRestartWitnessAttempt.disabled_services)
                $Report.activation["health_timeout_seconds"] = $LegacyRestartWitnessAttempt.health_timeout_seconds
                $Report.activation["health_poll_seconds"] = $LegacyRestartWitnessAttempt.health_poll_seconds
                $Report.activation["configured_stability_seconds"] = $LegacyRestartWitnessAttempt.configured_stability_seconds
            }
        }
        Restore-HandoffState -Reason $ActivationError
        if ($LegacyRestartWitnessStarted) {
            try {
                $RecoveryRefillState = Disable-RestoredRefillTaskForRecovery
                $Recovery = Test-RestoredLegacyRecoveryWitness -BootstrapRoot $BootstrapRoot -BootstrapPython $BootstrapPython
                $Recovery | Add-Member -NotePropertyName "outer_refill_disable_state" -NotePropertyValue $RecoveryRefillState -Force
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
