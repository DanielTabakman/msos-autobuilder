"""Persistent local Autobuilder host with an approval-gated manifest queue."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .codex_shadow import (
    CodexHostConfig,
    CodexShadowReport,
    ShadowTaskSpec,
    load_codex_host_config,
    load_codex_shadow_manifest,
    run_codex_shadow,
)

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PersistentHostConfigError(ValueError):
    """Raised when persistent host configuration is invalid."""


class HostLockError(RuntimeError):
    """Raised when a second host process tries to start."""


class HostJobError(RuntimeError):
    """Raised when a queued job is malformed, unsafe, or cannot be archived."""


class JobFeedError(RuntimeError):
    """Raised when the optional Git manifest feed cannot be synchronized safely."""


@dataclass(frozen=True)
class GitJobFeedConfig:
    repo_url: str
    branch: str
    relative_path: str
    refresh_seconds: float = 30.0


@dataclass(frozen=True)
class PersistentHostConfig:
    host_root: Path
    codex_host_config: Path
    poll_seconds: float = 10.0
    heartbeat_seconds: float = 5.0
    publication_enabled: bool = False
    feed: GitJobFeedConfig | None = None


@dataclass(frozen=True)
class HostPaths:
    root: Path
    pending: Path
    running: Path
    completed: Path
    failed: Path
    state: Path
    runtime_jobs: Path
    feed_repo: Path
    artifacts: Path
    logs: Path
    status_file: Path
    lock_file: Path
    stop_file: Path
    feed_ledger: Path
    archive_staging: Path

    @classmethod
    def from_root(cls, root: str | Path) -> HostPaths:
        resolved = Path(root).expanduser().resolve()
        queue = resolved / "queue"
        state = resolved / "state"
        return cls(
            root=resolved,
            pending=queue / "pending",
            running=queue / "running",
            completed=queue / "completed",
            failed=queue / "failed",
            state=state,
            runtime_jobs=state / "jobs",
            feed_repo=state / "feed-repo",
            artifacts=resolved / "artifacts" / "host-jobs",
            logs=resolved / "logs",
            status_file=state / "host-status.json",
            lock_file=state / "host.lock",
            stop_file=state / "stop.requested",
            feed_ledger=state / "feed-seen.json",
            archive_staging=state / "archive-staging",
        )

    def ensure(self) -> None:
        for path in (
            self.pending,
            self.running,
            self.completed,
            self.failed,
            self.state,
            self.runtime_jobs,
            self.artifacts,
            self.logs,
            self.archive_staging,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class HostJob:
    job_id: str
    approved: bool
    manifest: dict[str, Any]
    content_sha256: str
    requested_by: str | None = None
    submitted_at: str | None = None
    approved_at: str | None = None
    expected_source_head: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class HostStatus:
    version: int
    state: str
    publication_enabled: bool
    pid: int | None
    started_at: str | None
    heartbeat_at: str
    active_job_id: str | None
    queue_counts: dict[str, int]
    last_result: dict[str, Any] | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class HostRunResult:
    processed: bool
    job_id: str | None
    outcome: str
    detail: str | None = None


ShadowRunner = Callable[
    [CodexHostConfig, tuple[ShadowTaskSpec, ...]],
    CodexShadowReport,
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _safe_id(value: str, field: str = "job_id") -> str:
    normalized = value.strip()
    if not _ID_PATTERN.fullmatch(normalized):
        raise HostJobError(
            f"{field} must start with an alphanumeric character and contain only "
            "letters, numbers, dot, dash, or underscore"
        )
    return normalized


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PersistentHostConfigError(f"{field} must be a mapping")
    return value


def _resolve_from(base: Path, value: Any, field: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise PersistentHostConfigError(f"{field} is required")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def load_persistent_host_config(path: str | Path) -> PersistentHostConfig:
    config_path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = _mapping(raw, "persistent host config")
    if root.get("version") != 1:
        raise PersistentHostConfigError("only persistent host config version 1 is supported")
    if root.get("publication_enabled", False) is not False:
        raise PersistentHostConfigError("persistent host publication must remain disabled")

    base = config_path.parent
    host_root = _resolve_from(base, root.get("host_root"), "host_root")
    codex_host_config = _resolve_from(
        base,
        root.get("codex_host_config"),
        "codex_host_config",
    )
    poll_seconds = float(root.get("poll_seconds", 10.0))
    heartbeat_seconds = float(root.get("heartbeat_seconds", 5.0))
    if poll_seconds <= 0:
        raise PersistentHostConfigError("poll_seconds must be positive")
    if heartbeat_seconds <= 0:
        raise PersistentHostConfigError("heartbeat_seconds must be positive")

    feed: GitJobFeedConfig | None = None
    feed_raw = root.get("job_feed")
    if feed_raw is not None:
        feed_data = _mapping(feed_raw, "job_feed")
        if bool(feed_data.get("enabled", False)):
            repo_url = str(feed_data.get("repo_url") or "").strip()
            branch = str(feed_data.get("branch") or "jobs").strip()
            relative_path = str(feed_data.get("path") or "jobs/approved").strip().replace(
                "\\", "/"
            )
            refresh_seconds = float(feed_data.get("refresh_seconds", 30.0))
            if not repo_url:
                raise PersistentHostConfigError("job_feed.repo_url is required")
            if not branch:
                raise PersistentHostConfigError("job_feed.branch is required")
            relative = Path(relative_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise PersistentHostConfigError("job_feed.path must be a safe relative path")
            if refresh_seconds <= 0:
                raise PersistentHostConfigError("job_feed.refresh_seconds must be positive")
            feed = GitJobFeedConfig(
                repo_url=repo_url,
                branch=branch,
                relative_path=relative_path,
                refresh_seconds=refresh_seconds,
            )

    return PersistentHostConfig(
        host_root=host_root,
        codex_host_config=codex_host_config,
        poll_seconds=poll_seconds,
        heartbeat_seconds=heartbeat_seconds,
        publication_enabled=False,
        feed=feed,
    )


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise HostJobError("job manifest must be a mapping")
    if manifest.get("version") != 1:
        raise HostJobError("only Codex shadow manifest version 1 is supported")
    if manifest.get("publication_enabled", False) is not False:
        raise HostJobError("job manifest publication must remain disabled")
    lanes = manifest.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise HostJobError("job manifest lanes must be a non-empty list")
    for index, lane in enumerate(lanes):
        if not isinstance(lane, dict):
            raise HostJobError(f"job manifest lanes[{index}] must be a mapping")
        if lane.get("prompt_file"):
            raise HostJobError("persistent jobs must use inline instructions, not prompt_file")
        instruction = lane.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise HostJobError(f"job manifest lanes[{index}].instruction is required")
    return manifest


def parse_host_job(text: str, *, source: str | None = None) -> HostJob:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HostJobError(f"invalid job YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise HostJobError("job file must be a mapping")
    if raw.get("version") != 1:
        raise HostJobError("only host job version 1 is supported")
    if raw.get("publication_enabled", False) is not False:
        raise HostJobError("host job publication must remain disabled")
    approved = raw.get("approved", False)
    if not isinstance(approved, bool):
        raise HostJobError("approved must be a boolean")
    manifest = _validate_manifest(raw.get("manifest"))
    expected_source_head_raw = raw.get("expected_source_head")
    expected_source_head = (
        str(expected_source_head_raw).strip() if expected_source_head_raw else None
    )
    if expected_source_head and not re.fullmatch(r"[0-9a-fA-F]{7,64}", expected_source_head):
        raise HostJobError("expected_source_head must be a Git commit SHA")
    return HostJob(
        job_id=_safe_id(str(raw.get("job_id") or "")),
        approved=approved,
        manifest=manifest,
        content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        requested_by=str(raw.get("requested_by") or "").strip() or None,
        submitted_at=str(raw.get("submitted_at") or "").strip() or None,
        approved_at=str(raw.get("approved_at") or "").strip() or None,
        expected_source_head=expected_source_head,
        source=source or (str(raw.get("source") or "").strip() or None),
    )


def load_host_job(path: str | Path) -> HostJob:
    job_path = Path(path)
    return parse_host_job(job_path.read_text(encoding="utf-8"), source=str(job_path))


def _job_exists(paths: HostPaths, job_id: str) -> bool:
    filename = f"{job_id}.yaml"
    if (paths.pending / filename).exists() or (paths.running / filename).exists():
        return True
    return (paths.completed / job_id).exists() or (paths.failed / job_id).exists()


def enqueue_manifest(
    paths: HostPaths,
    manifest_path: str | Path,
    *,
    job_id: str,
    approved: bool = False,
    requested_by: str = "local-operator",
    expected_source_head: str | None = None,
) -> Path:
    paths.ensure()
    safe_job_id = _safe_id(job_id)
    if _job_exists(paths, safe_job_id):
        raise HostJobError(f"job {safe_job_id!r} already exists")
    manifest_raw = yaml.safe_load(Path(manifest_path).read_text(encoding="utf-8"))
    manifest = _validate_manifest(manifest_raw)
    now = _timestamp()
    payload: dict[str, Any] = {
        "version": 1,
        "job_id": safe_job_id,
        "approved": bool(approved),
        "publication_enabled": False,
        "requested_by": requested_by,
        "submitted_at": now,
        "manifest": manifest,
    }
    if approved:
        payload["approved_at"] = now
    if expected_source_head:
        payload["expected_source_head"] = expected_source_head
    destination = paths.pending / f"{safe_job_id}.yaml"
    _atomic_write_text(destination, yaml.safe_dump(payload, sort_keys=False))
    return destination


def approve_pending_job(paths: HostPaths, job_id: str) -> Path:
    safe_job_id = _safe_id(job_id)
    path = paths.pending / f"{safe_job_id}.yaml"
    if not path.exists():
        raise HostJobError(f"pending job not found: {safe_job_id}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise HostJobError("job file must be a mapping")
    raw["approved"] = True
    raw["approved_at"] = _timestamp()
    candidate = yaml.safe_dump(raw, sort_keys=False)
    parse_host_job(candidate, source=str(path))
    _atomic_write_text(path, candidate)
    return path


def _run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git command failed").strip()
        raise JobFeedError(detail)
    return proc.stdout.strip()


def _load_feed_ledger(paths: HostPaths) -> dict[str, str]:
    if not paths.feed_ledger.exists():
        return {}
    try:
        raw = json.loads(paths.feed_ledger.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JobFeedError(f"invalid feed ledger: {paths.feed_ledger}") from exc
    if not isinstance(raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
    ):
        raise JobFeedError("feed ledger must map job IDs to content hashes")
    return raw


def _record_feed_conflict(
    paths: HostPaths,
    job_id: str,
    previous_hash: str,
    current_hash: str,
    source: str,
) -> None:
    destination = paths.failed / f"{job_id}-feed-conflict-{secrets.token_hex(4)}"
    destination.mkdir(parents=True, exist_ok=False)
    _atomic_write_json(
        destination / "error.json",
        {
            "type": "feed-content-conflict",
            "job_id": job_id,
            "previous_sha256": previous_hash,
            "current_sha256": current_hash,
            "source": source,
            "recorded_at": _timestamp(),
            "publication_enabled": False,
        },
    )


def sync_git_job_feed(config: PersistentHostConfig, paths: HostPaths) -> tuple[str, ...]:
    feed = config.feed
    if feed is None:
        return ()
    paths.ensure()
    repo = paths.feed_repo
    if not (repo / ".git").exists():
        if repo.exists():
            shutil.rmtree(repo)
        repo.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--no-tags", "--no-checkout", feed.repo_url, str(repo)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "git clone failed").strip()
            raise JobFeedError(detail)

    _run_git(repo, "fetch", "--prune", "origin", feed.branch)
    _run_git(repo, "checkout", "-B", "autobuilder-job-feed", "FETCH_HEAD")
    _run_git(repo, "reset", "--hard", "FETCH_HEAD")
    _run_git(repo, "clean", "-fd")

    feed_root = (repo / feed.relative_path).resolve()
    if not feed_root.is_relative_to(repo.resolve()):
        raise JobFeedError("job feed path escapes the feed checkout")
    if not feed_root.exists():
        return ()

    ledger = _load_feed_ledger(paths)
    imported: list[str] = []
    for source_path in sorted(feed_root.glob("*.yaml")):
        text = source_path.read_text(encoding="utf-8")
        try:
            job = parse_host_job(text, source=str(source_path))
        except HostJobError:
            # A malformed remote file is ignored rather than taking down the host.
            continue
        if not job.approved:
            continue
        previous = ledger.get(job.job_id)
        if previous:
            if previous != job.content_sha256:
                _record_feed_conflict(
                    paths,
                    job.job_id,
                    previous,
                    job.content_sha256,
                    str(source_path),
                )
            continue
        if _job_exists(paths, job.job_id):
            # Existing local state is authoritative. Record the hash so the feed
            # does not keep attempting the same immutable job.
            ledger[job.job_id] = job.content_sha256
            continue
        destination = paths.pending / f"{job.job_id}.yaml"
        _atomic_write_text(destination, text)
        ledger[job.job_id] = job.content_sha256
        imported.append(job.job_id)
    _atomic_write_json(paths.feed_ledger, ledger)
    return tuple(imported)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class HostProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.token = secrets.token_hex(16)
        self.acquired = False

    def _existing_pid(self) -> int | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return int(raw.get("pid"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError, AttributeError):
            return None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            pid = self._existing_pid()
            if pid is not None and _pid_alive(pid):
                raise HostLockError(f"Autobuilder host is already running with PID {pid}")
            stale = self.path.with_name(
                f"{self.path.name}.stale-{int(time.time())}-{secrets.token_hex(3)}"
            )
            try:
                os.replace(self.path, stale)
            except FileNotFoundError:
                pass

        payload = json.dumps(
            {
                "pid": os.getpid(),
                "token": self.token,
                "started_at": _timestamp(),
            },
            sort_keys=True,
        ).encode("utf-8")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise HostLockError("another Autobuilder host acquired the lock") from exc
        try:
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if raw.get("token") == self.token:
            self.path.unlink(missing_ok=True)
        self.acquired = False

    def __enter__(self) -> HostProcessLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


class PersistentHost:
    def __init__(
        self,
        config: PersistentHostConfig,
        *,
        runner: ShadowRunner = run_codex_shadow,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.paths = HostPaths.from_root(config.host_root)
        self.runner = runner
        self.sleeper = sleeper
        self.started_at: str | None = None
        self.last_result: dict[str, Any] | None = None
        self._status_lock = threading.Lock()

    def initialize(self) -> HostStatus:
        self.paths.ensure()
        self._recover_interrupted_jobs()
        self.paths.stop_file.unlink(missing_ok=True)
        status = self._status("stopped")
        self._write_status(status)
        return status

    def _queue_counts(self) -> dict[str, int]:
        return {
            "pending": len(list(self.paths.pending.glob("*.yaml"))),
            "running": len(list(self.paths.running.glob("*.yaml"))),
            "completed": len([path for path in self.paths.completed.iterdir() if path.is_dir()]),
            "failed": len([path for path in self.paths.failed.iterdir() if path.is_dir()]),
        }

    def _status(
        self,
        state: str,
        *,
        active_job_id: str | None = None,
        errors: tuple[str, ...] = (),
    ) -> HostStatus:
        return HostStatus(
            version=1,
            state=state,
            publication_enabled=False,
            pid=os.getpid() if state not in {"stopped", "not-started"} else None,
            started_at=self.started_at,
            heartbeat_at=_timestamp(),
            active_job_id=active_job_id,
            queue_counts=self._queue_counts(),
            last_result=self.last_result,
            errors=errors,
        )

    def _write_status(self, status: HostStatus) -> None:
        with self._status_lock:
            _atomic_write_json(self.paths.status_file, asdict(status))

    def read_status(self) -> HostStatus:
        if not self.paths.status_file.exists():
            return self._status("not-started")
        raw = json.loads(self.paths.status_file.read_text(encoding="utf-8"))
        return HostStatus(
            version=int(raw.get("version", 1)),
            state=str(raw.get("state") or "unknown"),
            publication_enabled=bool(raw.get("publication_enabled", False)),
            pid=int(raw["pid"]) if raw.get("pid") is not None else None,
            started_at=str(raw["started_at"]) if raw.get("started_at") else None,
            heartbeat_at=str(raw.get("heartbeat_at") or ""),
            active_job_id=(
                str(raw["active_job_id"]) if raw.get("active_job_id") else None
            ),
            queue_counts={
                str(key): int(value) for key, value in dict(raw.get("queue_counts", {})).items()
            },
            last_result=(dict(raw["last_result"]) if raw.get("last_result") else None),
            errors=tuple(str(item) for item in raw.get("errors", [])),
        )

    def request_stop(self) -> None:
        self.paths.ensure()
        _atomic_write_text(self.paths.stop_file, _timestamp() + "\n")

    def _recover_interrupted_jobs(self) -> None:
        self.paths.ensure()
        for path in sorted(self.paths.running.glob("*.yaml")):
            job_id = path.stem
            self._archive_failure(
                path,
                job_id,
                RuntimeError("job was interrupted before the host restarted"),
                outcome="interrupted",
            )
        for staging in sorted(self.paths.archive_staging.iterdir()):
            if not staging.is_dir():
                continue
            destination = self.paths.failed / (
                f"archive-recovery-{staging.name}-{secrets.token_hex(3)}"
            )
            os.replace(staging, destination)
            _atomic_write_json(
                destination / "recovery.json",
                {
                    "type": "incomplete-archive-recovered",
                    "recorded_at": _timestamp(),
                    "publication_enabled": False,
                },
            )

    def _archive_failure(
        self,
        running_path: Path,
        job_id: str,
        error: BaseException,
        *,
        outcome: str = "failed",
    ) -> Path:
        safe_job_id = _safe_id(job_id)
        destination = self.paths.failed / safe_job_id
        if destination.exists():
            destination = self.paths.failed / f"{safe_job_id}-{secrets.token_hex(4)}"
        staging = self.paths.archive_staging / f"failure-{safe_job_id}-{secrets.token_hex(8)}"
        staging.mkdir(parents=True, exist_ok=False)
        if running_path.exists():
            os.replace(running_path, staging / "job.yaml")
        _atomic_write_json(
            staging / "error.json",
            {
                "job_id": safe_job_id,
                "outcome": outcome,
                "error_type": type(error).__name__,
                "message": str(error),
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )[-12000:],
                "recorded_at": _timestamp(),
                "publication_enabled": False,
            },
        )
        os.replace(staging, destination)
        return destination

    def _claim_next_job(self) -> tuple[Path, HostJob] | None:
        for pending_path in sorted(self.paths.pending.glob("*.yaml")):
            try:
                job = load_host_job(pending_path)
            except BaseException as exc:
                running_path = self.paths.running / pending_path.name
                try:
                    os.replace(pending_path, running_path)
                except FileNotFoundError:
                    continue
                self._archive_failure(running_path, pending_path.stem, exc)
                self.last_result = {
                    "job_id": pending_path.stem,
                    "outcome": "failed",
                    "message": str(exc),
                    "finished_at": _timestamp(),
                }
                continue
            if not job.approved:
                continue
            running_path = self.paths.running / pending_path.name
            try:
                os.replace(pending_path, running_path)
            except FileNotFoundError:
                continue
            return running_path, job
        return None

    @contextmanager
    def _heartbeat(self, job_id: str) -> Iterator[None]:
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(self.config.heartbeat_seconds):
                self._write_status(self._status("running", active_job_id=job_id))

        thread = threading.Thread(
            target=beat,
            name="autobuilder-host-heartbeat",
            daemon=True,
        )
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=max(1.0, self.config.heartbeat_seconds * 2))

    def _current_source_head(self, host_config: CodexHostConfig) -> str:
        proc = subprocess.run(
            ["git", "-C", str(host_config.source_repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if proc.returncode != 0:
            raise HostJobError((proc.stderr or proc.stdout or "git rev-parse failed").strip())
        return proc.stdout.strip()

    def _collect_workspace_patches(
        self,
        host_config: CodexHostConfig,
        specs: tuple[ShadowTaskSpec, ...],
        staging: Path,
    ) -> list[dict[str, Any]]:
        patch_index: list[dict[str, Any]] = []
        patch_root = staging / "patches"
        for spec in specs:
            workspace = host_config.workspace_root / spec.task.lane.lane_id
            proc = subprocess.run(
                ["git", "-C", str(workspace), "diff", "--binary", "--no-ext-diff", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
            )
            if proc.returncode != 0:
                raise HostJobError(
                    (proc.stderr or proc.stdout or "git diff failed").strip()
                )
            patch_text = proc.stdout
            patch_name = f"{spec.task.task_id}.patch"
            if patch_text:
                patch_root.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(patch_root / patch_name, patch_text)
            patch_index.append(
                {
                    "task_id": spec.task.task_id,
                    "lane_id": spec.task.lane.lane_id,
                    "allow_changes": spec.allow_changes,
                    "patch_file": f"patches/{patch_name}" if patch_text else None,
                    "patch_sha256": (
                        hashlib.sha256(patch_text.encode("utf-8")).hexdigest()
                        if patch_text
                        else None
                    ),
                }
            )
        return patch_index

    def _archive_success(
        self,
        running_path: Path,
        job: HostJob,
        report: CodexShadowReport,
        host_config: CodexHostConfig,
        specs: tuple[ShadowTaskSpec, ...],
    ) -> Path:
        destination = self.paths.completed / job.job_id
        if destination.exists():
            raise HostJobError(f"completed archive already exists for {job.job_id}")
        staging = self.paths.archive_staging / f"success-{job.job_id}-{secrets.token_hex(8)}"
        staging.mkdir(parents=True, exist_ok=False)
        # Keep the claimed job in running until every archive artifact is ready.
        # A crash is then recovered as an interrupted job rather than losing it.
        shutil.copy2(running_path, staging / "job.yaml")
        patch_index = self._collect_workspace_patches(host_config, specs, staging)
        report_payload = {
            "version": 1,
            "job_id": job.job_id,
            "outcome": "completed",
            "completed_at": _timestamp(),
            "publication_enabled": False,
            "job_sha256": job.content_sha256,
            "requested_by": job.requested_by,
            "source": job.source,
            "codex_report": asdict(report),
            "patches": patch_index,
        }
        _atomic_write_json(staging / "report.json", report_payload)
        running_path.unlink()
        os.replace(staging, destination)
        return destination

    def process_one(self) -> HostRunResult:
        claimed = self._claim_next_job()
        if claimed is None:
            return HostRunResult(processed=False, job_id=None, outcome="idle")
        running_path, job = claimed
        self._write_status(self._status("running", active_job_id=job.job_id))
        runtime_dir = self.paths.runtime_jobs / job.job_id
        try:
            host_config = load_codex_host_config(self.config.codex_host_config)
            current_head = self._current_source_head(host_config)
            if job.expected_source_head and not current_head.startswith(job.expected_source_head):
                raise HostJobError(
                    f"source HEAD {current_head} does not match expected "
                    f"{job.expected_source_head}"
                )

            if runtime_dir.exists():
                shutil.rmtree(runtime_dir)
            runtime_dir.mkdir(parents=True, exist_ok=False)
            manifest_path = runtime_dir / "manifest.yaml"
            _atomic_write_text(manifest_path, yaml.safe_dump(job.manifest, sort_keys=False))
            specs = load_codex_shadow_manifest(manifest_path)
            with self._heartbeat(job.job_id):
                report = self.runner(host_config, specs)
            archive = self._archive_success(
                running_path,
                job,
                report,
                host_config,
                specs,
            )
            self.last_result = {
                "job_id": job.job_id,
                "outcome": "completed",
                "archive": str(archive),
                "finished_at": _timestamp(),
            }
            return HostRunResult(True, job.job_id, "completed", str(archive))
        except BaseException as exc:
            archive = self._archive_failure(running_path, job.job_id, exc)
            self.last_result = {
                "job_id": job.job_id,
                "outcome": "failed",
                "archive": str(archive),
                "message": str(exc),
                "finished_at": _timestamp(),
            }
            return HostRunResult(True, job.job_id, "failed", str(exc))
        finally:
            shutil.rmtree(runtime_dir, ignore_errors=True)

    def run_once(self, *, sync_feed: bool = True) -> HostRunResult:
        self.paths.ensure()
        self.started_at = _timestamp()
        with HostProcessLock(self.paths.lock_file):
            self._recover_interrupted_jobs()
            errors: list[str] = []
            if sync_feed and self.config.feed is not None:
                try:
                    sync_git_job_feed(self.config, self.paths)
                except BaseException as exc:
                    errors.append(str(exc))
            self._write_status(self._status("idle", errors=tuple(errors)))
            result = self.process_one()
            self._write_status(self._status("stopped", errors=tuple(errors)))
            return result

    def run_forever(self) -> None:
        self.paths.ensure()
        self.started_at = _timestamp()
        last_feed_sync = 0.0
        with HostProcessLock(self.paths.lock_file):
            self._recover_interrupted_jobs()
            self.paths.stop_file.unlink(missing_ok=True)
            while not self.paths.stop_file.exists():
                errors: list[str] = []
                now = time.monotonic()
                if self.config.feed is not None and (
                    now - last_feed_sync >= self.config.feed.refresh_seconds
                ):
                    try:
                        sync_git_job_feed(self.config, self.paths)
                    except BaseException as exc:
                        errors.append(str(exc))
                    last_feed_sync = now
                self._write_status(self._status("idle", errors=tuple(errors)))
                result = self.process_one()
                if not result.processed:
                    self.sleeper(self.config.poll_seconds)
            self.paths.stop_file.unlink(missing_ok=True)
            self._write_status(self._status("stopped"))


def render_host_status_json(status: HostStatus) -> str:
    return json.dumps(asdict(status), indent=2, sort_keys=True) + "\n"


def render_host_result_json(result: HostRunResult) -> str:
    return json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
