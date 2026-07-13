[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$TaskName = "MSOS Autobuilder Controlled Publisher",
    [string]$EvidenceRepoUrl = "https://github.com/DanielTabakman/msos-autobuilder.git",
    [string]$ResultsBranch = "results",
    [string]$ProductRepoUrl = "https://github.com/DanielTabakman/Probability-prediction-engine.git",
    [string]$ProductRepoFullName = "DanielTabakman/Probability-prediction-engine",
    [string]$ProductBaseBranch = "main",
    [string]$MachineId = $env:COMPUTERNAME,
    [string]$WitnessJobId = "ppe-frozen-evaluation-contract-v1-revision-1",
    [int]$PollSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($ResultsBranch -in @("main", "master")) {
    throw "The controlled publisher requires a dedicated evidence branch."
}
if ($ProductBaseBranch -notin @("main", "master")) {
    throw "The product base branch must be main or master."
}

function Convert-ToYamlQuoted {
    param([Parameter(Mandatory = $true)][string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function Convert-ToForwardSlash {
    param([Parameter(Mandatory = $true)][string]$Value)
    return $Value.Replace("\", "/")
}

function Stop-TaskIfPresent {
    param([Parameter(Mandatory = $true)][string]$Name)
    $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($Task) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    }
}

function Start-TaskIfPresent {
    param([Parameter(Mandatory = $true)][string]$Name)
    $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($Task) {
        Enable-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue | Out-Null
        Start-ScheduledTask -TaskName $Name
    }
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Bootstrap = Join-Path $PSScriptRoot "bootstrap_windows_codex_host_auto.ps1"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$HostConfig = Join-Path $HostRoot "host.yaml"
$PublisherConfig = Join-Path $HostRoot "controlled-publisher.yaml"
$LogRoot = Join-Path $HostRoot "logs"
$LogFile = Join-Path $LogRoot "controlled-publisher.log"
$RunnerScript = Join-Path $HostRoot "run-controlled-publisher.ps1"
$StateRoot = Join-Path $HostRoot "state"
$WriterOwnerPath = Join-Path $StateRoot "product-writer-owner.json"
$IdentityWitness = Join-Path $RepoRoot "scripts\check_frozen_evaluation_candidate.py"
$SchemaWitness = Join-Path $RepoRoot "scripts\check_frozen_evaluation_schema_compatibility.py"

$ManagedTasks = @(
    "MSOS Autobuilder Host",
    "MSOS Autobuilder Result Relay",
    "MSOS Autobuilder Candidate Gate",
    "MSOS Autobuilder Revision Loop",
    $TaskName
)


if (-not (Test-Path $VenvPython)) {
    & $Bootstrap -HostRoot $HostRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Autobuilder bootstrap failed."
    }
}
if (-not (Test-Path $HostConfig)) {
    throw "Codex host config not found at $HostConfig"
}
if (-not (Test-Path $IdentityWitness)) {
    throw "Identity witness not found at $IdentityWitness"
}
if (-not (Test-Path $SchemaWitness)) {
    throw "Schema witness not found at $SchemaWitness"
}

$EditableSpec = "$RepoRoot[dev]"
& $VenvPython -m pip install -e $EditableSpec | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the updated Autobuilder package."
}

New-Item -ItemType Directory -Force -Path $HostRoot, $LogRoot, $StateRoot | Out-Null

if (Test-Path $WriterOwnerPath) {
    $Owner = Get-Content -Path $WriterOwnerPath -Raw | ConvertFrom-Json
    if ([string]$Owner.owner -ne "controlled-draft-publisher-v1") {
        throw "Another product writer owns the writer marker: $($Owner.owner)"
    }
}
else {
    $WriterOwner = @{
        version = 1
        owner = "controlled-draft-publisher-v1"
        task_name = $TaskName
        product_repo = $ProductRepoFullName
        draft_only = $true
        merge_enabled = $false
        main_write_enabled = $false
        recorded_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
    $WriterOwnerJson = ($WriterOwner | ConvertTo-Json -Depth 10) + [Environment]::NewLine
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($WriterOwnerPath, $WriterOwnerJson, $Utf8NoBom)
}

$HostRootYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $HostRoot)
$EvidenceRepoYaml = Convert-ToYamlQuoted $EvidenceRepoUrl
$ResultsBranchYaml = Convert-ToYamlQuoted $ResultsBranch
$ProductRepoYaml = Convert-ToYamlQuoted $ProductRepoUrl
$ProductFullNameYaml = Convert-ToYamlQuoted $ProductRepoFullName
$ProductBaseYaml = Convert-ToYamlQuoted $ProductBaseBranch
$MachineIdYaml = Convert-ToYamlQuoted $MachineId
$PythonYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $VenvPython)
$IdentityYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $IdentityWitness)
$SchemaYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $SchemaWitness)
$WitnessYaml = Convert-ToYamlQuoted $WitnessJobId
$BranchYaml = Convert-ToYamlQuoted "autobuilder/$WitnessJobId"
$TitleYaml = Convert-ToYamlQuoted "PPE: harden frozen evaluation contract"
$CommitYaml = Convert-ToYamlQuoted "PPE: harden frozen evaluation contract"

