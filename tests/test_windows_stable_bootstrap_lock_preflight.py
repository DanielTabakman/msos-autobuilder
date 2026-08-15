from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_HANDOFF = ROOT / "scripts" / "update_windows_stable_supervisor_bootstrap.ps1"
OBSERVED_STARTED_AT = "2026-08-14T13:01:07.140642+00:00"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _compact_powershell_text(*chunks: str) -> str:
    return re.sub(r"[\s|]+", "", _ANSI_ESCAPE.sub("", "".join(chunks)))


def _assert_powershell_reported(expected: str, *chunks: str) -> None:
    assert _compact_powershell_text(expected) in _compact_powershell_text(*chunks), (
        f"expected {expected!r} in PowerShell output: {''.join(chunks)!r}"
    )


def _powershell_test_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = (env.get("USERPROFILE") or "").strip()
    if existing:
        return env
    root = Path(tempfile.gettempdir()) / "msos-autobuilder-ci-userprofile"
    root.mkdir(parents=True, exist_ok=True)
    env["USERPROFILE"] = str(root)
    return env


def _powershell_executable() -> str:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is not installed on this runner")
    return powershell


def _write_lock(path: Path, pid: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "started_at": OBSERVED_STARTED_AT}) + "\n",
        encoding="utf-8",
    )


def _spawn_dead_pid() -> int:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pid = proc.pid
    proc.kill()
    proc.wait(timeout=10)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        probe = subprocess.run(
            [
                _powershell_executable(),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                f"Get-Process -Id {pid} -ErrorAction Stop | Out-Null",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=_powershell_test_env(),
        )
        if probe.returncode != 0:
            return pid
        time.sleep(0.05)
    pytest.fail(f"PID {pid} still appears running after kill")


def _lock_preflight_prelude() -> str:
    return f"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$HandoffPath = '{BOOTSTRAP_HANDOFF.as_posix()}'
$Tokens = $null
$Errors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $HandoffPath,
    [ref]$Tokens,
    [ref]$Errors
)
if ($Errors -and $Errors.Count -gt 0) {{
    throw ("Handoff parse failed: " + ($Errors | ForEach-Object {{ $_.ToString() }} | Out-String))
}}
$Wanted = @('Test-RecordedPidRunning', 'Assert-NoActiveUpdateAttempt')
foreach ($Name in $Wanted) {{
    $FunctionAst = $Ast.Find({{
            param($Node)
            $Node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            $Node.Name -eq $Name
        }}, $true) | Select-Object -First 1
    if (-not $FunctionAst) {{ throw "Missing handoff helper: $Name" }}
    Invoke-Expression $FunctionAst.Extent.Text
}}
"""


def _run_lock_preflight(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _powershell_executable(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            _lock_preflight_prelude() + "\n" + script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_powershell_test_env(),
    )


def test_handoff_script_does_not_use_pid_automatic_variable() -> None:
    script = BOOTSTRAP_HANDOFF.read_text(encoding="utf-8")
    assert "Test-RecordedPidRunning" in script
    assert "[Parameter(Mandatory = $true)][int]$RecordedPid" in script
    assert "Test-RecordedPidRunning -RecordedPid $RecordedPid" in script
    assert "$RecordedPid = [int]$Lock.pid" not in script
    assert "$JsonPid -isnot [int]" in script
    assert "$JsonPid -isnot [long]" in script
    assert re.search(r"(?i)\$Pid\b", script) is None
    assert re.search(r"(?i)(?<!Recorded)-Pid\b", script) is None
    assert "Test-PidRunning" not in script
    assert "[int]$Pid" not in script


def test_powershell_pid_automatic_variable_is_read_only() -> None:
    result = subprocess.run(
        [
            _powershell_executable(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            """
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Lock = [pscustomobject]@{ pid = 40096 }
try {
    $Pid = [int]$Lock.pid
    Write-Output 'COLLISION_ABSENT'
} catch {
    if ($_.Exception.Message -like '*Cannot overwrite variable PID*') {
        Write-Output 'COLLISION_PRESENT'
    } else {
        throw
    }
}
""",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_powershell_test_env(),
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "COLLISION_PRESENT" in result.stdout
    assert "COLLISION_ABSENT" not in result.stdout


def test_handoff_ast_has_no_pid_automatic_variable_identifiers() -> None:
    result = subprocess.run(
        [
            _powershell_executable(),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Tokens = $null
$Errors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{BOOTSTRAP_HANDOFF.as_posix()}',
    [ref]$Tokens,
    [ref]$Errors
)
if ($Errors -and $Errors.Count -gt 0) {{
    throw ("Handoff parse failed: " + ($Errors | ForEach-Object {{ $_.ToString() }} | Out-String))
}}
$Hits = $Ast.FindAll({{
        param($Node)
        (
            $Node -is [System.Management.Automation.Language.VariableExpressionAst] -and
            $Node.VariablePath.UserPath -eq 'PID'
        ) -or (
            $Node -is [System.Management.Automation.Language.ParameterAst] -and
            $Node.Name.VariablePath.UserPath -eq 'PID'
        ) -or (
            $Node -is [System.Management.Automation.Language.CommandParameterAst] -and
            $Node.ParameterName -eq 'PID'
        )
    }}, $true)
if (@($Hits).Count -ne 0) {{
    $Names = @($Hits) | ForEach-Object {{ $_.Extent.Text }}
    throw ("PID automatic-variable identifiers remain: " + ($Names | Out-String))
}}
Write-Output 'NO_PID_IDENTIFIERS'
""",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=_powershell_test_env(),
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "NO_PID_IDENTIFIERS" in result.stdout


