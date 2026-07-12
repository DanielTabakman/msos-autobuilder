[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$TaskName = "MSOS Autobuilder Host",
    [string]$FeedRepoUrl = "https://github.com/DanielTabakman/msos-autobuilder.git",
    [string]$FeedBranch = "jobs",
    [string]$FeedPath = "jobs/approved",
    [int]$PollSeconds = 10,
    [int]$FeedRefreshSeconds = 30,
    [string]$WaitForJobId = "persistent-host-smoke-v1",
    [int]$WaitMinutes = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

Write-Host "Preparing the Codex host and Python environment..." -ForegroundColor Cyan
& $Bootstrap -HostRoot $HostRoot
if (-not (Test-Path $VenvPython)) {
    throw "Autobuilder Python environment was not created at $VenvPython"
}

$ServiceConfig = Join-Path $HostRoot "service.yaml"
$CodexHostConfig = Join-Path $HostRoot "host.yaml"
$LogRoot = Join-Path $HostRoot "logs"
$LogFile = Join-Path $LogRoot "persistent-host.log"
$RunnerScript = Join-Path $HostRoot "run-persistent-host.ps1"

New-Item -ItemType Directory -Force -Path $HostRoot, $LogRoot | Out-Null

$HostRootYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $HostRoot)
$CodexHostYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $CodexHostConfig)
$FeedRepoYaml = Convert-ToYamlQuoted $FeedRepoUrl
$FeedBranchYaml = Convert-ToYamlQuoted $FeedBranch
$FeedPathYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $FeedPath)

$ServiceYaml = @"
version: 1
publication_enabled: false
host_root: $HostRootYaml
codex_host_config: $CodexHostYaml
poll_seconds: $PollSeconds
heartbeat_seconds: 5
job_feed:
  enabled: true
  repo_url: $FeedRepoYaml
  branch: $FeedBranchYaml
  path: $FeedPathYaml
  refresh_seconds: $FeedRefreshSeconds
"@
Set-Content -Path $ServiceConfig -Value $ServiceYaml -Encoding UTF8

& $VenvPython -m msos_autobuilder host-init --service-config $ServiceConfig | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Persistent host initialization failed."
}

$RunnerContent = @"
Set-StrictMode -Version Latest
`$ErrorActionPreference = "Continue"
`$env:PYTHONUTF8 = "1"
New-Item -ItemType Directory -Force -Path "$LogRoot" | Out-Null
& "$VenvPython" -m msos_autobuilder host-run --service-config "$ServiceConfig" *>> "$LogFile"
exit `$LASTEXITCODE
"@
Set-Content -Path $RunnerScript -Value $RunnerContent -Encoding UTF8

$PowerShellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
$TaskArgument = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunnerScript`""
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

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
    -Description "Persistent approval-gated MSOS Autobuilder host" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

Write-Host "Persistent Autobuilder host installed and started." -ForegroundColor Green
Write-Host "Task: $TaskName"
Write-Host "Service config: $ServiceConfig"
Write-Host "Status: $HostRoot\state\host-status.json"
Write-Host "Log: $LogFile"
Write-Host "Completed jobs: $HostRoot\queue\completed"
Write-Host "Failed jobs: $HostRoot\queue\failed"

& $VenvPython -m msos_autobuilder host-status --service-config $ServiceConfig | Out-Host

if ($WaitForJobId) {
    $CompletedReport = Join-Path $HostRoot "queue\completed\$WaitForJobId\report.json"
    $FailedReport = Join-Path $HostRoot "queue\failed\$WaitForJobId\error.json"
    $Deadline = (Get-Date).AddMinutes($WaitMinutes)
    Write-Host "Waiting for background job '$WaitForJobId' (up to $WaitMinutes minutes)..." `
        -ForegroundColor Cyan

    while ((Get-Date) -lt $Deadline) {
        if (Test-Path $CompletedReport) {
            Write-Host "Background smoke job completed successfully." -ForegroundColor Green
            Get-Content -Path $CompletedReport | Out-Host
            return
        }
        if (Test-Path $FailedReport) {
            Get-Content -Path $FailedReport | Out-Host
            throw "Background smoke job failed. See $FailedReport"
        }
        Start-Sleep -Seconds 5
    }

    Write-Warning "Timed out waiting for '$WaitForJobId'. The host remains installed and running."
    & $VenvPython -m msos_autobuilder host-status --service-config $ServiceConfig | Out-Host
    Write-Host "Inspect the host log at $LogFile"
}
