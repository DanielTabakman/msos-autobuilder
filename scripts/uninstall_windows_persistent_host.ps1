[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$TaskName = "MSOS Autobuilder Host",
    [switch]$RemoveRuntimeData
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$ServiceConfig = Join-Path $HostRoot "service.yaml"
$RunnerScript = Join-Path $HostRoot "run-persistent-host.ps1"

if ((Test-Path $VenvPython) -and (Test-Path $ServiceConfig)) {
    & $VenvPython -m msos_autobuilder host-stop --service-config $ServiceConfig | Out-Host
    Start-Sleep -Seconds 2
}

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Remove-Item -Path $RunnerScript -Force -ErrorAction SilentlyContinue

if ($RemoveRuntimeData -and (Test-Path $HostRoot)) {
    Remove-Item -Path $HostRoot -Recurse -Force
    Write-Host "Persistent host and runtime data removed." -ForegroundColor Green
}
else {
    Write-Host "Persistent host task removed. Runtime evidence was preserved at $HostRoot" `
        -ForegroundColor Green
}
