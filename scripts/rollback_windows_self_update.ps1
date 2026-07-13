[CmdletBinding()]
param(
    [string]$SupervisorRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder-supervisor")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$BootstrapPython = Join-Path $SupervisorRoot "bootstrap-venv\Scripts\python.exe"
$SupervisorModule = Join-Path $SupervisorRoot "bootstrap\self_update_supervisor.py"
$ConfigPath = Join-Path $SupervisorRoot "bootstrap\supervisor.yaml"
& $BootstrapPython $SupervisorModule rollback --config $ConfigPath
exit $LASTEXITCODE