$PublisherYaml = @"
version: 1
draft_pr_publication_enabled: true
merge_enabled: false
main_write_enabled: false
host_root: $HostRootYaml
evidence_repo_url: $EvidenceRepoYaml
results_branch: $ResultsBranchYaml
product_repo_url: $ProductRepoYaml
product_repo_full_name: $ProductFullNameYaml
product_base_branch: $ProductBaseYaml
machine_id: $MachineIdYaml
poll_seconds: $PollSeconds
plans:
  $WitnessYaml:
    branch: $BranchYaml
    title: $TitleYaml
    commit_message: $CommitYaml
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
Set-Content -Path $PublisherConfig -Value $PublisherYaml -Encoding UTF8

$RunnerContent = @"
Set-StrictMode -Version Latest
`$ErrorActionPreference = "Continue"
`$env:PYTHONUTF8 = "1"
`$env:GIT_TERMINAL_PROMPT = "0"
`$env:GIT_CONFIG_COUNT = "1"
`$env:GIT_CONFIG_KEY_0 = "core.autocrlf"
`$env:GIT_CONFIG_VALUE_0 = "false"
New-Item -ItemType Directory -Force -Path "$LogRoot" | Out-Null
& "$VenvPython" -m msos_autobuilder.controlled_publisher --config "$PublisherConfig" *>> "$LogFile"
exit `$LASTEXITCODE
"@
Set-Content -Path $RunnerScript -Value $RunnerContent -Encoding UTF8

$CutoverSucceeded = $false
try {
    foreach ($Name in $ManagedTasks) {
        Stop-TaskIfPresent -Name $Name
    }

    # Cut over from any stale in-product operator process/task before enabling the extracted writer.
    $LegacyPattern = "Probability-prediction-engine.*(autobuilder|operator|supervisor)|(autobuilder|operator|supervisor).*Probability-prediction-engine"
    $LegacyTasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
        $_.TaskName -notin $ManagedTasks -and
        ((@($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join " ") -match $LegacyPattern)
    }
    foreach ($Task in @($LegacyTasks)) {
        Write-Host "Disabling legacy product writer task: $($Task.TaskName)" -ForegroundColor Yellow
        Stop-ScheduledTask -TaskName $Task.TaskName -ErrorAction SilentlyContinue
        Disable-ScheduledTask -TaskName $Task.TaskName -ErrorAction SilentlyContinue | Out-Null
    }

    $CurrentPid = $PID
    $LegacyProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessId -ne $CurrentPid -and
        $null -ne $_.CommandLine -and
        $_.CommandLine -match $LegacyPattern
    }
    foreach ($Process in @($LegacyProcesses)) {
        Write-Host "Stopping legacy product writer process $($Process.ProcessId)" -ForegroundColor Yellow
        Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
    }

    # The legacy in-product publisher remains fail-closed even if an old process is restarted.
    $env:PPE_GIT_AUTONOMOUS_WRITES = $null
    $env:PPE_ALLOW_LEGACY_GIT_PUBLISH = $null
    [Environment]::SetEnvironmentVariable("PPE_GIT_AUTONOMOUS_WRITES", $null, "User")
    [Environment]::SetEnvironmentVariable("PPE_ALLOW_LEGACY_GIT_PUBLISH", $null, "User")

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Write-Host "Running the controlled publisher once in the foreground..." -ForegroundColor Cyan
$PreviousPrompt = $env:GIT_TERMINAL_PROMPT
$PreviousGitConfigCount = $env:GIT_CONFIG_COUNT
$PreviousGitConfigKey = $env:GIT_CONFIG_KEY_0
$PreviousGitConfigValue = $env:GIT_CONFIG_VALUE_0
$env:GIT_TERMINAL_PROMPT = "1"
$env:GIT_CONFIG_COUNT = "1"
$env:GIT_CONFIG_KEY_0 = "core.autocrlf"
$env:GIT_CONFIG_VALUE_0 = "false"
try {
    & $VenvPython -m msos_autobuilder.controlled_publisher `
        --config $PublisherConfig `
        --once | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "The foreground controlled publisher failed."
    }
}
finally {
    if ($null -eq $PreviousPrompt) {
        Remove-Item Env:\GIT_TERMINAL_PROMPT -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_TERMINAL_PROMPT = $PreviousPrompt
    }
    if ($null -eq $PreviousGitConfigCount) {
        Remove-Item Env:\GIT_CONFIG_COUNT -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_CONFIG_COUNT = $PreviousGitConfigCount
    }
    if ($null -eq $PreviousGitConfigKey) {
        Remove-Item Env:\GIT_CONFIG_KEY_0 -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_CONFIG_KEY_0 = $PreviousGitConfigKey
    }
    if ($null -eq $PreviousGitConfigValue) {
        Remove-Item Env:\GIT_CONFIG_VALUE_0 -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_CONFIG_VALUE_0 = $PreviousGitConfigValue
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
    -Description "Single-writer draft-only MSOS product PR publisher" `
    -Force | Out-Null

    $CutoverSucceeded = $true
}
finally {
    # Existing builder tasks are restarted even when the publisher witness fails.
    foreach ($Name in $ManagedTasks) {
        Start-TaskIfPresent -Name $Name
    }
}

if (-not $CutoverSucceedd) {
    throw "Controlled publisher cutover did not complete."
}

Write-Host "Controlled publisher installed and the Autobuilder machine is running." -ForegroundColor Green
Write-Host "Task: $TaskName"
Write-Host "Config: $PublisherConfig"
Write-Host "Log: $LogFile"
Write-Host "Writer owner: $WriterOwnerPath"
Write-Host "Draft PR publication is enabled; merge and product-main writes remain disabled."
