[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$TaskName = "MSOS Autobuilder Capacity-One Refill",
    [int]$IntervalSeconds = 30,
    [int]$MaxHostHeartbeatAgeSeconds = 300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Bootstrap = Join-Path $PSScriptRoot "bootstrap_windows_codex_host_auto.ps1"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ServiceConfig = Join-Path $HostRoot "service.yaml"
$LogRoot = Join-Path $HostRoot "logs"
$LogFile = Join-Path $LogRoot "capacity-one-refill.log"
$RunnerScript = Join-Path $HostRoot "run-capacity-one-refill.ps1"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Preparing the Autobuilder Python environment..." -ForegroundColor Cyan
    & $Bootstrap -HostRoot $HostRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Autobuilder bootstrap failed."
    }
}

if (-not (Test-Path $ServiceConfig)) {
    throw "Persistent host service config not found: $ServiceConfig"
}

& $VenvPython -m pip install -e $RepoRoot | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the updated Autobuilder package."
}

New-Item -ItemType Directory -Force -Path $HostRoot, $LogRoot | Out-Null

Write-Host "Checking refill controller status once in the foreground..." -ForegroundColor Cyan
& $VenvPython -m msos_autobuilder refill-service-status `
    --service-config $ServiceConfig `
    --max-host-heartbeat-age-seconds $MaxHostHeartbeatAgeSeconds | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "The refill service status command failed."
}

$RunnerContent = @"
Set-StrictMode -Version Latest
`$ErrorActionPreference = "Continue"
`$env:PYTHONUTF8 = "1"
`$env:GIT_TERMINAL_PROMPT = "0"
New-Item -ItemType Directory -Force -Path "$LogRoot" | Out-Null
& "$VenvPython" -m msos_autobuilder refill-run --service-config "$ServiceConfig" --interval-seconds $IntervalSeconds --max-host-heartbeat-age-seconds $MaxHostHeartbeatAgeSeconds *>> "$LogFile"
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
    -Description "Capacity-one MSOS Autobuilder refill controller using build-next only" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

Write-Host "Capacity-one refill controller installed and started." -ForegroundColor Green
Write-Host "Task: $TaskName"
Write-Host "Service config: $ServiceConfig"
Write-Host "Status: $HostRoot\state\refill-status.json"
Write-Host "Policy: $HostRoot\state\refill-policy.json"
Write-Host "Log: $LogFile"
Write-Host "Capacity remains hard-bounded to one; dispatch is delegated to build-next."
