from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from msos_autobuilder import self_update_supervisor as supervisor
from msos_autobuilder.self_update_supervisor import (
    _STILL_ACTIVE,
    _WINERROR_ACCESS_DENIED,
    _WINERROR_INVALID_PARAMETER,
    SupervisorConfig,
    SupervisorError,
    _exclusive_update_lock,
    _pid_is_running,
    _windows_pid_is_running,
    _windows_pid_is_running_from_open_result,
    _windows_platform,
)

OBSERVED_DEAD_PID = 40096


def _config(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig(
        supervisor_root=tmp_path / "supervisor",
        host_root=tmp_path / "host",
        repo_url="https://github.com/DanielTabakman/msos-autobuilder.git",
        repository="DanielTabakman/msos-autobuilder",
        task_controller_script=tmp_path / "task-control.ps1",
        release_probe_script=tmp_path / "probe.py",
        managed_tasks=(),
    )


def _write_lock(path: Path, pid: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "started_at": "2026-08-14T13:01:07.140642+00:00"})
        + "\n",
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
        if not _pid_is_running(pid):
            return pid
        time.sleep(0.05)
    pytest.fail(f"PID {pid} still appears running after kill")


def test_windows_dead_pid_winerror_87_is_not_running() -> None:
    assert (
        _windows_pid_is_running_from_open_result(
            opened=False,
            last_error=_WINERROR_INVALID_PARAMETER,
            exit_code=None,
        )
        is False
    )


def test_windows_live_pid_still_active_is_running() -> None:
    assert (
        _windows_pid_is_running_from_open_result(
            opened=True,
            last_error=0,
            exit_code=_STILL_ACTIVE,
        )
        is True
    )


def test_windows_exited_process_handle_is_not_running() -> None:
    assert (
        _windows_pid_is_running_from_open_result(
            opened=True,
            last_error=0,
            exit_code=0,
        )
        is False
    )


def test_windows_access_denied_is_conservative() -> None:
    assert (
        _windows_pid_is_running_from_open_result(
            opened=False,
            last_error=_WINERROR_ACCESS_DENIED,
            exit_code=None,
        )
        is True
    )


def test_windows_unknown_open_error_is_conservative() -> None:
    assert (
        _windows_pid_is_running_from_open_result(
            opened=False,
            last_error=6,
            exit_code=None,
        )
        is True
    )


def test_windows_exit_code_query_failure_is_conservative() -> None:
    assert (
        _windows_pid_is_running_from_open_result(
            opened=True,
            last_error=0,
            exit_code=None,
        )
        is True
    )


def test_pid_is_running_rejects_non_positive() -> None:
    assert _pid_is_running(0) is False
    assert _pid_is_running(-1) is False


def test_pid_is_running_live_pid() -> None:
    assert _pid_is_running(os.getpid()) is True


def test_pid_is_running_dead_pid() -> None:
    assert _pid_is_running(_spawn_dead_pid()) is False


def test_windows_dispatch_uses_native_probe_not_os_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(supervisor, "_windows_platform", lambda: True)
    monkeypatch.setattr(
        supervisor,
        "_windows_pid_is_running",
        lambda pid: _windows_pid_is_running_from_open_result(
            opened=False,
            last_error=_WINERROR_INVALID_PARAMETER,
            exit_code=None,
        ),
    )

    def _forbidden_kill(pid: int, sig: int) -> None:
        raise AssertionError("os.kill is not a Windows process-existence probe")

    monkeypatch.setattr(os, "kill", _forbidden_kill)
    assert _pid_is_running(OBSERVED_DEAD_PID) is False


def test_posix_permission_error_is_conservative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "_windows_platform", lambda: False)

    def _denied(pid: int, sig: int) -> None:
        raise PermissionError("EPERM")

    monkeypatch.setattr(os, "kill", _denied)
    assert _pid_is_running(OBSERVED_DEAD_PID) is True


def test_posix_process_lookup_error_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supervisor, "_windows_platform", lambda: False)

    def _missing(pid: int, sig: int) -> None:
        raise ProcessLookupError("ESRCH")

    monkeypatch.setattr(os, "kill", _missing)
    assert _pid_is_running(OBSERVED_DEAD_PID) is False


def test_stale_lock_with_dead_pid_is_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor, "_pid_is_running", lambda pid: False)
    config = _config(tmp_path)
    _write_lock(config.lock_path, OBSERVED_DEAD_PID)

    with _exclusive_update_lock(config):
        held = json.loads(config.lock_path.read_text(encoding="utf-8"))
        assert held["pid"] == os.getpid()

    assert not config.lock_path.exists()


def test_live_writer_lock_is_not_reclaimed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_lock(config.lock_path, os.getpid())
    original = config.lock_path.read_text(encoding="utf-8")

    with pytest.raises(SupervisorError, match="already running"):
        with _exclusive_update_lock(config):
            raise AssertionError("live writer lock must not be acquired")

    assert config.lock_path.read_text(encoding="utf-8") == original


def test_access_denied_pid_lock_is_not_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor, "_pid_is_running", lambda pid: True)
    config = _config(tmp_path)
    _write_lock(config.lock_path, OBSERVED_DEAD_PID)
    original = config.lock_path.read_text(encoding="utf-8")

    with pytest.raises(SupervisorError, match="already running"):
        with _exclusive_update_lock(config):
            raise AssertionError("ambiguous pid must not be reclaimed")

    assert config.lock_path.read_text(encoding="utf-8") == original


def test_reclaim_removes_only_configured_update_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(supervisor, "_pid_is_running", lambda pid: False)
    config = _config(tmp_path)
    _write_lock(config.lock_path, OBSERVED_DEAD_PID)
    other_state_lock = config.state_root / "other.lock"
    sibling_update_lock = tmp_path / "update.lock"
    nested_update_lock = config.state_root / "nested" / "update.lock"
    _write_lock(other_state_lock, OBSERVED_DEAD_PID)
    _write_lock(sibling_update_lock, OBSERVED_DEAD_PID)
    _write_lock(nested_update_lock, OBSERVED_DEAD_PID)

    with _exclusive_update_lock(config):
        assert config.lock_path.exists()
        assert other_state_lock.exists()
        assert sibling_update_lock.exists()
        assert nested_update_lock.exists()

    assert not config.lock_path.exists()
    assert other_state_lock.exists()
    assert sibling_update_lock.exists()
    assert nested_update_lock.exists()


def test_malformed_pid_lock_is_not_reclaimed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _write_lock(config.lock_path, "40096")
    original = config.lock_path.read_text(encoding="utf-8")

    with pytest.raises(SupervisorError, match="already running"):
        with _exclusive_update_lock(config):
            raise AssertionError("malformed pid must not be reclaimed")

    assert config.lock_path.read_text(encoding="utf-8") == original


@pytest.mark.skipif(not _windows_platform(), reason="Windows OpenProcess probe")
def test_windows_native_probe_live_and_dead_pid() -> None:
    assert _windows_pid_is_running(os.getpid()) is True
    assert _windows_pid_is_running(_spawn_dead_pid()) is False
