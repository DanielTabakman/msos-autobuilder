"""Managed source checkout freshness gate."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SourceIdentity:
    remote: str
    remote_url: str
    repository: str
    ref: str
    remote_ref: str
    commit: str


@dataclass(frozen=True)
class ManagedSourceSyncResult:
    identity: SourceIdentity
    evidence: dict[str, Any]


class ManagedSourceSyncError(RuntimeError):
    """Raised when a managed source checkout cannot be proven fresh."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


_SOURCE_LOCK_TIMEOUT_SECONDS = 30.0
_SOURCE_LOCK_RETRY_SECONDS = 0.05


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_github_repository(url: str) -> str | None:
    text = str(url or "").strip()
    patterns = (
        r"^https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
        r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, text, flags=re.IGNORECASE)
        if match:
            owner, repo = match.groups()
            return f"{owner}/{repo}"
    return None


def _run_git(
    repo: Path | None,
    *args: str,
    accepted: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    argv = ["git"]
    if repo is not None:
        argv.extend(["-C", str(repo)])
    argv.extend(args)
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if proc.returncode not in accepted:
        return proc
    return proc


def _git(repo: Path | None, *args: str, accepted: tuple[int, ...] = (0,)) -> str:
    proc = _run_git(repo, *args, accepted=accepted)
    if proc.returncode not in accepted:
        detail = (proc.stderr or proc.stdout or "command failed").strip()
        raise RuntimeError(f"{' '.join(['git', *args])}: {detail}")
    return proc.stdout.strip()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        if os.name != "nt":
            return False
        proc = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        return str(pid) in (proc.stdout or "")
    return True


def _read_lock_owner(owner_path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _lock_owner_pid(owner: dict[str, Any] | None) -> int | None:
    if owner is None:
        return None
    try:
        return int(owner.get("pid"))
    except (TypeError, ValueError):
        return None


@contextmanager
def _source_lock(ppe_repo: Path, evidence: dict[str, Any]):
    git_common = _git(ppe_repo, "rev-parse", "--git-common-dir")
    git_common_path = Path(git_common)
    if not git_common_path.is_absolute():
        git_common_path = (ppe_repo / git_common_path).resolve()
    lock_path = git_common_path / "msos-autobuilder-source-sync.lock"
    guard_path = git_common_path / "msos-autobuilder-source-sync.lockfile"
    owner_path = lock_path / "owner.json"
    evidence["lock"] = {
        "path": str(lock_path),
        "guard_path": str(guard_path),
        "acquired": False,
        "waited": False,
        "recovered_stale": False,
    }
    deadline = time.monotonic() + _SOURCE_LOCK_TIMEOUT_SECONDS
    guard_path.parent.mkdir(parents=True, exist_ok=True)
    handle = guard_path.open("a+b")
    try:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    evidence["block_reason"] = "source_lock_held"
                    evidence["lock"]["block_reason"] = "source_lock_held"
                    raise ManagedSourceSyncError(
                        "Managed PPE source synchronization is already in progress.",
                        evidence,
                    ) from exc
                evidence["lock"]["waited"] = True
                time.sleep(_SOURCE_LOCK_RETRY_SECONDS)

        if lock_path.exists():
            if not lock_path.is_dir():
                evidence["block_reason"] = "source_lock_held"
                evidence["lock"]["block_reason"] = "source_lock_path_not_directory"
                raise ManagedSourceSyncError(
                    "Managed PPE source synchronization lock path is not a directory.",
                    evidence,
                )
            owner = _read_lock_owner(owner_path)
            owner_pid = _lock_owner_pid(owner)
            evidence["lock"]["stale_owner"] = owner
            if owner_pid is not None and _pid_alive(owner_pid):
                evidence["block_reason"] = "source_lock_held"
                evidence["lock"]["block_reason"] = "source_lock_live_legacy_owner"
                raise ManagedSourceSyncError(
                    "Managed PPE source synchronization is already in progress.",
                    evidence,
                )
            try:
                owner_path.unlink(missing_ok=True)
                lock_path.rmdir()
                evidence["lock"]["recovered_stale"] = True
            except OSError as exc:
                evidence["block_reason"] = "source_lock_held"
                evidence["lock"]["block_reason"] = "source_lock_unrecoverable"
                raise ManagedSourceSyncError(
                    "Managed PPE source synchronization lock could not be recovered safely.",
                    evidence,
                ) from exc

        try:
            lock_path.mkdir()
        except FileExistsError as exc:
            evidence["block_reason"] = "source_lock_held"
            evidence["lock"]["block_reason"] = "source_lock_raced"
            raise ManagedSourceSyncError(
                "Managed PPE source synchronization is already in progress.",
                evidence,
            ) from exc
        token = secrets.token_hex(16)
        evidence["lock"]["acquired"] = True
        owner_path.write_text(
            json.dumps(
                {"pid": os.getpid(), "token": token, "created_at": utc_now()},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            yield
        finally:
            try:
                owner = _read_lock_owner(owner_path)
                if owner is None or owner.get("token") == token:
                    owner_path.unlink(missing_ok=True)
                    lock_path.rmdir()
            except OSError:
                pass
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _is_full_sha(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", value))


def _merge_base_is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    proc = _run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant, accepted=(0, 1))
    return proc.returncode == 0


def _active_operation(repo: Path) -> str | None:
    git_dir = _git(repo, "rev-parse", "--git-dir")
    git_dir_path = Path(git_dir)
    if not git_dir_path.is_absolute():
        git_dir_path = (repo / git_dir_path).resolve()
    checks = {
        "merge": ["MERGE_HEAD"],
        "rebase": ["rebase-merge", "rebase-apply"],
        "cherry_pick": ["CHERRY_PICK_HEAD"],
        "revert": ["REVERT_HEAD"],
        "bisect": ["BISECT_LOG", "BISECT_START"],
    }
    for name, rels in checks.items():
        if any((git_dir_path / rel).exists() for rel in rels):
            return name
    return None


def ensure_managed_source_fresh(
    ppe_repo: Path,
    *,
    source_remote: str,
    source_ref: str,
    expected_source_repository: str,
    allow_test_local_source_remote: bool = False,
    expected_source_commit: str | None = None,
) -> ManagedSourceSyncResult:
    ppe_repo = ppe_repo.expanduser().resolve()
    remote_ref = f"{source_remote}/{source_ref}"
    evidence: dict[str, Any] = {
        "version": 1,
        "checked_at": utc_now(),
        "repository_path": str(ppe_repo),
        "remote": source_remote,
        "ref": source_ref,
        "remote_ref": remote_ref,
        "expected_repository": expected_source_repository,
        "expected_source_commit": expected_source_commit,
        "action": "blocked",
        "block_reason": None,
    }

    def block(reason: str, message: str) -> None:
        evidence["block_reason"] = reason
        raise ManagedSourceSyncError(message, evidence)

    try:
        remote_url = _git(ppe_repo, "remote", "get-url", source_remote)
    except RuntimeError as exc:
        block("missing_remote", str(exc))
    repository = normalize_github_repository(remote_url)
    evidence["remote_url"] = remote_url
    evidence["repository"] = repository
    if repository is None:
        if allow_test_local_source_remote:
            repository = expected_source_repository
            evidence["repository"] = repository
            evidence["test_local_source_remote"] = True
        else:
            block(
                "non_canonical_remote",
                f"PPE source remote {source_remote!r} is not a canonical GitHub URL",
            )
    if repository != expected_source_repository:
        block(
            "wrong_remote_repository",
            "PPE source remote resolves to "
            f"{repository!r}, expected {expected_source_repository!r}",
        )

    with _source_lock(ppe_repo, evidence):
        operation = _active_operation(ppe_repo)
        evidence["active_operation"] = operation
        if operation is not None:
            block(
                f"active_{operation}",
                f"PPE source checkout has an active Git {operation.replace('_', '-')} operation",
            )
        clean_before = _git(ppe_repo, "status", "--porcelain=v1", "--untracked-files=all")
        evidence["clean_before"] = clean_before == ""
        evidence["dirty_entries_before"] = clean_before.splitlines()
        if clean_before:
            block("dirty_checkout", "PPE source checkout is dirty; cannot prove source freshness")

        current_commit = _git(ppe_repo, "rev-parse", "HEAD")
        branch = _git(ppe_repo, "symbolic-ref", "-q", "--short", "HEAD", accepted=(0, 1))
        evidence["old_commit"] = current_commit
        evidence["branch"] = branch or None
        evidence["detached"] = not bool(branch)
        if not _is_full_sha(current_commit):
            block("invalid_head", "PPE source HEAD is not a full SHA")

        fetch = _run_git(
            ppe_repo,
            "fetch",
            "--no-tags",
            source_remote,
            source_ref,
            accepted=(0,),
        )
        if fetch.returncode != 0:
            evidence["fetch_error"] = (fetch.stderr or fetch.stdout or "").strip()
            block("fetch_failed", f"Could not fetch {source_remote} {source_ref}")
        fetched_commit = _git(ppe_repo, "rev-parse", remote_ref)
        evidence["fetched_commit"] = fetched_commit
        if not _is_full_sha(fetched_commit):
            block("invalid_fetched_commit", "PPE remote source commit is not a full SHA")
        if expected_source_commit is not None:
            if not _is_full_sha(expected_source_commit):
                block("invalid_pinned_source", "Pinned generation source is not a full SHA")
            if current_commit != expected_source_commit:
                block(
                    "pinned_source_mismatch",
                    f"PPE source commit {current_commit} does not match pinned generation source "
                    f"{expected_source_commit}",
                )
            if fetched_commit != expected_source_commit:
                block(
                    "pinned_source_mismatch",
                    f"PPE remote source commit {fetched_commit} does not match pinned generation "
                    f"source {expected_source_commit}",
                )
            if branch and branch != source_ref:
                block(
                    "unexpected_branch",
                    f"PPE source checkout is on {branch!r}, expected managed {source_ref!r}",
                )
            evidence["ancestry"] = {
                "current_is_ancestor_of_fetched": True,
                "fetched_is_ancestor_of_current": True,
                "ahead": 0,
                "behind": 0,
                "diverged": False,
            }
            evidence["action"] = "already_current"
            evidence["new_commit"] = current_commit
            evidence["clean_after"] = True
            evidence["dirty_entries_after"] = []
            evidence["block_reason"] = None
            identity = SourceIdentity(
                remote=source_remote,
                remote_url=remote_url,
                repository=repository,
                ref=source_ref,
                remote_ref=remote_ref,
                commit=current_commit,
            )
            evidence["identity"] = asdict(identity)
            return ManagedSourceSyncResult(identity=identity, evidence=evidence)

        current_is_ancestor = _merge_base_is_ancestor(ppe_repo, current_commit, fetched_commit)
        fetched_is_ancestor = _merge_base_is_ancestor(ppe_repo, fetched_commit, current_commit)
        ahead_behind = _git(
            ppe_repo,
            "rev-list",
            "--left-right",
            "--count",
            f"HEAD...{remote_ref}",
        )
        ahead, behind = [int(part) for part in ahead_behind.split()]
        ancestry = {
            "current_is_ancestor_of_fetched": current_is_ancestor,
            "fetched_is_ancestor_of_current": fetched_is_ancestor,
            "ahead": ahead,
            "behind": behind,
            "diverged": not current_is_ancestor and not fetched_is_ancestor,
        }
        evidence["ancestry"] = ancestry

        if branch and branch != source_ref:
            block(
                "unexpected_branch",
                f"PPE source checkout is on {branch!r}, expected managed {source_ref!r}",
            )
        elif current_commit == fetched_commit:
            evidence["action"] = "already_current"
        elif not current_is_ancestor:
            if fetched_is_ancestor:
                block("local_only_commits", "PPE source checkout has local-only commits")
            block("diverged", "PPE source checkout has diverged from freshly fetched origin/main")
        elif branch:
            _git(ppe_repo, "merge", "--ff-only", remote_ref)
            evidence["action"] = "fast_forward"
        else:
            _git(ppe_repo, "checkout", "--detach", fetched_commit)
            evidence["action"] = "detached_checkout"

        final_commit = _git(ppe_repo, "rev-parse", "HEAD")
        clean_after = _git(ppe_repo, "status", "--porcelain=v1", "--untracked-files=all")
        evidence["new_commit"] = final_commit
        evidence["clean_after"] = clean_after == ""
        evidence["dirty_entries_after"] = clean_after.splitlines()
        if final_commit != fetched_commit:
            block("final_head_mismatch", f"PPE source HEAD does not match fetched {remote_ref}")
        if clean_after:
            block("dirty_after_sync", "PPE source checkout is dirty after source synchronization")
        if expected_source_commit is not None and final_commit != expected_source_commit:
            block(
                "pinned_source_mismatch",
                f"PPE source commit {final_commit} does not match pinned generation source "
                f"{expected_source_commit}",
            )
        evidence["block_reason"] = None
        identity = SourceIdentity(
            remote=source_remote,
            remote_url=remote_url,
            repository=repository,
            ref=source_ref,
            remote_ref=remote_ref,
            commit=final_commit,
        )
        evidence["identity"] = asdict(identity)
        return ManagedSourceSyncResult(identity=identity, evidence=evidence)
