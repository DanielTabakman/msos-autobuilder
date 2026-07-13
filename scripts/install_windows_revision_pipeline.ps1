[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$GateInstaller = Join-Path $PSScriptRoot "configure_windows_revision_candidate_gate.ps1"
$RevisionInstaller = Join-Path $PSScriptRoot "install_windows_revision_loop.ps1"

Write-Host "Configuring the revision-aware candidate gate..." -ForegroundColor Cyan
& $GateInstaller -HostRoot $HostRoot
if ($LASTEXITCODE -ne 0) { throw "Candidate-gate configuration failed." }

Write-Host "Installing the automatic revision loop..." -ForegroundColor Cyan
& $RevisionInstaller -HostRoot $HostRoot
if ($LASTEXITCODE -ne 0) { throw "Revision-loop installation failed." }

Write-Host "MSOS revision pipeline is installed." -ForegroundColor Green
Write-Host "Candidate failures can now generate bounded correction jobs automatically."
Write-Host "Product publication remains disabled."
