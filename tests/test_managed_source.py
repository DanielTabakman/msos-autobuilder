from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from test_build_next import SOURCE_REPO, _commit_all, _git, _write_ppe

import msos_autobuilder.managed_source as managed_source
from msos_autobuilder.managed_source import (
    ManagedSourceSyncError,
    ensure_managed_source_fresh,
)


def _sync(repo: Path, **overrides: object):
    config = {
        "source_remote": "origin",
        "source_ref": "main",
        "expected_source_repository": SOURCE_REPO,
        "allow_test_local_source_remote": True,
    }
    config.update(overrides)
    return ensure_managed_source_fresh(repo, **config)


def _advance_origin(repo: Path) -> str:
    (repo / "fresh.txt").write_text("fresh\n", encoding="utf-8")
    commit = _commit_all(repo, "advance origin")
    _git(repo, "push", "-q", "origin", "main")
    return commit


def test_current_source_is_idempotent(tmp_path: Path) -> None:
    repo = _write_ppe(tmp_path / "ppe")

    first = _sync(repo)
    second = _sync(repo)

    assert first.evidence["action"] == "already_current"
    assert second.evidence["action"] == "already_current"
    assert first.identity.commit == second.identity.commit


def test_clean_behind_managed_branch_fast_forwards(tmp_path: Path) -> None:
    repo = _write_ppe(tmp_path / "ppe")
    expected = _advance_origin(repo)
    _git(repo, "reset", "--hard", "HEAD~1")

    result = _sync(repo)

    assert result.identity.commit == expected
    assert result.evidence["action"] == "fast_forward"
    assert result.evidence["old_commit"] != result.evidence["new_commit"]
    assert result.evidence["clean_after"] is True


def test_clean_behind_detached_checkout_advances_by_detaching_to_fetched(
    tmp_path: Path,
) -> None:
    repo = _write_ppe(tmp_path / "ppe")
    expected = _advance_origin(repo)
    _git(repo, "checkout", "-q", "--detach", "HEAD~1")

    result = _sync(repo)

    assert result.identity.commit == expected
    assert result.evidence["action"] == "detached_checkout"
    assert result.evidence["detached"] is True


@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        (
            "tracked",
            lambda repo: (repo / "requirements.txt").write_text(
                "dirty\n",
                encoding="utf-8",
            ),
        ),
        (
            "untracked",
            lambda repo: (repo / "untracked.txt").write_text(
                "dirty\n",
                encoding="utf-8",
            ),
        ),
    ],
)
def test_dirty_tracked_and_untracked_checkouts_block(
    tmp_path: Path,
    name: str,
    mutate: Callable[[Path], object],
) -> None:
    repo = _write_ppe(tmp_path / f"ppe-{name}")
    mutate(repo)

    with pytest.raises(ManagedSourceSyncError) as raised:
        _sync(repo)

    assert raised.value.evidence["block_reason"] == "dirty_checkout"
    assert raised.value.evidence["clean_before"] is False


def test_local_only_commits_block(tmp_path: Path) -> None:
    repo = _write_ppe(tmp_path / "ppe")
    (repo / "local.txt").write_text("local\n", encoding="utf-8")
    _commit_all(repo, "local only")

    with pytest.raises(ManagedSourceSyncError) as raised:
        _sync(repo)

    assert raised.value.evidence["block_reason"] == "local_only_commits"
    assert raised.value.evidence["ancestry"]["ahead"] == 1


def test_diverged_checkout_blocks(tmp_path: Path) -> None:
    repo = _write_ppe(tmp_path / "ppe")
    _advance_origin(repo)
    _git(repo, "reset", "--hard", "HEAD~1")
    (repo / "local.txt").write_text("local\n", encoding="utf-8")
    _commit_all(repo, "local divergence")

    with pytest.raises(ManagedSourceSyncError) as raised:
        _sync(repo)

    assert raised.value.evidence["block_reason"] == "diverged"
    assert raised.value.evidence["ancestry"]["diverged"] is True


