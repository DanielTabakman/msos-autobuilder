[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$TaskName = "MSOS Autobuilder Result Relay",
    [string]$RepoUrl = "https://github.com/DanielTabakman/msos-autobuilder.git",
    [string]$Branch = "results",
    [string]$MachineId = $env:COMPUTERNAME,
    [int]$PollSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($Branch -in @("main", "master")) {
    throw "The result relay may not target main or master."
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Bootstrap = Join-Path $PSScriptRoot "bootstrap_windows_codex_host_auto.ps1"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LogRoot = Join-Path $HostRoot "logs"
$LogFile = Join-Path $LogRoot "results-relay.log"
$RunnerScript = Join-Path $HostRoot "run-results-relay.ps1"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Preparing the Autobuilder Python environment..." -ForegroundColor Cyan
    & $Bootstrap -HostRoot $HostRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Autobuilder bootstrap failed."
    }
}

& $VenvPython -m pip install -e $RepoRoot | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the updated Autobuilder package."
}

New-Item -ItemType Directory -Force -Path $HostRoot, $LogRoot | Out-Null

Write-Host "Relaying existing completed jobs once in the foreground..." -ForegroundColor Cyan
$PreviousPrompt = $env:GIT_TERMINAL_PROMPT
$env:GIT_TERMINAL_PROMPT = "1"
try {
    & $VenvPython -m msos_autobuilder.results_relay `
        --host-root $HostRoot `
        --repo-url $RepoUrl `
        --branch $Branch `
        --machine-id $MachineId `
        --poll-seconds $PollSeconds `
        --once | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "The foreground result relay failed. Git may need one-time authentication."
    }
}
finally {
    if ($null -eq $PreviousPrompt) {
        Remove-Item Env:\GIT_TERMINAL_PROMPT -ErrorAction SilentlyContinue
    }
    else {
        $env:GIT_TERMINAL_PROMPT = $PreviousPrompt
    }
}

$RunnerContent = @"
Set-StrictMode -Version Latest
`$ErrorActionPreference = "Continue"
`$env:PYTHONUTF8 = "1"
`$env:GIT_TERMINAL_PROMPT = "0"
New-Item -ItemType Directory -Force -Path "$LogRoot" | Out-Null
& "$VenvPython" -m msos_autobuilder.results_relay --host-root "$HostRoot" --repo-url "$RepoUrl" --branch "$Branch" --machine-id "$MachineId" --poll-seconds $PollSeconds *>> "$LogFile"
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
    -Description "Review-only MSOS Autobuilder result relay" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

Write-Host "Autobuilder result relay installed and started." -ForegroundColor Green
Write-Host "Task: $TaskName"
Write-Host "Branch: $Branch"
Write-Host "Log: $LogFile"
Write-Host "The relay can push review artifacts only; product publication remains disabled."
