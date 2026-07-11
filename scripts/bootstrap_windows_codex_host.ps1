[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$SourceRepoUrl = "https://github.com/DanielTabakman/Probability-prediction-engine.git",
    [switch]$RunShadow,
    [switch]$DangerousBypass
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory
    )

    if ($WorkingDirectory) {
        Push-Location $WorkingDirectory
    }
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed ($LASTEXITCODE): $FilePath $($Arguments -join ' ')"
        }
    }
    finally {
        if ($WorkingDirectory) {
            Pop-Location
        }
    }
}

function Convert-ToYamlQuoted {
    param([Parameter(Mandatory = $true)][string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function Convert-ToForwardSlash {
    param([Parameter(Mandatory = $true)][string]$Value)
    return $Value.Replace("\", "/")
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvRoot = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        Invoke-Checked -FilePath $PyLauncher.Source -Arguments @("-3.11", "-m", "venv", $VenvRoot)
    }
    else {
        $Python = Get-Command python -ErrorAction Stop
        Invoke-Checked -FilePath $Python.Source -Arguments @("-m", "venv", $VenvRoot)
    }
}

Invoke-Checked -FilePath $VenvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked -FilePath $VenvPython -Arguments @("-m", "pip", "install", "-e", $RepoRoot)

$SourceRoot = Join-Path $HostRoot "source\msos"
$WorkspaceRoot = Join-Path $HostRoot "workspaces"
$RuntimeRoot = Join-Path $HostRoot "runtime"
$ArtifactRoot = Join-Path $HostRoot "artifacts"
$HostConfig = Join-Path $HostRoot "host.yaml"
$ShadowManifest = Join-Path $HostRoot "shadow.yaml"

New-Item -ItemType Directory -Force -Path $HostRoot, $WorkspaceRoot, $RuntimeRoot, $ArtifactRoot | Out-Null

$Git = Get-Command git -ErrorAction Stop
$ManagedMarker = Join-Path $SourceRoot ".git\msos-autobuilder-managed"
if (-not (Test-Path (Join-Path $SourceRoot ".git"))) {
    New-Item -ItemType Directory -Force -Path (Split-Path $SourceRoot -Parent) | Out-Null
    Invoke-Checked -FilePath $Git.Source -Arguments @("clone", $SourceRepoUrl, $SourceRoot)
    Set-Content -Path $ManagedMarker -Value "managed by msos-autobuilder" -Encoding UTF8
}
elseif (-not (Test-Path $ManagedMarker)) {
    throw "Refusing to reset an unmanaged repository at $SourceRoot"
}

Invoke-Checked -FilePath $Git.Source -Arguments @("-C", $SourceRoot, "fetch", "origin", "main")
Invoke-Checked -FilePath $Git.Source -Arguments @("-C", $SourceRoot, "checkout", "-B", "main", "origin/main")
Invoke-Checked -FilePath $Git.Source -Arguments @("-C", $SourceRoot, "reset", "--hard", "origin/main")
Invoke-Checked -FilePath $Git.Source -Arguments @("-C", $SourceRoot, "clean", "-fd")

$CodexCandidates = New-Object System.Collections.Generic.List[string]
if ($env:MSOS_AUTOBUILDER_CODEX_EXE) {
    $CodexCandidates.Add($env:MSOS_AUTOBUILDER_CODEX_EXE)
}
if ($env:LOCALAPPDATA) {
    $CodexCandidates.Add((Join-Path $env:LOCALAPPDATA "Programs\OpenAI\Codex\bin\codex.exe"))
}
$CodexCommand = Get-Command codex -ErrorAction SilentlyContinue
if ($CodexCommand) {
    $CodexCandidates.Add($CodexCommand.Source)
}
if ($env:APPDATA) {
    $CodexCandidates.Add((Join-Path $env:APPDATA "npm\codex.cmd"))
}

$CodexExe = $CodexCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $CodexExe) {
    throw "Codex CLI was not found. Install/sign in to Codex, then rerun this script."
}

& $CodexExe login status
if ($LASTEXITCODE -ne 0) {
    Write-Host "Codex is installed but not authenticated. Run: codex login" -ForegroundColor Yellow
    exit 3
}

$SandboxMode = if ($DangerousBypass) { "dangerous-bypass" } else { "workspace-write" }
$SourceYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $SourceRoot)
$WorkspaceYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $WorkspaceRoot)
$RuntimeYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $RuntimeRoot)
$CodexYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $CodexExe)

$HostYaml = @"
version: 1
publication_enabled: false
source_repo: $SourceYaml
workspace_root: $WorkspaceYaml
runtime_root: $RuntimeYaml
owner_id: windows-codex-shadow
reset_workspaces: true
codex:
  executable: $CodexYaml
  sandbox_mode: $SandboxMode
  max_concurrency: 2
  timeout_seconds: 7200
"@
Set-Content -Path $HostConfig -Value $HostYaml -Encoding UTF8

$ManifestYaml = @"
version: 1
publication_enabled: false
lanes:
  - task_id: msos-web-shadow-review
    lane_id: msos-web-shadow
    chapter_id: SHADOW-MSOS-WEB-REVIEW
    branch: shadow/msos-web-review
    layer: msos-shell
    preferred_cost_class: standard
    allowed_paths:
      - apps/msos-web/**
    forbidden_paths:
      - artifacts/**
      - docs/SOP/**
    allow_changes: false
    instruction: |
      Inspect the current MSOS web shell as a product engineer.
      Focus on the Minimum Credible Demo, user clarity, and the MSOS/PPE boundary.
      Identify the single highest-leverage bounded improvement and explain why.
      Do not modify files. Do not commit, push, publish, or open a pull request.
      Return concise findings only.
  - task_id: ppe-core-shadow-review
    lane_id: ppe-core-shadow
    chapter_id: SHADOW-PPE-CORE-REVIEW
    branch: shadow/ppe-core-review
    layer: ppe-core
    preferred_cost_class: standard
    allowed_paths:
      - src/engine/**
      - src/data/**
      - src/models/**
    forbidden_paths:
      - artifacts/**
      - apps/msos-web/**
    allow_changes: false
    instruction: |
      Inspect the PPE core engine, data, and model boundaries as a senior Python engineer.
      Identify the single highest-leverage bounded improvement for stable MSOS integration.
      Pay attention to contract stability, frozen snapshots, and duplicated product logic.
      Do not modify files. Do not commit, push, publish, or open a pull request.
      Return concise findings only.
"@
Set-Content -Path $ShadowManifest -Value $ManifestYaml -Encoding UTF8

$PreflightOut = Join-Path $ArtifactRoot "codex-preflight.json"
Invoke-Checked -FilePath $VenvPython -Arguments @(
    "-m", "msos_autobuilder", "codex-preflight",
    "--host-config", $HostConfig,
    "--json-out", $PreflightOut
)
Get-Content $PreflightOut

if ($RunShadow) {
    $ShadowOut = Join-Path $ArtifactRoot "codex-shadow.json"
    Invoke-Checked -FilePath $VenvPython -Arguments @(
        "-m", "msos_autobuilder", "codex-shadow",
        "--host-config", $HostConfig,
        "--manifest", $ShadowManifest,
        "--json-out", $ShadowOut
    )
    Get-Content $ShadowOut
}
else {
    Write-Host "Host is ready. Run the shadow lanes with:" -ForegroundColor Green
    Write-Host "& '$VenvPython' -m msos_autobuilder codex-shadow --host-config '$HostConfig' --manifest '$ShadowManifest' --json-out '$(Join-Path $ArtifactRoot "codex-shadow.json")'"
}