def test_active_git_operation_blocks(tmp_path: Path) -> None:
    repo = _write_ppe(tmp_path / "ppe")
    git_dir = Path(_git(repo, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    (git_dir / "MERGE_HEAD").write_text(_git(repo, "rev-parse", "HEAD") + "\n", encoding="utf-8")

    with pytest.raises(ManagedSourceSyncError) as raised:
        _sync(repo)

    assert raised.value.evidence["block_reason"] == "active_merge"


def test_wrong_remote_blocks_before_mutation(tmp_path: Path) -> None:
    repo = _write_ppe(tmp_path / "ppe")

    with pytest.raises(ManagedSourceSyncError) as raised:
        _sync(repo, allow_test_local_source_remote=False)

    assert raised.value.evidence["block_reason"] == "non_canonical_remote"


def test_fetch_failure_blocks_with_evidence(tmp_path: Path) -> None:
    repo = _write_ppe(tmp_path / "ppe")

    with pytest.raises(ManagedSourceSyncError) as raised:
        _sync(repo, source_ref="missing")

    assert raised.value.evidence["block_reason"] == "fetch_failed"
    assert "fetch_error" in raised.value.evidence


def _source_lock_dir(repo: Path) -> Path:
    git_common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    if not git_common.is_absolute():
        git_common = repo / git_common
    return git_common / "msos-autobuilder-source-sync.lock"


def test_active_lock_blocks_second_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _write_ppe(tmp_path / "ppe")
    marker = tmp_path / "lock-held.txt"
    script = """
import sys
import time
from pathlib import Path
from msos_autobuilder.managed_source import _source_lock

repo = Path(sys.argv[1])
marker = Path(sys.argv[2])
with _source_lock(repo, {}):
    marker.write_text("held", encoding="utf-8")
    time.sleep(5)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path.cwd() / "src")
    holder = subprocess.Popen(
        [sys.executable, "-c", script, str(repo), str(marker)],
        cwd=Path.cwd(),
        env=env,
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        monkeypatch.setattr(managed_source, "_SOURCE_LOCK_TIMEOUT_SECONDS", 0.2)
        monkeypatch.setattr(managed_source, "_SOURCE_LOCK_RETRY_SECONDS", 0.01)

        with pytest.raises(ManagedSourceSyncError) as raised:
            _sync(repo)

        assert raised.value.evidence["block_reason"] == "source_lock_held"
        assert raised.value.evidence["lock"]["block_reason"] == "source_lock_held"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_orphaned_owner_lock_recovers_after_process_death(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _write_ppe(tmp_path / "ppe")
    lock = _source_lock_dir(repo)
    lock.mkdir()
    (lock / "owner.json").write_text(
        '{"created_at": "2026-07-27T00:00:00Z", "pid": 12345}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(managed_source, "_pid_alive", lambda pid: False)

    result = _sync(repo)

    assert result.evidence["action"] == "already_current"
    assert result.evidence["lock"]["recovered_stale"] is True
    assert not lock.exists()


def test_ownerless_partial_acquisition_lock_recovers(tmp_path: Path) -> None:
    repo = _write_ppe(tmp_path / "ppe")
    lock = _source_lock_dir(repo)
    lock.mkdir()

    result = _sync(repo)

    assert result.evidence["action"] == "already_current"
    assert result.evidence["lock"]["recovered_stale"] is True
    assert not lock.exists()


def test_live_legacy_owner_is_not_displaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _write_ppe(tmp_path / "ppe")
    lock = _source_lock_dir(repo)
    lock.mkdir()
    (lock / "owner.json").write_text(
        '{"created_at": "2026-07-27T00:00:00Z", "pid": 12345}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(managed_source, "_pid_alive", lambda pid: True)

    with pytest.raises(ManagedSourceSyncError) as raised:
        _sync(repo)

    assert raised.value.evidence["block_reason"] == "source_lock_held"
    assert raised.value.evidence["lock"]["block_reason"] == "source_lock_live_legacy_owner"
    assert lock.exists()
