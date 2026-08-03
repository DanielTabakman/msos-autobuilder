from pathlib import Path

path = Path("tests/test_windows_self_update_supervisor.py")
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one anchor, found {count}")
    text = text.replace(old, new, 1)


replace_once(
    '            "$script:StubbedStartCount = 0",\n',
    '            "$script:StubbedStartCount = 0",\n'
    '            "$script:ProtectedMutationApplied = $false",\n',
    "nested mutation state",
)

replace_once(
    '            "    Write-StubbedWitness $TaskName",\n',
    '''            "    if (-not $script:ProtectedMutationApplied -and $TaskName -eq 'MSOS Autobuilder Host') {",\n            "        $MutationPath = $env:MSOS_STUBBED_PROTECTED_MUTATION_PATH",\n            "        $MutationOperation = $env:MSOS_STUBBED_PROTECTED_MUTATION_OPERATION",\n            "        if ($MutationPath -and $MutationOperation) {",\n            "            $script:ProtectedMutationApplied = $true",\n            "            switch ($MutationOperation) {",\n            "                'append' { Add-Content -Path $MutationPath -Value 'changed' }",\n            "                'create' {",\n            "                    $MutationParent = Split-Path -Parent $MutationPath",\n            "                    New-Item -ItemType Directory -Force -Path $MutationParent | Out-Null",\n            "                    New-Item -ItemType File -Force -Path $MutationPath | Out-Null",\n            "                }",\n            "                'delete' { Remove-Item -Force $MutationPath }",\n            "                default { throw \"unsupported protected mutation operation: $MutationOperation\" }",\n            "            }",\n            "        }",\n            "    }",\n            "    $ActionChangeMarker = $env:MSOS_STUBBED_ACTION_CHANGE_MARKER",\n            "    if ($ActionChangeMarker -and $TaskName -eq 'MSOS Autobuilder Host') {",\n            "        $MarkerParent = Split-Path -Parent $ActionChangeMarker",\n            "        New-Item -ItemType Directory -Force -Path $MarkerParent | Out-Null",\n            "        Set-Content -Path $ActionChangeMarker -Value 'changed' -Encoding UTF8",\n            "    }",\n            "    Write-StubbedWitness $TaskName",\n''',
    "nested start hook",
)

replace_once(
    '''    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {{
        $State = $global:RefillState
        if ($null -ne $global:RefillAction) {{
''',
    '''    if ($TaskName -eq 'MSOS Autobuilder Capacity-One Refill') {{
        $State = $global:RefillState
        if (
            -not $global:RefillActionChanged -and
            $env:MSOS_STUBBED_ACTION_CHANGE_MARKER -and
            (Test-Path $env:MSOS_STUBBED_ACTION_CHANGE_MARKER -PathType Leaf)
        ) {{
            $global:RefillAction.Arguments += ' -HostRoot C:/conflict'
            $global:RefillActionChanged = $true
        }}
        if ($null -ne $global:RefillAction) {{
''',
    "outer post-restart action change",
)

replace_once(
    '''$global:RefillAction = $null
$global:UpdateTaskState = 'Ready'
''',
    '''$global:RefillAction = $null
$global:RefillActionChanged = $false
$global:UpdateTaskState = 'Ready'
''',
    "outer action state",
)

replace_once(
    '''def _restart_mutation_script(command: str) -> str:
    return f"""
$global:ProtectedMutationApplied = $false
function Start-ScheduledTask {{
    param([string]$TaskName, [object]$ErrorAction)
    Add-Call -Action 'start' -Name $TaskName
    if (Get-ServiceName $TaskName) {{
        $global:ManagedTaskStates[$TaskName] = 'Running'
    }}
    if (-not $global:ProtectedMutationApplied -and $TaskName -eq 'MSOS Autobuilder Host') {{
        $global:ProtectedMutationApplied = $true
        {command}
    }}
    Write-OuterStubbedWitness $TaskName
}}
"""
''',
    '''def _restart_mutation_script(mutation_path: Path, operation: str) -> str:
    return f"""
$env:MSOS_STUBBED_PROTECTED_MUTATION_PATH = '{mutation_path.as_posix()}'
$env:MSOS_STUBBED_PROTECTED_MUTATION_OPERATION = '{operation}'
"""
''',
    "mutation helper",
)

replace_once(
    '("relative_path", "mutation", "expected_change"),',
    '("relative_path", "operation", "expected_change"),',
    "mutation parameter names",
)
text = text.replace('"Add-Content -Path $MutationPath -Value \'changed\'"', '"append"')
text = text.replace('"New-Item -ItemType File -Force -Path $MutationPath | Out-Null"', '"create"')
text = text.replace('"Remove-Item -Force $MutationPath"', '"delete"')
replace_once("    mutation: str,\n", "    operation: str,\n", "mutation function argument")
replace_once(
    '''    mutation_script = _restart_mutation_script(
        f"$MutationPath = '{mutation_path.as_posix()}'; {mutation}"
    )
''',
    '''    mutation_script = _restart_mutation_script(mutation_path, operation)
''',
    "mutation invocation",
)
replace_once(
    '''    mutation_script = _restart_mutation_script(
        "$global:RefillAction.Arguments += ' -HostRoot C:/conflict'"
    )
''',
    '''    action_change_marker = tmp_path / "refill-action-changed.marker"
    mutation_script = (
        "$env:MSOS_STUBBED_ACTION_CHANGE_MARKER = "
        f"'{action_change_marker.as_posix()}'"
    )
''',
    "action change invocation",
)

path.write_text(text, encoding="utf-8")
