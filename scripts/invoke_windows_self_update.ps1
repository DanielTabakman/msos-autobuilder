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
$StateRoot = Join-Path $SupervisorRoot "state"
$LogRoot = Join-Path $SupervisorRoot "logs"
$LogPath = Join-Path $LogRoot "self-update.log"
New-Item -ItemType Directory -Force -Path $Inbox, $StateRoot, $LogRoot | Out-Null

if (-not (Test-Path $BootstrapPython -PathType Leaf)) {
    throw "Stable supervisor Python not found at $BootstrapPython"
}
if (-not (Test-Path $SupervisorModule -PathType Leaf)) {
    throw "Stable supervisor module not found at $SupervisorModule"
}

$Temporary = Join-Path $Inbox ("manifest-" + [Guid]::NewGuid().ToString("N") + ".tmp")
$Manifest = Join-Path $Inbox "approved-update.yaml"
$SeenManifestHash = Join-Path $StateRoot "last-successful-manifest.sha256"
try {
    Invoke-WebRequest -UseBasicParsing -Uri $ManifestUrl -OutFile $Temporary
    $DownloadedHash = (Get-FileHash -Path $Temporary -Algorithm SHA256).Hash.ToLowerInvariant()
    if (Test-Path $SeenManifestHash -PathType Leaf) {
        $SeenHash = (Get-Content -Path $SeenManifestHash -Raw).Trim().ToLowerInvariant()
        if ($SeenHash -eq $DownloadedHash) {
            exit 0
        }
    }
    Move-Item -Force -Path $Temporary -Destination $Manifest
    & $BootstrapPython $SupervisorModule apply --config $ConfigPath --manifest $Manifest *>> $LogPath
    $ApplyExitCode = $LASTEXITCODE
    if ($ApplyExitCode -eq 0) {
        $HashTemporary = Join-Path $StateRoot (".last-successful-manifest." + [Guid]::NewGuid().ToString("N") + ".tmp")
        try {
            [System.IO.File]::WriteAllText($HashTemporary, $DownloadedHash + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
            Move-Item -Force -Path $HashTemporary -Destination $SeenManifestHash
        }
        finally {
            Remove-Item -Force -ErrorAction SilentlyContinue $HashTemporary
        }
    }
    exit $ApplyExitCode
}
finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $Temporary
}
