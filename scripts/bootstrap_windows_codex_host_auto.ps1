[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$SourceRepoUrl = "https://github.com/DanielTabakman/Probability-prediction-engine.git",
    [switch]$RunShadow,
    [switch]$DangerousBypass
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

function Test-Python311 {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$PrefixArguments = @()
    )

    # Windows PowerShell 5 converts native stderr into error records. Missing
    # launcher versions are expected probes, so temporarily suppress them.
    $PreviousPreference = $ErrorActionPreference
    $ExitCode = 1
    try {
        $ErrorActionPreference = "SilentlyContinue"
        & $FilePath @PrefixArguments "-c" "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" *> $null
        $ExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousPreference
    }
    return $ExitCode -eq 0
}

if (-not (Test-Path $VenvPython)) {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    $Compatible = $false

    if ($PyLauncher) {
        foreach ($Version in @("3.13", "3.12", "3.11")) {
            if (Test-Python311 -FilePath $PyLauncher.Source -PrefixArguments @("-$Version")) {
                $Compatible = $true
                break
            }
        }

        if (-not $Compatible) {
            Write-Host "No Python 3.11+ runtime is installed. Installing Python 3.11..." -ForegroundColor Yellow
            & $PyLauncher.Source install 3.11
            if ($LASTEXITCODE -ne 0) {
                throw "Python installation failed. Run 'py install 3.11' manually, then rerun this script."
            }
        }
    }
    else {
        $Python = Get-Command python -ErrorAction SilentlyContinue
        if (-not $Python -or -not (Test-Python311 -FilePath $Python.Source)) {
            throw "Python 3.11 or newer is required. Install it, then rerun this script."
        }
    }
}

$Bootstrap = Join-Path $PSScriptRoot "bootstrap_windows_codex_host.ps1"
$Arguments = @{
    HostRoot = $HostRoot
    SourceRepoUrl = $SourceRepoUrl
    RunShadow = $RunShadow
    DangerousBypass = $DangerousBypass
}

& $Bootstrap @Arguments
