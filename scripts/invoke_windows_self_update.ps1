[CmdletBinding()]
param(
    [string]$SupervisorRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder-supervisor"),
    [Parameter(Mandatory = $true)][string]$ManifestUrl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$BootstrapPython = Join-Path $SupervisorRoot "bootstrap-venv\Scripts\python.exe"
$SupervisorModule = Join-Path $SupervisorRoot "bootstrap\self_update_supervisor.py"
$ConfigPath = Join-Path $SupervisorRoot "bootstrap\supervisor.yaml"
$Inbox = Join-Path $SupervisorRoot "inbox"
$LogRoot = Join-Path $SupervisorRoot "logs"
$LogPath = Join-Path $LogRoot "self-update.log"
New-Item -ItemType Directory -Force -Path $Inbox, $LogRoot | Out-Null

if (-not (Test-Path $BootstrapPython -PathType Leaf)) {
    throw "Stable supervisor Python not found at $BootstrapPython"
}
if (-not (Test-Path $SupervisorModule -PathType Leaf)) {
    throw "Stable supervisor module not found at $SupervisorModule"
}

$Temporary = Join-Path $Inbox ("manifest-" + [Guid]::NewGuid().ToString("N") + ".tmp")
$Manifest = Join-Path $Inbox "approved-update.yaml"
try {
    Invoke-WebRequest -UseBasicParsing -Uri $ManifestUrl -OutFile $Temporary
    Move-Item -Force -Path $Temporary -Destination $Manifest
    & $BootstrapPython $SupervisorModule apply --config $ConfigPath --manifest $Manifest *>> $LogPath
    exit $LASTEXITCODE
}
finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $Temporary
}
