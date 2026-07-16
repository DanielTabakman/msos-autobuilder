"""Bounded capacity-one refill controller for accepted build-next dispatch."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .build_next import (
    BuildNextConfig,
    BuildNextReceipt,
    _prepare_feed_checkout,
    build_next,
)
from .persistent_host import HostPaths, HostProcessLock, parse_host_job


class RefillControllerError(RuntimeError):
    """Raised when refill policy or state would exceed the v1 boundary."""


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(payload), indent=2, sort_keys=True) + "\n"
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


class ReconcileFileLock(AbstractContextManager["ReconcileFileLock"]):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> ReconcileFileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        self.handle.write(b"0")
        self.handle.flush()
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RefillControllerError("could not acquire refill reconcile lock") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def _required_bool(raw: Mapping[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise RefillControllerError(f"{key} must be a boolean")
    return value


def _required_int(raw: Mapping[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RefillControllerError(f"{key} must be an integer")
    return value


@dataclass(frozen=True)
class RefillPolicy:
    version: int = 1
    enabled: bool = False
    desired_capacity: int = 1
    resume_desired_capacity: int | None = None
    dispatch_window: dict[str, Any] = field(
        default_factory=lambda: {"mode": "always", "suppression_enabled": False}
    )
    queue_cap: int = 4
    review_cap_per_repository: int = 2
    last_decision_evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.version != 1:
            raise RefillControllerError("only refill policy version 1 is supported")
        if self.desired_capacity not in {0, 1}:
            raise RefillControllerError("v1 refill supports only desired capacity 0 or 1")
        if (
            self.resume_desired_capacity is not None
            and self.resume_desired_capacity not in {0, 1}
        ):
            raise RefillControllerError("v1 refill resume capacity must be 0 or 1")
        if self.queue_cap < 0:
            raise RefillControllerError("queue_cap must be non-negative")
        if self.review_cap_per_repository < 0:
            raise RefillControllerError("review_cap_per_repository must be non-negative")
        if not isinstance(self.dispatch_window, dict):
            raise RefillControllerError("dispatch_window must be a mapping")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> RefillPolicy:
        dispatch_window = raw.get(
            "dispatch_window",
            {"mode": "always", "suppression_enabled": False},
        )
        if not isinstance(dispatch_window, dict):
            raise RefillControllerError("dispatch_window must be a mapping")
        resume_raw = raw.get("resume_desired_capacity")
        if resume_raw is not None and (
            not isinstance(resume_raw, int) or isinstance(resume_raw, bool)
        ):
            raise RefillControllerError("resume_desired_capacity must be an integer or null")
        return cls(
            version=_required_int(raw, "version", 1),
            enabled=_required_bool(raw, "enabled", False),
            desired_capacity=_required_int(raw, "desired_capacity", 0),
            resume_desired_capacity=resume_raw,
            dispatch_window=dict(dispatch_window),
            queue_cap=_required_int(raw, "queue_cap", 4),
            review_cap_per_repository=_required_int(raw, "review_cap_per_repository", 2),
            last_decision_evidence=(
                dict(raw["last_decision_evidence"])
                if isinstance(raw.get("last_decision_evidence"), dict)
                else None
            ),
        )


@dataclass(frozen=True)
class RefillConfig:
    build_next: BuildNextConfig
    policy_path: Path | None = None
    max_host_heartbeat_age_seconds: int = 300
    supervisor_root: Path | None = None

    @classmethod
    def from_service_config(
        cls,
        service_config: str | Path,
        *,
        checkout_root: Path | None = None,
        max_snapshot_age_seconds: int = 600,
        requested_by: str = "capacity-one refill controller",
        submit: bool = True,
    ) -> RefillConfig:
        build_config = BuildNextConfig.from_service_config(
            service_config,
            checkout_root=checkout_root,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            requested_by=requested_by,
            submit=submit,
        )
        return cls(build_next=build_config)


@dataclass(frozen=True)
class RefillReport:
    status: str
    enabled: bool
    desired_capacity: int
    active_running: int
    active_queued: int
    feed_awaiting_import: int
    awaiting_review: dict[str, int]
    message: str
    decision_evidence: dict[str, Any]
    build_next_receipt: BuildNextReceipt | None = None


@dataclass(frozen=True)
class RefillServiceStatus:
    version: int
    state: str
    pid: int | None
    started_at: str | None
    heartbeat_at: str
    last_reconcile: dict[str, Any] | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapacitySnapshot:
    running: int
    queued: int
    feed_awaiting_import: int
    awaiting_review: dict[str, int]
    health: dict[str, Any]


def _policy_path(config: RefillConfig) -> Path:
    if config.policy_path is not None:
        return config.policy_path.expanduser().resolve()
    if config.build_next.host_root is None:
        raise RefillControllerError("refill controller requires host_root or policy_path")
    return HostPaths.from_root(config.build_next.host_root).state / "refill-policy.json"


def _status_path(config: RefillConfig) -> Path:
    return _host_paths(config).state / "refill-status.json"


def _service_lock_path(config: RefillConfig) -> Path:
    return _host_paths(config).state / "refill-service.lock"


def _reconcile_lock_path(config: RefillConfig) -> Path:
    return _host_paths(config).state / "refill-reconcile.lock"


def _refill_stop_path(config: RefillConfig) -> Path:
    return _host_paths(config).state / "refill-stop.requested"


def load_refill_policy(config: RefillConfig) -> RefillPolicy:
    path = _policy_path(config)
    if not path.exists():
        return RefillPolicy()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefillControllerError(f"refill policy is not valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise RefillControllerError("refill policy must be a JSON object")
    return RefillPolicy.from_mapping(raw)


def save_refill_policy(config: RefillConfig, policy: RefillPolicy) -> RefillPolicy:
    _atomic_write_json(_policy_path(config), asdict(policy))
    return policy


def keep_one_running(config: RefillConfig) -> RefillPolicy:
    with ReconcileFileLock(_reconcile_lock_path(config)):
        return _keep_one_running_locked(config)


def _keep_one_running_locked(config: RefillConfig) -> RefillPolicy:
    policy = load_refill_policy(config)
    updated = RefillPolicy(
        enabled=True,
        desired_capacity=1,
        resume_desired_capacity=1,
        dispatch_window=policy.dispatch_window,
        queue_cap=policy.queue_cap,
        review_cap_per_repository=policy.review_cap_per_repository,
        last_decision_evidence=policy.last_decision_evidence,
    )
    return save_refill_policy(config, updated)


def pause_builds(config: RefillConfig) -> RefillPolicy:
    with ReconcileFileLock(_reconcile_lock_path(config)):
        return _pause_builds_locked(config)


def _pause_builds_locked(config: RefillConfig) -> RefillPolicy:
    policy = load_refill_policy(config)
    resume_capacity = (
        policy.desired_capacity if policy.desired_capacity > 0 else policy.resume_desired_capacity
    )
    updated = RefillPolicy(
        enabled=False,
        desired_capacity=0,
        resume_desired_capacity=resume_capacity,
        dispatch_window=policy.dispatch_window,
        queue_cap=policy.queue_cap,
        review_cap_per_repository=policy.review_cap_per_repository,
        last_decision_evidence=policy.last_decision_evidence,
    )
    return save_refill_policy(config, updated)


def resume_builds(config: RefillConfig) -> RefillPolicy:
    with ReconcileFileLock(_reconcile_lock_path(config)):
        return _resume_builds_locked(config)


def _resume_builds_locked(config: RefillConfig) -> RefillPolicy:
    path = _policy_path(config)
    if not path.exists():
        raise RefillControllerError("cannot resume refill without a prior founder target")
    policy = load_refill_policy(config)
    if policy.resume_desired_capacity is None:
        raise RefillControllerError("cannot resume refill without a prior founder target")
    capacity = policy.resume_desired_capacity
    if capacity > 1:
        raise RefillControllerError("v1 refill cannot resume above capacity one")
    updated = RefillPolicy(
        enabled=True,
        desired_capacity=capacity,
        resume_desired_capacity=capacity,
        dispatch_window=policy.dispatch_window,
        queue_cap=policy.queue_cap,
        review_cap_per_repository=policy.review_cap_per_repository,
        last_decision_evidence=policy.last_decision_evidence,
    )
    return save_refill_policy(config, updated)


def keep_one_running_and_reconcile(config: RefillConfig) -> RefillReport:
    with ReconcileFileLock(_reconcile_lock_path(config)):
        _keep_one_running_locked(config)
        return _reconcile_refill_locked(config)


def pause_builds_and_reconcile(config: RefillConfig) -> RefillReport:
    with ReconcileFileLock(_reconcile_lock_path(config)):
        _pause_builds_locked(config)
        return _reconcile_refill_locked(config)


def resume_builds_and_reconcile(config: RefillConfig) -> RefillReport:
    with ReconcileFileLock(_reconcile_lock_path(config)):
        _resume_builds_locked(config)
        return _reconcile_refill_locked(config)


def _host_paths(config: RefillConfig) -> HostPaths:
    if config.build_next.host_root is None:
        raise RefillControllerError("refill controller requires host_root")
    paths = HostPaths.from_root(config.build_next.host_root)
    paths.ensure()
    return paths


def _supervisor_root(config: RefillConfig, paths: HostPaths) -> Path:
    if config.supervisor_root is not None:
        return config.supervisor_root.expanduser().resolve()
    return paths.root.parent / ".msos-autobuilder-supervisor"


def _queue_counts(paths: HostPaths) -> tuple[int, int]:
    return (
        len(list(paths.running.glob("*.yaml"))),
        len(list(paths.pending.glob("*.yaml"))),
    )


def _active_local_job_ids(paths: HostPaths) -> set[str]:
    return {
        path.stem
        for root in (paths.pending, paths.running)
        for path in root.glob("*.yaml")
    }


def _terminal_local_job_ids(paths: HostPaths) -> set[str]:
    return {
        path.name
        for root in (paths.completed, paths.failed)
        if root.exists()
        for path in root.iterdir()
        if path.is_dir()
    }


def _feed_awaiting_import(
    config: RefillConfig, paths: HostPaths
) -> tuple[int, list[str], dict[str, Any]]:
    check: dict[str, Any] = {"ok": True, "job_ids": [], "error": None}
    if not config.build_next.submit:
        check["skipped"] = "feed submission disabled"
        return 0, [], check
    try:
        checkout = _prepare_feed_checkout(config.build_next)
    except Exception as exc:
        check.update({"ok": False, "error": str(exc)})
        return 0, [], check
    root = checkout / config.build_next.jobs_path
    if not root.exists():
        check["path"] = str(root)
        return 0, [], check
    known = _active_local_job_ids(paths) | _terminal_local_job_ids(paths)
    pending: list[str] = []
    for source in sorted(root.glob("*.yaml")):
        try:
            job = parse_host_job(source.read_text(encoding="utf-8"), source=str(source))
        except BaseException:
            continue
        if job.approved and job.job_id not in known:
            pending.append(job.job_id)
    check.update({"path": str(root), "job_ids": list(pending)})
    return len(pending), pending, check


def _gate_report_repository(job: Mapping[str, Any], report: Mapping[str, Any]) -> str | None:
    contract = job.get("candidate_validation")
    if isinstance(contract, dict) and contract.get("target_repository"):
        return str(contract["target_repository"])
    founder = job.get("founder_build_next")
    if isinstance(founder, dict) and founder.get("repository"):
        return str(founder["repository"])
    report_job = report.get("job")
    if isinstance(report_job, dict):
        report_founder = report_job.get("founder_build_next")
        if isinstance(report_founder, dict) and report_founder.get("repository"):
            return str(report_founder["repository"])
    return None


def _publisher_dispositions(paths: HostPaths) -> set[str]:
    seen = paths.state / "controlled-publisher-seen.json"
    if not seen.exists():
        return set()
    try:
        raw = json.loads(seen.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(raw, dict):
        return set()
    return {str(key) for key in raw}


def _awaiting_review_counts(paths: HostPaths) -> dict[str, int]:
    counts: dict[str, int] = {}
    disposed = _publisher_dispositions(paths)
    results_root = paths.state / "candidate-gate-results-repo" / "results"
    if not results_root.exists():
        return counts
    for report_path in sorted(results_root.glob("*/*/gate-report.json")):
        try:
            raw = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        job_id = report_path.parent.name
        if job_id in disposed:
            continue
        state = str(raw.get("state") or "")
        status = str(raw.get("status") or "")
        if status != "passed" or state not in {"candidate_passed", "awaiting_review"}:
            continue
        job_path = report_path.with_name("job.yaml")
        try:
            job = yaml.safe_load(job_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(job, dict):
            continue
        repository = _gate_report_repository(job, raw) or "unknown"
        counts[repository] = counts.get(repository, 0) + 1
    return counts


def _active_release_evidence(path: Path) -> dict[str, Any]:
    evidence: dict[str, Any] = {"ok": False, "path": str(path)}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        evidence["error"] = str(exc)
        return evidence
    if not isinstance(raw, dict):
        evidence["error"] = "active release pointer must be a JSON object"
        return evidence
    commit = str(raw.get("commit") or "")
    release_path = Path(str(raw.get("release_path") or "")).expanduser()
    evidence.update(
        {
            "commit": commit,
            "release_path": str(release_path),
            "activated_at": raw.get("activated_at"),
        }
    )
    if not commit or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        evidence["error"] = "active release commit is not an exact lowercase SHA"
        return evidence
    try:
        marker_raw = json.loads((release_path / "release.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        evidence["error"] = f"active release marker is missing or invalid: {exc}"
        return evidence
    if not isinstance(marker_raw, dict) or marker_raw.get("commit") != commit:
        evidence["error"] = "active release marker does not match active pointer"
        return evidence
    evidence["ok"] = True
    return evidence


def _managed_service_evidence(
    supervisor: Path,
    *,
    expected_commit: Any,
    activated_at: Any,
) -> dict[str, Any]:
    services = ("host", "relay", "gate", "revision", "publisher", "refill")
    root = supervisor / "state" / "service-witnesses"
    activated = _parse_utc(activated_at)
    checks: dict[str, Any] = {"ok": True, "path": str(root), "services": {}}
    for service in services:
        path = root / f"{service}.json"
        item: dict[str, Any] = {"ok": False, "path": str(path)}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            item["error"] = str(exc)
            checks["ok"] = False
            checks["services"][service] = item
            continue
        if not isinstance(raw, dict):
            item["error"] = "service witness must be a JSON object"
            checks["ok"] = False
            checks["services"][service] = item
            continue
        started = _parse_utc(raw.get("started_at"))
        item.update(
            {
                "state": raw.get("state"),
                "release_commit": raw.get("release_commit"),
                "pid": raw.get("child_pid") or raw.get("pid") or raw.get("wrapper_pid"),
                "started_at": raw.get("started_at"),
            }
        )
        if raw.get("state") != "running":
            item["error"] = "service witness is not running"
        elif expected_commit and raw.get("release_commit") != expected_commit:
            item["error"] = "service witness does not match active release"
        elif activated is not None and (started is None or started < activated):
            item["error"] = "service witness predates active release"
        else:
            item["ok"] = True
        if not item["ok"]:
            checks["ok"] = False
        checks["services"][service] = item
    return checks


def _health_snapshot(config: RefillConfig, paths: HostPaths) -> dict[str, Any]:
    health: dict[str, Any] = {
        "checked_at": _utc_now(),
        "ok": True,
        "checks": {},
    }
    checks = health["checks"]
    host_status: dict[str, Any] | None = None
    if paths.status_file.exists():
        try:
            raw = json.loads(paths.status_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                host_status = raw
        except (OSError, json.JSONDecodeError):
            host_status = None
    heartbeat = _parse_utc((host_status or {}).get("heartbeat_at"))
    host_ok = False
    age: float | None = None
    if heartbeat is not None:
        age = (datetime.now(UTC) - heartbeat).total_seconds()
        host_ok = age <= config.max_host_heartbeat_age_seconds
    checks["host_heartbeat"] = {
        "ok": host_ok,
        "heartbeat_at": (host_status or {}).get("heartbeat_at"),
        "max_age_seconds": config.max_host_heartbeat_age_seconds,
        "age_seconds": age,
    }
    if not host_ok:
        health["ok"] = False
    checks["feed_configuration"] = {
        "ok": bool(config.build_next.feed_repo_url and config.build_next.jobs_branch),
        "jobs_branch": config.build_next.jobs_branch,
        "jobs_path": config.build_next.jobs_path,
    }
    if not checks["feed_configuration"]["ok"]:
        health["ok"] = False
    results_checkout = paths.state / "candidate-gate-results-repo"
    checks["results_checkout"] = {"ok": results_checkout.exists(), "path": str(results_checkout)}
    if not checks["results_checkout"]["ok"]:
        health["ok"] = False
    supervisor = _supervisor_root(config, paths)
    active_release = _active_release_evidence(supervisor / "state" / "active-release.json")
    checks["active_release"] = active_release
    if not active_release["ok"]:
        health["ok"] = False
    service_checks = _managed_service_evidence(
        supervisor,
        expected_commit=active_release.get("commit"),
        activated_at=active_release.get("activated_at"),
    )
    checks["managed_services"] = service_checks
    if not service_checks["ok"]:
        health["ok"] = False
    checks["publisher_state"] = {
        "ok": not (paths.state / "controlled-publisher-error.json").exists(),
        "ledger": str(paths.state / "controlled-publisher-seen.json"),
        "error": str(paths.state / "controlled-publisher-error.json"),
    }
    if not checks["publisher_state"]["ok"]:
        health["ok"] = False
    return health


def _capacity_snapshot(config: RefillConfig, paths: HostPaths) -> CapacitySnapshot:
    running, queued = _queue_counts(paths)
    feed_count, feed_ids, feed_check = _feed_awaiting_import(config, paths)
    health = _health_snapshot(config, paths)
    health["checks"]["feed_checkout"] = feed_check
    if not feed_check["ok"]:
        health["ok"] = False
    return CapacitySnapshot(
        running=running,
        queued=queued,
        feed_awaiting_import=feed_count,
        awaiting_review=_awaiting_review_counts(paths),
        health={**health, "feed_awaiting_import_job_ids": feed_ids},
    )


def _report(
    *,
    config: RefillConfig,
    policy: RefillPolicy,
    status: str,
    message: str,
    active_running: int,
    active_queued: int,
    feed_awaiting_import: int,
    awaiting_review: dict[str, int],
    evidence: dict[str, Any],
    receipt: BuildNextReceipt | None = None,
) -> RefillReport:
    decision = {
        "decided_at": _utc_now(),
        "status": status,
        "message": message,
        "enabled": policy.enabled,
        "desired_capacity": policy.desired_capacity,
        "active_running": active_running,
        "active_queued": active_queued,
        "feed_awaiting_import": feed_awaiting_import,
        "awaiting_review": dict(awaiting_review),
        **evidence,
    }
    updated = RefillPolicy(
        enabled=policy.enabled,
        desired_capacity=policy.desired_capacity,
        resume_desired_capacity=policy.resume_desired_capacity,
        dispatch_window=policy.dispatch_window,
        queue_cap=policy.queue_cap,
        review_cap_per_repository=policy.review_cap_per_repository,
        last_decision_evidence=decision,
    )
    save_refill_policy(config, updated)
    return RefillReport(
        status=status,
        enabled=policy.enabled,
        desired_capacity=policy.desired_capacity,
        active_running=active_running,
        active_queued=active_queued,
        feed_awaiting_import=feed_awaiting_import,
        awaiting_review=dict(awaiting_review),
        message=message,
        decision_evidence=decision,
        build_next_receipt=receipt,
    )


def reconcile_refill(config: RefillConfig) -> RefillReport:
    with ReconcileFileLock(_reconcile_lock_path(config)):
        return _reconcile_refill_locked(config)


def _reconcile_refill_locked(config: RefillConfig) -> RefillReport:
    policy = load_refill_policy(config)
    paths = _host_paths(config)
    snapshot = _capacity_snapshot(config, paths)
    active_running = snapshot.running
    active_queued = snapshot.queued
    feed_awaiting_import = snapshot.feed_awaiting_import
    awaiting_review = snapshot.awaiting_review

    if not policy.enabled or policy.desired_capacity == 0:
        return _report(
            config=config,
            policy=policy,
            status="PAUSED",
            message="Refill is paused; no new dispatch was attempted.",
            active_running=active_running,
            active_queued=active_queued,
            feed_awaiting_import=feed_awaiting_import,
            awaiting_review=awaiting_review,
            evidence={"reason": "paused", "automatic_mode": "paused", "health": snapshot.health},
        )
    if policy.desired_capacity > 1:
        raise RefillControllerError("v1 refill cannot reconcile capacity above one")
    if not snapshot.health.get("ok"):
        return _report(
            config=config,
            policy=policy,
            status="BLOCKED",
            message="Runtime health evidence is not fresh enough for automatic refill.",
            active_running=active_running,
            active_queued=active_queued,
            feed_awaiting_import=feed_awaiting_import,
            awaiting_review=awaiting_review,
            evidence={"reason": "runtime_health", "health": snapshot.health},
        )
    if active_queued + feed_awaiting_import >= policy.queue_cap:
        return _report(
            config=config,
            policy=policy,
            status="BACKPRESSURE",
            message="Queue backpressure blocks refill dispatch.",
            active_running=active_running,
            active_queued=active_queued,
            feed_awaiting_import=feed_awaiting_import,
            awaiting_review=awaiting_review,
            evidence={
                "reason": "queue_backpressure",
                "queue_cap": policy.queue_cap,
                "health": snapshot.health,
            },
        )
    if active_running >= 1:
        return _report(
            config=config,
            policy=policy,
            status="RUNNING",
            message="Capacity one is already occupied by a running job.",
            active_running=active_running,
            active_queued=active_queued,
            feed_awaiting_import=feed_awaiting_import,
            awaiting_review=awaiting_review,
            evidence={"reason": "running_capacity_full", "health": snapshot.health},
        )
    if active_queued + feed_awaiting_import >= 1:
        return _report(
            config=config,
            policy=policy,
            status="QUEUED",
            message="Capacity one is already occupied by a queued or submitted job.",
            active_running=active_running,
            active_queued=active_queued,
            feed_awaiting_import=feed_awaiting_import,
            awaiting_review=awaiting_review,
            evidence={"reason": "queued_capacity_full", "health": snapshot.health},
        )
    over_review = {
        repo: count
        for repo, count in awaiting_review.items()
        if count >= policy.review_cap_per_repository
    }
    if over_review:
        return _report(
            config=config,
            policy=policy,
            status="BACKPRESSURE",
            message="Review backpressure blocks refill dispatch.",
            active_running=active_running,
            active_queued=active_queued,
            feed_awaiting_import=feed_awaiting_import,
            awaiting_review=awaiting_review,
            evidence={
                "reason": "review_backpressure",
                "review_cap_per_repository": policy.review_cap_per_repository,
                "repositories": over_review,
                "health": snapshot.health,
            },
        )

    receipt = build_next(config.build_next)
    status = receipt.status
    message = receipt.message
    if status == "QUEUED":
        feed_awaiting_import = max(feed_awaiting_import, 1)
    elif status == "RUNNING":
        active_running = max(active_running, 1)
    return _report(
        config=config,
        policy=policy,
        status=status,
        message=message,
        active_running=active_running,
        active_queued=active_queued,
        feed_awaiting_import=feed_awaiting_import,
        awaiting_review=awaiting_review,
        evidence={
            "reason": "build_next_reconciled",
            "build_next": asdict(receipt),
            "health": snapshot.health,
        },
        receipt=receipt,
    )


class RefillService:
    def __init__(
        self,
        config: RefillConfig,
        *,
        interval_seconds: float = 30.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if interval_seconds <= 0:
            raise RefillControllerError("interval_seconds must be positive")
        self.config = config
        self.interval_seconds = interval_seconds
        self.sleeper = sleeper
        self.started_at: str | None = None
        self.last_reconcile: dict[str, Any] | None = None

    def _write_status(self, state: str, errors: tuple[str, ...] = ()) -> RefillServiceStatus:
        status = RefillServiceStatus(
            version=1,
            state=state,
            pid=os.getpid() if state != "stopped" else None,
            started_at=self.started_at,
            heartbeat_at=_utc_now(),
            last_reconcile=self.last_reconcile,
            errors=errors,
        )
        _atomic_write_json(_status_path(self.config), asdict(status))
        return status

    def read_status(self) -> RefillServiceStatus:
        path = _status_path(self.config)
        if not path.exists():
            return RefillServiceStatus(1, "not-started", None, None, _utc_now(), None)
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RefillControllerError("refill status must be a JSON object")
        return RefillServiceStatus(
            version=_required_int(raw, "version", 1),
            state=str(raw.get("state") or "unknown"),
            pid=int(raw["pid"]) if raw.get("pid") is not None else None,
            started_at=str(raw["started_at"]) if raw.get("started_at") else None,
            heartbeat_at=str(raw.get("heartbeat_at") or ""),
            last_reconcile=(
                dict(raw["last_reconcile"])
                if isinstance(raw.get("last_reconcile"), dict)
                else None
            ),
            errors=tuple(str(item) for item in raw.get("errors", ())),
        )

    def request_stop(self) -> Path:
        path = _refill_stop_path(self.config)
        _atomic_write_json(path, {"requested_at": _utc_now(), "token": secrets.token_hex(8)})
        return path

    def run_once(self) -> RefillReport:
        report = reconcile_refill(self.config)
        self.last_reconcile = report.decision_evidence
        self._write_status("running")
        return report

    def run_forever(self) -> None:
        self.started_at = _utc_now()
        stop_path = _refill_stop_path(self.config)
        stop_path.unlink(missing_ok=True)
        with HostProcessLock(_service_lock_path(self.config)):
            self._write_status("running")
            while not stop_path.exists():
                errors: list[str] = []
                try:
                    self.run_once()
                except BaseException as exc:
                    errors.append(str(exc))
                    self._write_status("running", tuple(errors))
                self.sleeper(self.interval_seconds)
            stop_path.unlink(missing_ok=True)
            self._write_status("stopped")


def render_refill_report_json(report: RefillReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True) + "\n"


def render_refill_service_status_json(status: RefillServiceStatus) -> str:
    return json.dumps(asdict(status), indent=2, sort_keys=True) + "\n"
