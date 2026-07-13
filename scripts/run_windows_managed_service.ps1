[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
    [string]$ServiceName,
    [string]$SupervisorRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder-supervisor"),
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:GIT_TERMINAL_PROMPT = "0"
$env:GIT_CONFIG_COUNT = "1"
$env:GIT_CONFIG_KEY_0 = "core.autocrlf"
$env:GIT_CONFIG_VALUE_0 = "false"

function Write-Utf8AtomicText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )
    $Parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $Parent | Out-Null
    $Temporary = Join-Path $Parent ("." + [IO.Path]::GetFileName($Path) + "." + [Guid]::NewGuid().ToString("N") + ".tmp")
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Temporary, $Value, $Utf8NoBom)
    Move-Item -Force -Path $Temporary -Destination $Path
}

function Write-Utf8AtomicJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][hashtable]$Value
    )
    $Parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $Parent | Out-Null
    $Temporary = Join-Path $Parent ("." + [IO.Path]::GetFileName($Path) + "." + [Guid]::NewGuid().ToString("N") + ".tmp")
    $Json = ($Value | ConvertTo-Json -Depth 20) + [Environment]::NewLine
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Temporary, $Json, $Utf8NoBom)
    Move-Item -Force -Path $Temporary -Destination $Path
}

function Expand-ServiceArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    return $Value.Replace("{host_root}", $HostRoot).Replace("{machine_id}", $env:COMPUTERNAME).Replace("{managed_release_root}", $ReleasePath).Replace("{managed_python}", $Python).Replace("{runtime_config}", $RuntimeConfig)
}

$StateRoot = Join-Path $SupervisorRoot "state"
$ActivePointerPath = Join-Path $StateRoot "active-release.json"
$ServicesPath = Join-Path $SupervisorRoot "bootstrap\managed-services.json"
$ProbeScript = Join-Path $SupervisorRoot "bootstrap\managed_release_health_probe.py"
$WitnessPath = Join-Path $StateRoot "service-witnesses\$ServiceName.json"

if (-not (Test-Path $ActivePointerPath)) {
    throw "Active release pointer not found at $ActivePointerPath"
}
if (-not (Test-Path $ServicesPath)) {
    throw "Managed service specification not found at $ServicesPath"
}
if (-not (Test-Path $ProbeScript -PathType Leaf)) {
    throw "Stable managed-release health probe not found at $ProbeScript"
}

$Active = Get-Content -Path $ActivePointerPath -Raw | ConvertFrom-Json
$ReleaseCommit = [string]$Active.commit
$ReleasePath = [string]$Active.release_path
if ($ReleaseCommit -notmatch "^[0-9a-f]{40}$" -or -not (Test-Path $ReleasePath -PathType Container)) {
    throw "Active release pointer is malformed."
}
$ReleaseMarkerPath = Join-Path $ReleasePath "release.json"
$ReleaseMarker = Get-Content -Path $ReleaseMarkerPath -Raw | ConvertFrom-Json
if ([string]$ReleaseMarker.commit -ne $ReleaseCommit) {
    throw "Active release marker does not match the active pointer."
}

$Services = Get-Content -Path $ServicesPath -Raw | ConvertFrom-Json
$ServiceProperty = $Services.services.PSObject.Properties[$ServiceName]
if ($null -eq $ServiceProperty) {
    throw "Unknown managed service: $ServiceName"
}
$Service = $ServiceProperty.Value
$Python = Join-Path $ReleasePath ".venv\Scripts\python.exe"
if (-not (Test-Path $Python -PathType Leaf)) {
    throw "Managed release Python not found at $Python"
}

$env:PATH = (Join-Path $ReleasePath ".venv\Scripts") + ";" + $env:PATH
$RuntimeConfig = ""
$TemplateProperty = $Service.PSObject.Properties["config_template"]
if ($null -ne $TemplateProperty -and [string]$TemplateProperty.Value) {
    $TemplatePath = [string]$TemplateProperty.Value
    if (-not (Test-Path $TemplatePath -PathType Leaf)) { throw "Managed config template not found at $TemplatePath" }
    $RuntimeConfig = Join-Path $StateRoot "runtime-config\$ServiceName.yaml"
    $Template = Get-Content -Path $TemplatePath -Raw
    $Rendered = $Template.Replace("{managed_release_root}", $ReleasePath.Replace("\", "/")).Replace("{managed_python}", $Python.Replace("\", "/"))
    Write-Utf8AtomicText -Path $RuntimeConfig -Value $Rendered
}

$LogPath = Expand-ServiceArgument ([string]$Service.log_file)
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
$Arguments = @($Service.argv | ForEach-Object { Expand-ServiceArgument ([string]$_) })

# The stable wrapper, not managed supervisor code, proves every managed entry point imports
# from the selected exact release before emitting a running witness.
& $Python $ProbeScript $ReleasePath *>> $LogPath
if ($LASTEXITCODE -ne 0) {
    Write-Utf8AtomicJson -Path $WitnessPath -Value @{
        version = 1
        service = $ServiceName
        state = "failed"
        release_commit = $ReleaseCommit
        wrapper_pid = $PID
        failed_at = [DateTimeOffset]::UtcNow.ToString("o")
        error = "stable release health probe failed"
    }
    exit $LASTEXITCODE
}

Write-Utf8AtomicJson -Path $WitnessPath -Value @{
    version = 1
    service = $ServiceName
    state = "running"
    release_commit = $ReleaseCommit
    release_path = $ReleasePath
    child_pid = $PID
    started_at = [DateTimeOffset]::UtcNow.ToString("o")
}

try {
    & $Python @Arguments *>> $LogPath
    $ExitCode = $LASTEXITCODE
}
catch {
    $ExitCode = 1
    $_ | Out-String | Add-Content -Path $LogPath -Encoding UTF8
}

Write-Utf8AtomicJson -Path $WitnessPath -Value @{
    version = 1
    service = $ServiceName
    state = "stopped"
    release_commit = $ReleaseCommit
    child_pid = $PID
    stopped_at = [DateTimeOffset]::UtcNow.ToString("o")
    exit_code = $ExitCode
}
exit $ExitCode
