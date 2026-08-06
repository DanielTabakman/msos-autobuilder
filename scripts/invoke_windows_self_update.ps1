[CmdletBinding()]
param(
    [string]$SupervisorRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder-supervisor"),
    [Parameter(Mandatory = $true)][string]$ManifestUrl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$BootstrapPython = Join-Path $SupervisorRoot "bootstrap-venv\Scripts\python.exe"
$SupervisorModule = Join-Path $SupervisorRoot "bootstrap\self_update_supervisor.py"
$EvidenceRelayModule = Join-Path $SupervisorRoot "bootstrap\self_update_evidence_relay.py"
$ConfigPath = Join-Path $SupervisorRoot "bootstrap\supervisor.yaml"
$Inbox = Join-Path $SupervisorRoot "inbox"
$StateRoot = Join-Path $SupervisorRoot "state"
$LogRoot = Join-Path $SupervisorRoot "logs"
$LogPath = Join-Path $LogRoot "self-update.log"
New-Item -ItemType Directory -Force -Path $Inbox, $StateRoot, $LogRoot | Out-Null

$UpdatePolicyPath = Join-Path $StateRoot "update-supervisor-policy.json"
if (Test-Path -LiteralPath $UpdatePolicyPath -PathType Leaf) {
    try {
        $UpdatePolicy = Get-Content -LiteralPath $UpdatePolicyPath -Raw | ConvertFrom-Json
    }
    catch {
        throw "Update supervisor policy is malformed at $UpdatePolicyPath"
    }
    $AutonomousEnabled = $false
    if ($null -ne $UpdatePolicy.PSObject.Properties["autonomous_installation_enabled"]) {
        $AutonomousEnabled = [bool]$UpdatePolicy.autonomous_installation_enabled
    }
    $Mode = [string]($UpdatePolicy.mode)
    if (-not $AutonomousEnabled -or $Mode -eq "disabled-idle") {
        $IdlePayload = @{
            version = 1
            status = "completed"
            mode = "disabled-idle"
            autonomous_installation_enabled = $false
            update_attempted = $false
            installation_attempted = $false
            message = "Update supervisor is intentionally disabled/idle; no update or installation was attempted."
        } | ConvertTo-Json -Compress
        Add-Content -LiteralPath $LogPath -Value $IdlePayload -Encoding utf8
        Write-Output $IdlePayload
        exit 0
    }
}

if (-not (Test-Path $BootstrapPython -PathType Leaf)) {
    throw "Stable supervisor Python not found at $BootstrapPython"
}
if (-not (Test-Path $SupervisorModule -PathType Leaf)) {
    throw "Stable supervisor module not found at $SupervisorModule"
}
if (-not (Test-Path $EvidenceRelayModule -PathType Leaf)) {
    throw "Stable self-update evidence relay not found at $EvidenceRelayModule"
}

function Invoke-EvidenceRelay {
    & $BootstrapPython $EvidenceRelayModule --config $ConfigPath *>> $LogPath
    return $LASTEXITCODE
}

# Retry any locally durable evidence before manifest deduplication. A completed update must not
# become invisible merely because the previous Git push was interrupted or temporarily offline.
$InitialRelayExitCode = Invoke-EvidenceRelay

$Temporary = Join-Path $Inbox ("manifest-" + [Guid]::NewGuid().ToString("N") + ".tmp")
$Manifest = Join-Path $Inbox "approved-update.yaml"
$SeenManifestHash = Join-Path $StateRoot "last-successful-manifest.sha256"
try {
    Invoke-WebRequest -UseBasicParsing -Uri $ManifestUrl -OutFile $Temporary
    $DownloadedHash = (Get-FileHash -Path $Temporary -Algorithm SHA256).Hash.ToLowerInvariant()
    if (Test-Path $SeenManifestHash -PathType Leaf) {
        $SeenHash = (Get-Content -Path $SeenManifestHash -Raw).Trim().ToLowerInvariant()
        if ($SeenHash -eq $DownloadedHash) {
            exit $InitialRelayExitCode
        }
    }
    Move-Item -Force -Path $Temporary -Destination $Manifest
    & $BootstrapPython $SupervisorModule apply --config $ConfigPath --manifest $Manifest *>> $LogPath
    $ApplyExitCode = $LASTEXITCODE

    # Relay the report produced by this attempt even when the update failed. The local immutable
    # evidence remains authoritative; the results branch is the automatic review/notification feed.
    $RelayExitCode = Invoke-EvidenceRelay
    if ($ApplyExitCode -eq 0 -and $RelayExitCode -eq 0) {
        $HashTemporary = Join-Path $StateRoot (".last-successful-manifest." + [Guid]::NewGuid().ToString("N") + ".tmp")
        try {
            [System.IO.File]::WriteAllText($HashTemporary, $DownloadedHash + [Environment]::NewLine, (New-Object System.Text.UTF8Encoding($false)))
            Move-Item -Force -Path $HashTemporary -Destination $SeenManifestHash
        }
        finally {
            Remove-Item -Force -ErrorAction SilentlyContinue $HashTemporary
        }
    }
    if ($ApplyExitCode -ne 0) { exit $ApplyExitCode }
    exit $RelayExitCode
}
finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $Temporary
}