def test_readable_dead_pid_lock_passes_preflight_without_mutating_lock(
    tmp_path: Path,
) -> None:
    supervisor_root = tmp_path / "supervisor"
    lock_path = supervisor_root / "state" / "update.lock"
    _write_lock(lock_path, _spawn_dead_pid())
    original = lock_path.read_bytes()

    result = _run_lock_preflight(
        f"""
Assert-NoActiveUpdateAttempt -SupervisorRoot '{supervisor_root.as_posix()}'
Write-Output 'PREFLIGHT_OK'
"""
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert "PREFLIGHT_OK" in result.stdout
    assert lock_path.read_bytes() == original


def test_readable_live_pid_lock_blocks_preflight_without_mutating_lock(
    tmp_path: Path,
) -> None:
    supervisor_root = tmp_path / "supervisor"
    lock_path = supervisor_root / "state" / "update.lock"
    live_pid = os.getpid()
    _write_lock(lock_path, live_pid)
    original = lock_path.read_bytes()

    result = _run_lock_preflight(
        f"""
Assert-NoActiveUpdateAttempt -SupervisorRoot '{supervisor_root.as_posix()}'
Write-Output 'PREFLIGHT_OK'
"""
    )

    assert result.returncode != 0
    _assert_powershell_reported(
        f"A self-update supervisor attempt is active with PID {live_pid}.",
        result.stderr,
        result.stdout,
    )
    assert "PREFLIGHT_OK" not in result.stdout
    assert "unreadable update lock" not in _compact_powershell_text(
        result.stderr, result.stdout
    )
    assert lock_path.read_bytes() == original


@pytest.mark.parametrize(
    ("payload", "as_bytes"),
    [
        ("not-json", False),
        ("", False),
        (json.dumps({"started_at": OBSERVED_STARTED_AT}) + "\n", False),
        (json.dumps({"pid": "not-a-pid", "started_at": OBSERVED_STARTED_AT}) + "\n", False),
        (b"\xff\xfe{" + b"not-utf8", True),
    ],
)
def test_unreadable_or_malformed_lock_blocks_preflight(
    tmp_path: Path,
    payload: str | bytes,
    as_bytes: bool,
) -> None:
    supervisor_root = tmp_path / "supervisor"
    lock_path = supervisor_root / "state" / "update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if as_bytes:
        assert isinstance(payload, bytes)
        lock_path.write_bytes(payload)
    else:
        assert isinstance(payload, str)
        lock_path.write_text(payload, encoding="utf-8")
    original = lock_path.read_bytes()

    result = _run_lock_preflight(
        f"""
Assert-NoActiveUpdateAttempt -SupervisorRoot '{supervisor_root.as_posix()}'
Write-Output 'PREFLIGHT_OK'
"""
    )

    assert result.returncode != 0
    _assert_powershell_reported(
        f"Found an unreadable update lock at {lock_path}; refusing bootstrap replacement.",
        result.stderr,
        result.stdout,
    )
    assert "PREFLIGHT_OK" not in result.stdout
    assert lock_path.read_bytes() == original


@pytest.mark.parametrize(
    "pid",
    [
        "40096",
        True,
        40096.5,
    ],
)
def test_coercible_non_integer_pid_lock_blocks_preflight_without_mutating_lock(
    tmp_path: Path,
    pid: object,
) -> None:
    supervisor_root = tmp_path / "supervisor"
    lock_path = supervisor_root / "state" / "update.lock"
    _write_lock(lock_path, pid)
    original = lock_path.read_bytes()

    result = _run_lock_preflight(
        f"""
Assert-NoActiveUpdateAttempt -SupervisorRoot '{supervisor_root.as_posix()}'
Write-Output 'PREFLIGHT_OK'
"""
    )

    assert result.returncode != 0
    _assert_powershell_reported(
        f"Found an unreadable update lock at {lock_path}; refusing bootstrap replacement.",
        result.stderr,
        result.stdout,
    )
    assert "PREFLIGHT_OK" not in result.stdout
    assert "A self-update supervisor attempt is active" not in _compact_powershell_text(
        result.stderr, result.stdout
    )
    assert lock_path.read_bytes() == original


def test_ambiguous_process_probe_blocks_preflight_without_mutating_lock(
    tmp_path: Path,
) -> None:
    supervisor_root = tmp_path / "supervisor"
    lock_path = supervisor_root / "state" / "update.lock"
    _write_lock(lock_path, 40096)
    original = lock_path.read_bytes()

    result = _run_lock_preflight(
        f"""
function Get-Process {{
    [CmdletBinding()]
    param([int]$Id)
    throw (New-Object System.UnauthorizedAccessException 'access denied')
}}
Assert-NoActiveUpdateAttempt -SupervisorRoot '{supervisor_root.as_posix()}'
Write-Output 'PREFLIGHT_OK'
"""
    )

    assert result.returncode != 0
    _assert_powershell_reported(
        "Process existence for PID 40096 is ambiguous:",
        result.stderr,
        result.stdout,
    )
    assert "PREFLIGHT_OK" not in result.stdout
    assert "unreadable update lock" not in _compact_powershell_text(
        result.stderr, result.stdout
    )
    assert lock_path.read_bytes() == original
