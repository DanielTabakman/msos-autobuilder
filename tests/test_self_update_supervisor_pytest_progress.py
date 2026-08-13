from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from msos_autobuilder.self_update_supervisor import (
    STAGING_PYTEST_HARD_CEILING_SECONDS,
    STAGING_PYTEST_HEARTBEAT_SECONDS,
    STAGING_PYTEST_NO_PROGRESS_SECONDS,
    STAGING_PYTEST_SOFT_CHECKPOINT_SECONDS,
    STAGING_PYTEST_TIMEOUT_SECONDS,
    _is_staged_pytest_command,
    _pytest_progress_advanced,
    _pytest_progress_view,
    _run_named_check,
    _run_progress_aware_command,
    default_command_executor,
)


def _progress_command(
    argv: list[str],
    cwd: Path,
    *,
    soft: float = 0.5,
    hard: float = 4.0,
    no_progress: float = 1.5,
    heartbeat: float = 0.2,
    poll: float = 0.05,
):
    return _run_progress_aware_command(
        argv,
        cwd,
        hard_ceiling_seconds=hard,
        soft_checkpoint_seconds=soft,
        no_progress_seconds=no_progress,
        heartbeat_seconds=heartbeat,
        poll_seconds=poll,
    )


def _python_script(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/NH", "/FI", f"PID eq {pid}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        output = completed.stdout
        return str(pid) in output and "No tasks" not in output
    return Path(f"/proc/{pid}").exists()


def test_progress_policy_constants_match_issue_148() -> None:
    assert STAGING_PYTEST_SOFT_CHECKPOINT_SECONDS == 2400.0
    assert STAGING_PYTEST_TIMEOUT_SECONDS == 2400.0
    assert STAGING_PYTEST_HARD_CEILING_SECONDS == 3600.0
    assert STAGING_PYTEST_NO_PROGRESS_SECONDS == 600.0
    assert STAGING_PYTEST_HEARTBEAT_SECONDS == 300.0
    assert _is_staged_pytest_command(["python", "-m", "pytest", "-q"])
    assert not _is_staged_pytest_command(["python", "-m", "pytest", "-vv"])


def test_liveness_text_is_not_pytest_progress() -> None:
    idle = _pytest_progress_view("still running\nstill running\n")
    progressed = _pytest_progress_view(".... [85%]\n")
    later = _pytest_progress_view(".... [85%]\n.... [92%]\n")

    assert idle.percentage is None
    assert idle.pass_dots == 0
    assert not _pytest_progress_advanced(idle, idle)
    assert _pytest_progress_advanced(idle, progressed)
    assert _pytest_progress_advanced(progressed, later)
    assert not _pytest_progress_advanced(later, later)


def test_progressing_process_can_cross_soft_checkpoint(tmp_path: Path) -> None:
    script = (
        "import time\n"
        "for step in range(1, 13):\n"
        "    print(f'.... [{step * 8}%]', flush=True)\n"
        "    time.sleep(0.12)\n"
        "print('12 passed', flush=True)\n"
    )
    result = _progress_command(
        _python_script(script),
        tmp_path,
        soft=0.5,
        hard=4.0,
        no_progress=1.5,
        heartbeat=0.2,
    )

    assert result.passed
    assert not result.timed_out
    assert result.duration_seconds > 0.5
    assert result.duration_seconds < 4.0
    assert result.progress["abort_reason"] is None
    assert result.progress["soft_checkpoint"] == "after"
    assert result.progress["latest_percentage"] == 96
    checkpoints = {item["soft_checkpoint"] for item in result.progress["heartbeats"]}
    assert "before" in checkpoints
    assert "after" in checkpoints


def test_progress_heartbeat_is_recorded(tmp_path: Path) -> None:
    script = (
        "import time\n"
        "for step in range(1, 10):\n"
        "    print(f'.... [{step * 10}%]', flush=True)\n"
        "    time.sleep(0.12)\n"
    )
    result = _progress_command(
        _python_script(script),
        tmp_path,
        soft=0.4,
        hard=4.0,
        no_progress=1.5,
        heartbeat=0.25,
    )

    heartbeats = result.progress["heartbeats"]
    assert heartbeats
    for item in heartbeats:
        assert "elapsed_seconds" in item
        assert "latest_progress" in item
        assert "seconds_since_last_progress" in item
        assert item["soft_checkpoint"] in {"before", "after"}
        assert item["latest_percentage"] is None or 0 <= item["latest_percentage"] <= 100
    assert any(item["latest_percentage"] for item in heartbeats)


def test_stagnant_process_dies_on_no_progress_threshold(tmp_path: Path) -> None:
    script = (
        "import time\n"
        "print('.... [10%]', flush=True)\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    result = _progress_command(
        _python_script(script),
        tmp_path,
        soft=5.0,
        hard=8.0,
        no_progress=0.8,
        heartbeat=10.0,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert not result.passed
    assert result.progress["abort_reason"] == "no_progress"
    assert result.termination["attempted"] is True
    assert elapsed < 4.0
    assert "no forward progress" in result.stderr
    assert result.progress["soft_checkpoint"] == "before"


def test_mere_liveness_does_not_reset_watchdog(tmp_path: Path) -> None:
    script = (
        "import time\n"
        "for _ in range(80):\n"
        "    print('still running', flush=True)\n"
        "    time.sleep(0.1)\n"
    )
    started = time.monotonic()
    result = _progress_command(
        _python_script(script),
        tmp_path,
        soft=5.0,
        hard=6.0,
        no_progress=0.8,
        heartbeat=10.0,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert result.progress["abort_reason"] == "no_progress"
    assert elapsed < 3.0
    assert "still running" in result.stdout
    assert result.progress["latest_percentage"] is None


def test_hard_ceiling_always_wins(tmp_path: Path) -> None:
    script = (
        "import time\n"
        "step = 0\n"
        "while True:\n"
        "    step += 1\n"
        "    print(f'. [{min(step, 99)}%]', flush=True)\n"
        "    time.sleep(0.05)\n"
    )
    started = time.monotonic()
    result = _progress_command(
        _python_script(script),
        tmp_path,
        soft=0.3,
        hard=1.0,
        no_progress=30.0,
        heartbeat=0.25,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert result.progress["abort_reason"] == "hard_ceiling"
    assert result.duration_seconds >= 0.9
    assert elapsed < 4.0
    assert result.progress["soft_checkpoint"] == "after"
    assert result.progress["latest_percentage"]
    assert "hard ceiling" in result.stderr


def test_successful_process_preserves_quiet_gate_semantics(tmp_path: Path) -> None:
    argv = [
        sys.executable,
        "-c",
        "print('.... [100%]', flush=True); print('1 passed in 0.01s', flush=True)",
        "-m",
        "pytest",
        "-q",
    ]
    result = _run_named_check(default_command_executor, "pytest", argv, tmp_path, 8.0)

    assert result.name == "pytest"
    assert result.argv[-3:] == ("-m", "pytest", "-q")
    assert result.passed
    assert not result.timed_out
    assert result.returncode == 0
    assert result.timeout_seconds == 8.0
    assert result.progress["abort_reason"] is None
    assert result.progress["latest_percentage"] == 100
    assert result.progress["hard_ceiling_seconds"] == 8.0
    assert result.progress["soft_checkpoint_seconds"] == STAGING_PYTEST_SOFT_CHECKPOINT_SECONDS


def test_windows_process_tree_termination_remains_safe(tmp_path: Path) -> None:
    script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen("
        "[sys.executable, '-c', 'import time; time.sleep(60)']"
        ")\n"
        "print(f'child_pid={child.pid}', flush=True)\n"
        "print('.... [10%]', flush=True)\n"
        "time.sleep(60)\n"
    )
    result = _progress_command(
        _python_script(script),
        tmp_path,
        soft=5.0,
        hard=8.0,
        no_progress=0.8,
        heartbeat=10.0,
    )

    assert result.timed_out
    assert result.termination["attempted"] is True
    assert result.process_tree["root_pid"] > 0
    if os.name == "nt":
        assert result.termination["method"] == "taskkill-tree"
    else:
        assert result.termination["method"] == "process-group-term-kill"
    child_pid = 0
    for line in result.stdout.splitlines():
        if line.startswith("child_pid="):
            child_pid = int(line.split("=", 1)[1])
            break
    assert child_pid > 0
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _pid_exists(child_pid):
        time.sleep(0.05)
    assert not _pid_exists(child_pid)
    assert not _pid_exists(result.process_tree["root_pid"])
