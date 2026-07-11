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
$VenvRoot = Join-Path $RepoRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"

function Invoke-NativeQuiet {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    $PreviousPreference = $ErrorActionPreference
    $ExitCode = 1
    try {
        # Windows PowerShell 5 promotes native stderr into error records. Missing
        # Python launcher versions are expected during discovery, so suppress them.
        $ErrorActionPreference = "SilentlyContinue"
        & $FilePath @Arguments *> $null
        if ($null -ne $LASTEXITCODE) {
            $ExitCode = [int]$LASTEXITCODE
        }
    }
    catch {
        $ExitCode = 1
    }
    finally {
        $ErrorActionPreference = $PreviousPreference
    }
    return $ExitCode
}

function Test-Python311 {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$PrefixArguments = @()
    )

    $Arguments = @($PrefixArguments) + @(
        "-c",
        "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    )
    return (Invoke-NativeQuiet -FilePath $FilePath -Arguments $Arguments) -eq 0
}

function New-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$PrefixArguments = @()
    )

    return [PSCustomObject]@{
        FilePath = $FilePath
        PrefixArguments = @($PrefixArguments)
    }
}

function Find-Python311 {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        foreach ($Version in @("3.13", "3.12", "3.11")) {
            $Prefix = @("-$Version")
            if (Test-Python311 -FilePath $PyLauncher.Source -PrefixArguments $Prefix) {
                return New-PythonCandidate -FilePath $PyLauncher.Source -PrefixArguments $Prefix
            }
        }
    }

    foreach ($CommandName in @("python", "python3", "python3.13", "python3.12", "python3.11")) {
        $Command = Get-Command $CommandName -ErrorAction SilentlyContinue
        if ($Command -and (Test-Python311 -FilePath $Command.Source)) {
            return New-PythonCandidate -FilePath $Command.Source
        }
    }

    $KnownPaths = New-Object System.Collections.Generic.List[string]
    foreach ($Base in @($env:LOCALAPPDATA, $env:ProgramFiles)) {
        if (-not $Base) {
            continue
        }
        foreach ($Version in @("313", "312", "311")) {
            $KnownPaths.Add((Join-Path $Base "Programs\Python\Python$Version\python.exe"))
            $KnownPaths.Add((Join-Path $Base "Python$Version\python.exe"))
        }
    }

    if ($env:LOCALAPPDATA) {
        $PackageRoot = Join-Path $env:LOCALAPPDATA "Packages"
        if (Test-Path $PackageRoot) {
            $StorePackages = Get-ChildItem -Path $PackageRoot -Directory -Filter "PythonSoftwareFoundation.Python.3.*" -ErrorAction SilentlyContinue
            foreach ($Package in $StorePackages) {
                $Executables = Get-ChildItem -Path $Package.FullName -Filter "python.exe" -File -Recurse -ErrorAction SilentlyContinue
                foreach ($Executable in $Executables) {
                    $KnownPaths.Add($Executable.FullName)
                }
            }
        }
    }

    foreach ($Path in $KnownPaths | Select-Object -Unique) {
        if ((Test-Path $Path) -and (Test-Python311 -FilePath $Path)) {
            return New-PythonCandidate -FilePath $Path
        }
    }

    return $null
}

function Install-Python311 {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        Write-Host "Trying the Python launcher installer for Python 3.11..." -ForegroundColor Yellow
        $PreviousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            & $PyLauncher.Source install 3.11
        }
        catch {
            Write-Host "The Python launcher installer did not complete; trying winget." -ForegroundColor Yellow
        }
        finally {
            $ErrorActionPreference = $PreviousPreference
        }

        $Candidate = Find-Python311
        if ($Candidate) {
            return $Candidate
        }
    }

    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($Winget) {
        Write-Host "Installing Python 3.11 for the current user with winget..." -ForegroundColor Yellow
        $WingetArguments = @(
            "install",
            "--exact",
            "--id", "Python.Python.3.11",
            "--scope", "user",
            "--silent",
            "--accept-package-agreements",
            "--accept-source-agreements"
        )
        $Process = Start-Process -FilePath $Winget.Source -ArgumentList $WingetArguments -Wait -PassThru -NoNewWindow
        if ($Process.ExitCode -ne 0) {
            Write-Host "winget returned exit code $($Process.ExitCode); verifying installation anyway." -ForegroundColor Yellow
        }

        $Candidate = Find-Python311
        if ($Candidate) {
            return $Candidate
        }
    }

    throw @"
Python 3.11 or newer could not be installed automatically.
Run this once, then rerun the bootstrap:
  winget install --exact --id Python.Python.3.11 --scope user
"@
}

if (-not (Test-Path $VenvPython)) {
    if ((Test-Path $VenvRoot) -and -not (Test-Path $VenvPython)) {
        Remove-Item -Path $VenvRoot -Recurse -Force
    }

    $PythonCandidate = Find-Python311
    if (-not $PythonCandidate) {
        $PythonCandidate = Install-Python311
    }

    Write-Host "Creating Autobuilder virtual environment with $($PythonCandidate.FilePath)..." -ForegroundColor Cyan
    $VenvArguments = @($PythonCandidate.PrefixArguments) + @("-m", "venv", $VenvRoot)
    & $PythonCandidate.FilePath @VenvArguments
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $VenvPython)) {
        throw "Python was found, but virtual-environment creation failed."
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
