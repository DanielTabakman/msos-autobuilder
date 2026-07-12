[CmdletBinding()]
param(
    [string]$HostRoot = (Join-Path $env:USERPROFILE ".msos-autobuilder"),
    [string]$TaskName = "MSOS Autobuilder Candidate Gate",
    [string]$ResultsRepoUrl = "https://github.com/DanielTabakman/msos-autobuilder.git",
    [string]$ResultsBranch = "results",
    [string]$MachineId = $env:COMPUTERNAME,
    [string]$WitnessJobId = "mcd-boundary-and-frozen-contract-v1",
    [int]$PollSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($ResultsBranch -in @("main", "master")) {
    throw "The candidate gate may not target main or master."
}

function Convert-ToYamlQuoted {
    param([Parameter(Mandatory = $true)][string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function Convert-ToForwardSlash {
    param([Parameter(Mandatory = $true)][string]$Value)
    return $Value.Replace("\", "/")
}

function Restore-EnvironmentValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()][string]$Value
    )

    if ($null -eq $Value) {
        Remove-Item -Path "Env:\$Name" -ErrorAction SilentlyContinue
    }
    else {
        Set-Item -Path "Env:\$Name" -Value $Value
    }
}

function Invoke-GitChecked {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    & $Git.Source @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed ($LASTEXITCODE): git $($Arguments -join ' ')"
    }
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Bootstrap = Join-Path $PSScriptRoot "bootstrap_windows_codex_host_auto.ps1"
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$HostConfig = Join-Path $HostRoot "host.yaml"
$GateConfig = Join-Path $HostRoot "candidate-gate.yaml"
$LogRoot = Join-Path $HostRoot "logs"
$LogFile = Join-Path $LogRoot "candidate-gate.log"
$RunnerScript = Join-Path $HostRoot "run-candidate-gate.ps1"
$IntegrityWitness = Join-Path $RepoRoot "scripts\check_frozen_evaluation_candidate.py"
$ResultsCheckout = Join-Path $HostRoot "state\candidate-gate-results-repo"
$LedgerPath = Join-Path $HostRoot "state\candidate-gate-seen.json"
$Git = Get-Command git -ErrorAction Stop

if (-not (Test-Path $VenvPython)) {
    Write-Host "Preparing the Autobuilder Python environment..." -ForegroundColor Cyan
    & $Bootstrap -HostRoot $HostRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Autobuilder bootstrap failed."
    }
}
if (-not (Test-Path $HostConfig)) {
    throw "Codex host config not found at $HostConfig"
}
if (-not (Test-Path $IntegrityWitness)) {
    throw "Candidate integrity witness not found at $IntegrityWitness"
}

$EditableSpec = "$RepoRoot[dev]"
& $VenvPython -m pip install -e $EditableSpec | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw "Could not install the updated Autobuilder package and test dependencies."
}

New-Item -ItemType Directory -Force -Path $HostRoot, $LogRoot | Out-Null

$HostRootYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $HostRoot)
$ResultsRepoYaml = Convert-ToYamlQuoted $ResultsRepoUrl
$ResultsBranchYaml = Convert-ToYamlQuoted $ResultsBranch
$MachineIdYaml = Convert-ToYamlQuoted $MachineId
$PythonYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $VenvPython)
$WitnessYaml = Convert-ToYamlQuoted (Convert-ToForwardSlash $IntegrityWitness)

$GateYaml = @"
version: 1
publication_enabled: false
host_root: $HostRootYaml
results_repo_url: $ResultsRepoYaml
results_branch: $ResultsBranchYaml
machine_id: $MachineIdYaml
poll_seconds: $PollSeconds
plans:
  mcd-boundary-and-frozen-contract-v1:
    checks:
      - name: focused-product-tests
        argv:
          - $PythonYaml
          - -m
          - pytest
          - -q
          - apps/msos-web/tests/test_strategy_lab_explainer_strip.py
          - tests/test_frozen_evaluation_contract.py
          - tests/test_frozen_evaluation_record.py
        cwd: .
        timeout_seconds: 900
      - name: frozen-evaluation-snapshot-identity
        argv:
          - $PythonYaml
          - $WitnessYaml
          - .
        cwd: .
        timeout_seconds: 120
    policy_blocks:
      - >-
        Schema write-version compatibility is unresolved: changing
        ppe_frozen_eval_v1 to frozen_evaluation_v1 requires explicit downstream
        consumer approval before this candidate can pass.
"@
Set-Content -Path $GateConfig -Value $GateYaml -Encoding UTF8

$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Write-Host "Running the candidate gate once in the foreground..." -ForegroundColor Cyan
$PreviousPrompt = $env:GIT_TERMINAL_PROMPT
$PreviousGitConfigCount = $env:GIT_CONFIG_COUNT
$PreviousGitConfigKey = $env:GIT_CONFIG_KEY_0
$PreviousGitConfigValue = $env:GIT_CONFIG_VALUE_0
$env:GIT_TERMINAL_PROMPT = "1"
$env:GIT_CONFIG_COUNT = "1"
$env:GIT_CONFIG_KEY_0 = "core.autocrlf"
$env:GIT_CONFIG_VALUE_0 = "false"
try {
    # Git patch hashes are calculated over canonical LF bytes. Force the results checkout
    # to preserve those bytes rather than applying the Windows core.autocrlf conversion.
    if (Test-Path (Join-Path $ResultsCheckout ".git")) {
        Invoke-GitChecked -Arguments @("-C", $ResultsCheckout, "config", "core.autocrlf", "false")
        Invoke-GitChecked -Arguments @("-C", $ResultsCheckout, "checkout-index", "-a", "-f")
    }

    # The first Windows witness may have been recorded as failed solely because CRLF
    # conversion changed a patch hash. Remove only that immutable ledger entry so the
    # corrected gate can replace its report. Other processed jobs remain untouched.
    $ExistingGateReport = Join-Path $ResultsCheckout "results\$MachineId\$WitnessJobId\gate-report.json"
    $RetryWitness = $false
    if (Test-Path $ExistingGateReport) {
        try {
            $GateReport = Get-Content -Path $ExistingGateReport -Raw | ConvertFrom-Json
            foreach ($ErrorEntry in @($GateReport.errors)) {
                if ($null -ne $ErrorEntry -and ([string]$ErrorEntry.message) -like "patch hash mismatch*") {
                    $RetryWitness = $true
                    break
                }
            }
        }
        catch {
            Write-Warning "Could not inspect the existing candidate report: $($_.Exception.Message)"
        }
    }

    if ($RetryWitness -and (Test-Path $LedgerPath)) {
        $Ledger = Get-Content -Path $LedgerPath -Raw | ConvertFrom-Json
        if ($Ledger.PSObject.Properties.Name -contains $WitnessJobId) {
            $Ledger.PSObject.Properties.Remove($WitnessJobId)
            $LedgerJson = ($Ledger | ConvertTo-Json -Depth 20) + [Environment]::NewLine
            $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($LedgerPath, $LedgerJson, $Utf8NoBom)
            Write-Host "Reprocessing the first witness after canonicalizing patch line endings." `
                -ForegroundColor Yellow
        }
    }

    & $VenvPython -m msos_autobuilder.candidate_gate `
        --config $GateConfig `
        --once | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "The foreground candidate gate failed to process results."
    }
}
finally {
    Restore-EnvironmentValue -Name "GIT_TERMINAL_PROMPT" -Value $PreviousPrompt
    Restore-EnvironmentValue -Name "GIT_CONFIG_COUNT" -Value $PreviousGitConfigCount
    Restore-EnvironmentValue -Name "GIT_CONFIG_KEY_0" -Value $PreviousGitConfigKey
    Restore-EnvironmentValue -Name "GIT_CONFIG_VALUE_0" -Value $PreviousGitConfigValue
}

$RunnerContent = @"
Set-StrictMode -Version Latest
`$ErrorActionPreference = "Continue"
`$env:PYTHONUTF8 = "1"
`$env:GIT_TERMINAL_PROMPT = "0"
`$env:GIT_CONFIG_COUNT = "1"
`$env:GIT_CONFIG_KEY_0 = "core.autocrlf"
`$env:GIT_CONFIG_VALUE_0 = "false"
New-Item -ItemType Directory -Force -Path "$LogRoot" | Out-Null
& "$VenvPython" -m msos_autobuilder.candidate_gate --config "$GateConfig" *>> "$LogFile"
exit `$LASTEXITCODE
"@
Set-Content -Path $RunnerScript -Value $RunnerContent -Encoding UTF8

$PowerShellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
$TaskArgument = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RunnerScript`""
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $TaskArgument
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $Settings `
    -Description "Review-only disposable MSOS candidate integration gate" `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2

Write-Host "Autobuilder candidate gate installed and started." -ForegroundColor Green
Write-Host "Task: $TaskName"
Write-Host "Config: $GateConfig"
Write-Host "Log: $LogFile"
Write-Host "Reports: results/$MachineId/<job-id>/gate-report.json on branch $ResultsBranch"
Write-Host "Product publication remains disabled."
