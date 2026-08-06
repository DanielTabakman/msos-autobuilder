"""Bounded capacity-one refill controller for accepted build-next dispatch."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .build_next import (
    BuildNextConfig,
    BuildNextReceipt,
    RefillAttemptContext,
    _prepare_feed_checkout,
    _source_identity,
    build_next,
)
from .codex_shadow import load_codex_host_config
from .lifecycle_evidence import (
    EvidenceHeadsLock,
    LifecycleEvidenceError,
    attempt_identity,
    canonical_refill_classification,
    canonical_refill_classification_locked,
    emit_lifecycle_evidence,
    record_producer_evidence_error,
)
from .managed_source import ManagedSourceSyncError, ensure_managed_source_fresh
from .persistent_host import (
    HostPaths,
    HostProcessLock,
    _pid_alive,
    load_persistent_host_config,
    parse_host_job,
)
from .service_error_lifecycle import (
    GATE_ERROR_SPEC,
    PUBLISHER_ERROR_SPEC,
    REVISION_ERROR_SPEC,
    evaluate_service_error_marker,
)


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


def _immutable_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    data = json.dumps(dict(payload), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise RefillControllerError(f"immutable receipt conflict: {path}")
        return
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    completed_supersession_id: str | None = None
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
        if self.completed_supersession_id is not None and not isinstance(
            self.completed_supersession_id, str
        ):
            raise RefillControllerError("completed_supersession_id must be a string or null")
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
        completed_supersession_id = raw.get("completed_supersession_id")
        if completed_supersession_id is not None and not isinstance(
            completed_supersession_id, str
        ):
            raise RefillControllerError("completed_supersession_id must be a string or null")
        return cls(
            version=_required_int(raw, "version", 1),
            enabled=_required_bool(raw, "enabled", False),
            desired_capacity=_required_int(raw, "desired_capacity", 0),
            resume_desired_capacity=resume_raw,
            completed_supersession_id=completed_supersession_id,
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
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    @classmethod
    def from_service_config(
        cls,
        service_config: str | Path,
        *,
        checkout_root: Path | None = None,
        max_snapshot_age_seconds: int = 600,
        requested_by: str = "capacity-one refill controller",
        submit: bool = True,
        policy_path: Path | None = None,
        max_host_heartbeat_age_seconds: int = 300,
    ) -> RefillConfig:
        config_path = Path(service_config).expanduser().resolve()
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RefillControllerError(f"invalid service config YAML: {config_path}") from exc
        if not isinstance(raw, dict):
            raise RefillControllerError("service config must be a mapping")

        service = load_persistent_host_config(config_path)
        supervisor_root: Path | None = None
        root_text = str(raw.get("supervisor_root") or "").strip()
        if root_text:
            supervisor_root = Path(root_text).expanduser()
            if not supervisor_root.is_absolute():
                supervisor_root = (config_path.parent / supervisor_root).resolve()
            else:
                supervisor_root = supervisor_root.resolve()

        if service.feed is None:
            feed_raw = raw.get("job_feed")
            if not isinstance(feed_raw, dict):
                raise RefillControllerError(
                    "paused/disabled job feed still requires a job_feed mapping with "
                    "repo_url/branch/path metadata for refill health"
                )
            if bool(feed_raw.get("enabled", False)):
                raise RefillControllerError(
                    "job feed metadata is inconsistent with host feed state"
                )
            feed_repo_url = str(feed_raw.get("repo_url") or "").strip()
            jobs_branch = str(feed_raw.get("branch") or "jobs").strip()
            jobs_path = str(feed_raw.get("path") or "jobs/approved").strip().replace("\\", "/")
            if not feed_repo_url:
                raise RefillControllerError(
                    "job_feed.repo_url is required even when feed is disabled"
                )
            host_config = load_codex_host_config(service.codex_host_config)
            build_config = BuildNextConfig(
                ppe_repo=host_config.source_repo,
                feed_repo_url=feed_repo_url,
                jobs_branch=jobs_branch,
                jobs_path=jobs_path,
                checkout_root=checkout_root,
                host_root=service.host_root,
                max_snapshot_age_seconds=max_snapshot_age_seconds,
                requested_by=requested_by,
                # Disabled feed must not clone or count the live jobs branch.
                submit=False,
            )
        else:
            build_config = BuildNextConfig.from_service_config(
                config_path,
                checkout_root=checkout_root,
                max_snapshot_age_seconds=max_snapshot_age_seconds,
                requested_by=requested_by,
                submit=submit,
            )
        return cls(
            build_next=build_config,
            policy_path=policy_path,
            max_host_heartbeat_age_seconds=max_host_heartbeat_age_seconds,
            supervisor_root=supervisor_root,
        )


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
class RefillSupersessionReceipt:
    version: int
    old_generation_id: str
    old_generation_sha256: str
    old_job_id: str | None
    old_work_item_id: str | None
    classification: dict[str, Any]
    ambiguity_evidence: dict[str, Any]
    requested_by: str
    active_release_commit: str
    superseded_at: str
    new_generation_id: str
    archive_path: str
    new_generation: dict[str, Any]


@dataclass(frozen=True)
class RefillServiceStatus:
    version: int
    state: str
    pid: int | None
    started_at: str | None
    heartbeat_at: str
    last_reconcile: dict[str, Any] | None
    errors: tuple[str, ...] = ()
    service_generation_id: str | None = None


@dataclass(frozen=True)
class CapacitySnapshot:
    running: int
    queued: int
    feed_awaiting_import: int
    awaiting_review: dict[str, int]
    health: dict[str, Any]


_EXPECTED_GENERATION_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_expected_generation_sha256(value: str) -> str:
    if not isinstance(value, str) or not _EXPECTED_GENERATION_SHA256_RE.fullmatch(value):
        raise RefillControllerError(
            "refill supersession expected generation SHA-256 must be exactly "
            "64 lowercase hexadecimal characters"
        )
    return value


def _generation_path(config: RefillConfig) -> Path:
    return _host_paths(config).state / "refill-generation.json"


def _prepared_dispatch_receipt_path(config: RefillConfig, prepared: Mapping[str, Any]) -> Path:
    generation_id = str(prepared.get("generation_id") or "unknown")
    job_id = str(prepared.get("job_id") or "unknown")
    digest = hashlib.sha256(
        json.dumps(dict(prepared), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (
        _host_paths(config).state
        / "refill-evidence"
        / "sources"
        / "dispatch-prepared"
        / generation_id
        / f"{job_id}.{digest}.json"
    )


def _generation_history_path(config: RefillConfig, generation: Mapping[str, Any]) -> Path:
    generation_id = str(generation.get("generation_id") or "unknown").strip() or "unknown"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", generation_id).strip(".-") or "unknown"
    return _host_paths(config).state / "refill-generation-history" / f"{safe}.json"


def _supersession_receipt_path(
    config: RefillConfig,
    *,
    old_generation_id: str,
    old_generation_sha256: str,
) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", old_generation_id).strip(".-") or "unknown"
    return (
        _host_paths(config).state
        / "refill-generation-supersessions"
        / f"{safe}-{old_generation_sha256}.json"
    )


def _load_generation_history(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefillControllerError("refill generation history is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise RefillControllerError("refill generation history must be a JSON object")
    return raw


def _generation_unresolved(generation: Mapping[str, Any]) -> str | None:
    if isinstance(generation.get("prepared_dispatch"), dict):
        return "prepared_dispatch"
    if isinstance(generation.get("current_attempt"), dict):
        return "current_attempt"
    if generation.get("state") in {
        "READY",
        "DISPATCHING",
        "QUEUED",
        "OCCUPIED",
        "RECOVERED",
        "RETRYING",
        "BACKPRESSURE",
        "BLOCKED",
        "PAUSED",
    }:
        return str(generation.get("state"))
    return None


def _generation_can_be_replaced(generation: Mapping[str, Any]) -> bool:
    return (
        generation.get("state") == "UNFILLED"
        and not isinstance(generation.get("current_attempt"), dict)
        and not isinstance(generation.get("prepared_dispatch"), dict)
    )


def _archive_generation_for_replacement(
    config: RefillConfig, generation: Mapping[str, Any]
) -> None:
    history = _generation_history_path(config, generation)
    payload = dict(generation)
    existing = _load_generation_history(history)
    if existing is not None:
        if existing != payload:
            raise RefillControllerError(
                "refill generation history conflicts with active generation"
            )
        return
    _atomic_write_json(history, payload)


def _new_generation(policy: RefillPolicy, *, now: str) -> dict[str, Any]:
    generation_id = f"refill-{secrets.token_hex(12)}"
    return {
        "version": 1,
        "generation_id": generation_id,
        "created_at": now,
        "founder_intent": "refill-keep-one",
        "desired_capacity": policy.desired_capacity,
        "source_ppe_identity": None,
        "attempt_sequence": [],
        "current_attempt": None,
        "attempted_work_item_ids": [],
        "item_scoped_terminal_exclusions": [],
        "provider_failure": None,
        "trustworthy_retry_at": None,
        "provider_retry_consumed": False,
        "state": "READY",
    }


def _new_superseded_generation_id(
    *,
    old_generation_id: str,
    old_generation_sha256: str,
) -> str:
    digest = hashlib.sha256(
        f"refill-supersession-v1\n{old_generation_id}\n{old_generation_sha256}\n".encode()
    ).hexdigest()
    return f"refill-superseded-{digest[:24]}"


def _new_superseded_generation(
    policy: RefillPolicy,
    *,
    now: str,
    old_generation_id: str,
    old_generation_sha256: str,
) -> dict[str, Any]:
    generation = _new_generation(policy, now=now)
    generation["generation_id"] = _new_superseded_generation_id(
        old_generation_id=old_generation_id,
        old_generation_sha256=old_generation_sha256,
    )
    generation["canonical_lifecycle_boundary"] = "post_recorder"
    return generation


def _load_supersession_receipt(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefillControllerError("refill supersession receipt is not valid JSON") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise RefillControllerError("refill supersession receipt must be a version 1 object")
    return raw


def _assert_supersession_receipt_matches(
    receipt: Mapping[str, Any],
    *,
    old_generation_id: str,
    old_generation_sha256: str,
    archive_path: Path,
    requested_by: str,
    active_release_commit: str,
    old_job_id: str | None,
    old_work_item_id: str | None,
    classification: Mapping[str, Any],
) -> None:
    new_generation = receipt.get("new_generation")
    expected_new_generation_id = _new_superseded_generation_id(
        old_generation_id=old_generation_id,
        old_generation_sha256=old_generation_sha256,
    )
    classification_evidence = dict(classification.get("evidence") or {})
    if (
        receipt.get("old_generation_id") != old_generation_id
        or receipt.get("old_generation_sha256") != old_generation_sha256
        or Path(str(receipt.get("archive_path") or "")) != archive_path
        or receipt.get("requested_by") != requested_by
        or receipt.get("active_release_commit") != active_release_commit
        or receipt.get("old_job_id") != old_job_id
        or receipt.get("old_work_item_id") != old_work_item_id
        or receipt.get("classification") != dict(classification)
        or receipt.get("ambiguity_evidence") != classification_evidence
        or not isinstance(new_generation, dict)
        or receipt.get("new_generation_id") != new_generation.get("generation_id")
        or new_generation.get("generation_id") != expected_new_generation_id
        or new_generation.get("version") != 1
        or new_generation.get("created_at") != receipt.get("superseded_at")
        or new_generation.get("founder_intent") != "refill-keep-one"
        or new_generation.get("desired_capacity") != 1
        or new_generation.get("source_ppe_identity") is not None
        or new_generation.get("current_attempt") is not None
        or new_generation.get("attempt_sequence") != []
        or new_generation.get("attempted_work_item_ids") != []
        or new_generation.get("item_scoped_terminal_exclusions") != []
        or new_generation.get("provider_failure") is not None
        or new_generation.get("trustworthy_retry_at") is not None
        or new_generation.get("provider_retry_consumed") is not False
        or new_generation.get("canonical_lifecycle_boundary") != "post_recorder"
        or new_generation.get("state") != "READY"
    ):
        raise RefillControllerError("refill supersession receipt conflicts with request")


def _assert_completed_supersession_receipt_matches(
    receipt: Mapping[str, Any],
    *,
    old_generation_id: str,
    old_generation_sha256: str,
    archive_path: Path,
    requested_by: str,
    active_release_commit: str,
) -> None:
    classification = receipt.get("classification")
    if not isinstance(classification, dict):
        raise RefillControllerError("refill supersession receipt conflicts with request")
    _assert_supersession_receipt_matches(
        receipt,
        old_generation_id=old_generation_id,
        old_generation_sha256=old_generation_sha256,
        archive_path=archive_path,
        requested_by=requested_by,
        active_release_commit=active_release_commit,
        old_job_id=(
            str(receipt.get("old_job_id")) if receipt.get("old_job_id") is not None else None
        ),
        old_work_item_id=(
            str(receipt.get("old_work_item_id"))
            if receipt.get("old_work_item_id") is not None
            else None
        ),
        classification=classification,
    )


def _supersession_completion_id(
    *,
    old_generation_id: str,
    old_generation_sha256: str,
) -> str:
    return f"{old_generation_id}:{old_generation_sha256}"


def _save_or_verify_supersession_archive(
    *,
    archive_path: Path,
    old_generation_bytes: bytes,
    old_generation_sha256: str,
) -> None:
    if archive_path.exists():
        archive_bytes = archive_path.read_bytes()
        if archive_bytes != old_generation_bytes:
            raise RefillControllerError("refill supersession archive conflicts with request")
    else:
        _atomic_write_bytes(archive_path, old_generation_bytes)
        archive_bytes = archive_path.read_bytes()
    if hashlib.sha256(archive_bytes).hexdigest() != old_generation_sha256:
        raise RefillControllerError("refill supersession archive SHA-256 verification failed")


def _verify_supersession_archive(
    *,
    archive_path: Path,
    old_generation_bytes: bytes,
    old_generation_sha256: str,
) -> None:
    if not archive_path.exists():
        raise RefillControllerError("refill supersession archive is missing")
    archive_bytes = archive_path.read_bytes()
    if archive_bytes != old_generation_bytes:
        raise RefillControllerError("refill supersession archive conflicts with request")
    if hashlib.sha256(archive_bytes).hexdigest() != old_generation_sha256:
        raise RefillControllerError("refill supersession archive SHA-256 verification failed")


def _verify_supersession_archive_sha(
    *,
    archive_path: Path,
    old_generation_sha256: str,
) -> None:
    if not archive_path.exists():
        raise RefillControllerError("refill supersession archive is missing")
    archive_bytes = archive_path.read_bytes()
    if hashlib.sha256(archive_bytes).hexdigest() != old_generation_sha256:
        raise RefillControllerError("refill supersession archive SHA-256 verification failed")


def _archived_supersession_attempt(
    *,
    archive_path: Path,
    old_generation_id: str,
    old_generation_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], str | None, str | None]:
    if not archive_path.exists():
        raise RefillControllerError("refill supersession archive is missing")
    archive_bytes = archive_path.read_bytes()
    if hashlib.sha256(archive_bytes).hexdigest() != old_generation_sha256:
        raise RefillControllerError("refill supersession archive SHA-256 verification failed")
    try:
        raw = json.loads(archive_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RefillControllerError("refill supersession archive is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise RefillControllerError("refill supersession archive must be a JSON object")
    if raw.get("version") != 1:
        raise RefillControllerError("refill supersession archive must be a version 1 object")
    if raw.get("generation_id") != old_generation_id:
        raise RefillControllerError("refill supersession archive generation ID mismatch")
    current = dict(_supersession_current_attempt(raw))
    current["generation_id"] = old_generation_id
    old_job_id = str(current.get("job_id") or "") or None
    old_work_item_id = str(current.get("work_item_id") or "") or None
    return raw, current, old_job_id, old_work_item_id


def _assert_supersession_receipt_archive_identity(
    receipt: Mapping[str, Any],
    *,
    old_job_id: str | None,
    old_work_item_id: str | None,
) -> None:
    if (
        receipt.get("old_job_id") != old_job_id
        or receipt.get("old_work_item_id") != old_work_item_id
    ):
        raise RefillControllerError("refill supersession receipt conflicts with archive")


def _supersession_report(
    *,
    config: RefillConfig,
    policy: RefillPolicy,
    snapshot: CapacitySnapshot,
    receipt: Mapping[str, Any],
    idempotent: bool,
) -> RefillReport:
    return _report(
        config=config,
        policy=policy,
        status="SUPERSEDED",
        message="Refill generation was superseded without dispatch.",
        active_running=snapshot.running,
        active_queued=snapshot.queued,
        feed_awaiting_import=snapshot.feed_awaiting_import,
        awaiting_review=snapshot.awaiting_review,
        evidence=_supersession_evidence(
            snapshot=snapshot,
            receipt=receipt,
            idempotent=idempotent,
        ),
    )


def _supersession_evidence(
    *,
    snapshot: CapacitySnapshot,
    receipt: Mapping[str, Any],
    idempotent: bool,
) -> dict[str, Any]:
    return {
        "reason": "generation_superseded",
        "idempotent": idempotent,
        "supersession": {
            key: receipt.get(key)
            for key in (
                "old_generation_id",
                "old_generation_sha256",
                "old_job_id",
                "old_work_item_id",
                "classification",
                "ambiguity_evidence",
                "requested_by",
                "active_release_commit",
                "superseded_at",
                "new_generation_id",
                "archive_path",
            )
        },
        "health": snapshot.health,
    }


def _read_only_supersession_report(
    *,
    policy: RefillPolicy,
    snapshot: CapacitySnapshot,
    receipt: Mapping[str, Any],
) -> RefillReport:
    status = "SUPERSEDED"
    message = "Refill generation supersession was already complete."
    decision = {
        "decided_at": _utc_now(),
        "status": status,
        "message": message,
        "enabled": policy.enabled,
        "desired_capacity": policy.desired_capacity,
        "active_running": snapshot.running,
        "active_queued": snapshot.queued,
        "feed_awaiting_import": snapshot.feed_awaiting_import,
        "awaiting_review": dict(snapshot.awaiting_review),
        **_supersession_evidence(snapshot=snapshot, receipt=receipt, idempotent=True),
    }
    return RefillReport(
        status=status,
        enabled=policy.enabled,
        desired_capacity=policy.desired_capacity,
        active_running=snapshot.running,
        active_queued=snapshot.queued,
        feed_awaiting_import=snapshot.feed_awaiting_import,
        awaiting_review=dict(snapshot.awaiting_review),
        message=message,
        decision_evidence=decision,
        build_next_receipt=None,
    )


def load_refill_generation(config: RefillConfig) -> dict[str, Any] | None:
    path = _generation_path(config)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefillControllerError(f"refill generation is not valid JSON: {path}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise RefillControllerError("refill generation must be a version 1 JSON object")
    return raw


def save_refill_generation(config: RefillConfig, generation: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(generation)
    _atomic_write_json(_generation_path(config), payload)
    return payload


def _attempt_context(
    generation: Mapping[str, Any],
    *,
    attempt_ordinal: int,
    retry_ordinal: int,
    reason: str,
    selected_work_item_id: str | None = None,
) -> RefillAttemptContext:
    return RefillAttemptContext(
        generation_id=str(generation["generation_id"]),
        attempt_ordinal=attempt_ordinal,
        retry_ordinal=retry_ordinal,
        reason=reason,
        selected_work_item_id=selected_work_item_id,
    )


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


def _read_refill_stop_request(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _refill_stop_applies_to_generation(
    request: Mapping[str, Any] | None,
    service_generation_id: str,
) -> bool:
    if request is None:
        return False
    requested_generation = request.get("service_generation_id")
    if requested_generation is None:
        return True
    return str(requested_generation) == service_generation_id


def _read_refill_service_lock_owner(config: RefillConfig) -> dict[str, Any]:
    try:
        raw = json.loads(_service_lock_path(config).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


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
        completed_supersession_id=policy.completed_supersession_id,
        dispatch_window=policy.dispatch_window,
        queue_cap=policy.queue_cap,
        review_cap_per_repository=policy.review_cap_per_repository,
        last_decision_evidence=policy.last_decision_evidence,
    )
    generation = load_refill_generation(config)
    if generation is not None:
        if not _generation_can_be_replaced(generation):
            unresolved = _generation_unresolved(generation) or str(
                generation.get("state") or "unknown"
            )
            raise RefillControllerError(
                "cannot replace unresolved refill generation "
                f"{generation.get('generation_id')!r}: {unresolved}"
            )
        _archive_generation_for_replacement(config, generation)
    saved = save_refill_policy(config, updated)
    save_refill_generation(config, _new_generation(saved, now=config.clock().isoformat()))
    return saved


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
        completed_supersession_id=policy.completed_supersession_id,
        dispatch_window=policy.dispatch_window,
        queue_cap=policy.queue_cap,
        review_cap_per_repository=policy.review_cap_per_repository,
        last_decision_evidence=policy.last_decision_evidence,
    )
    saved = save_refill_policy(config, updated)
    generation = load_refill_generation(config)
    if generation is not None:
        generation["state"] = "PAUSED"
        generation["desired_capacity"] = 0
        save_refill_generation(config, generation)
    return saved


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
        completed_supersession_id=policy.completed_supersession_id,
        dispatch_window=policy.dispatch_window,
        queue_cap=policy.queue_cap,
        review_cap_per_repository=policy.review_cap_per_repository,
        last_decision_evidence=policy.last_decision_evidence,
    )
    saved = save_refill_policy(config, updated)
    generation = load_refill_generation(config)
    if generation is not None:
        generation["state"] = "READY"
        generation["desired_capacity"] = capacity
        save_refill_generation(config, generation)
    return saved


def keep_one_running_and_reconcile(config: RefillConfig) -> RefillReport:
    with ReconcileFileLock(_reconcile_lock_path(config)):
        _keep_one_running_locked(config)
        return _reconcile_refill_locked(config)


def supersede_refill_generation(
    config: RefillConfig,
    *,
    expected_generation_id: str,
    expected_generation_sha256: str,
) -> RefillReport:
    expected_generation_sha256 = validate_expected_generation_sha256(
        expected_generation_sha256
    )
    with ReconcileFileLock(_reconcile_lock_path(config)):
        return _supersede_refill_generation_locked(
            config,
            expected_generation_id=expected_generation_id,
            expected_generation_sha256=expected_generation_sha256,
        )


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


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _attempt_archive(paths: HostPaths, job_id: str) -> tuple[str | None, Path | None]:
    if (paths.completed / job_id).is_dir():
        return "completed", paths.completed / job_id
    if (paths.failed / job_id).is_dir():
        return "failed", paths.failed / job_id
    return None, None


def _job_in_active_queue(paths: HostPaths, job_id: str) -> bool:
    if (paths.running / f"{job_id}.yaml").exists() or (paths.pending / f"{job_id}.yaml").exists():
        return True
    return False


def _job_in_feed(config: RefillConfig, job_id: str) -> bool:
    if not config.build_next.submit:
        return False
    checkout = config.build_next.checkout_root or (
        Path(tempfile.gettempdir()) / "msos-autobuilder-build-next-feed"
    )
    feed_job = checkout.expanduser().resolve() / config.build_next.jobs_path / f"{job_id}.yaml"
    return feed_job.exists()


def _read_job_yaml(path: Path) -> dict[str, Any] | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return raw if isinstance(raw, dict) else None


def _job_yaml_sources(
    config: RefillConfig,
    paths: HostPaths,
) -> list[tuple[str, Path, dict[str, Any]]]:
    sources: list[tuple[str, Path, dict[str, Any]]] = []
    if config.build_next.submit:
        checkout = config.build_next.checkout_root or (
            Path(tempfile.gettempdir()) / "msos-autobuilder-build-next-feed"
        )
        feed_root = checkout.expanduser().resolve() / config.build_next.jobs_path
        if feed_root.exists():
            for path in sorted(feed_root.glob("*.yaml")):
                payload = _read_job_yaml(path)
                if payload is not None:
                    sources.append(("feed", path, payload))
    for stage, root in (("pending", paths.pending), ("running", paths.running)):
        for path in sorted(root.glob("*.yaml")):
            payload = _read_job_yaml(path)
            if payload is not None:
                sources.append((stage, path, payload))
    for stage, root in (("completed", paths.completed), ("failed", paths.failed)):
        if not root.exists():
            continue
        for archive in sorted(path for path in root.iterdir() if path.is_dir()):
            path = archive / "job.yaml"
            payload = _read_job_yaml(path)
            if payload is not None:
                sources.append((stage, path, payload))
    return sources


def _extract_refill_attempt_candidate(
    generation: Mapping[str, Any],
    *,
    stage: str,
    path: Path,
    job: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    job_id = str(job.get("job_id") or "").strip()
    founder = job.get("founder_build_next")
    if not job_id or not isinstance(founder, dict):
        return None, None
    attempt = founder.get("refill_attempt")
    if not isinstance(attempt, dict):
        return None, None
    if attempt.get("generation_id") != generation.get("generation_id"):
        return None, None
    candidate_validation = job.get("candidate_validation")
    if isinstance(candidate_validation, dict) and candidate_validation.get("job_id") != job_id:
        return None, "candidate validation job_id disagrees with job_id"
    evidence = founder.get("portfolio_selection_evidence")
    if isinstance(evidence, dict) and isinstance(evidence.get("refill_attempt"), dict):
        if dict(evidence["refill_attempt"]) != dict(attempt):
            return None, "portfolio selection refill_attempt disagrees"
    attempt_ordinal = attempt.get("attempt_ordinal")
    retry_ordinal = attempt.get("retry_ordinal", 0)
    if not isinstance(attempt_ordinal, int) or isinstance(attempt_ordinal, bool):
        return None, "refill attempt ordinal is malformed"
    if attempt_ordinal < 1:
        return None, "refill attempt ordinal must be positive"
    if not isinstance(retry_ordinal, int) or isinstance(retry_ordinal, bool) or retry_ordinal < 0:
        return None, "refill retry ordinal is malformed"
    work_item_id = str(founder.get("work_item_id") or "").strip()
    selected_work_item_id = str(attempt.get("selected_work_item_id") or "").strip()
    if not work_item_id or selected_work_item_id != work_item_id:
        return None, "refill selected work item disagrees with job evidence"
    pipeline_id = str(founder.get("pipeline_id") or "").strip()
    if not pipeline_id:
        return None, "refill attempt lacks pipeline identity"
    source = founder.get("source")
    source_commit = source.get("commit") if isinstance(source, dict) else None
    if not isinstance(source_commit, str) or not source_commit:
        return None, "refill attempt lacks source commit evidence"
    return {
        "attempt_ordinal": attempt_ordinal,
        "retry_ordinal": retry_ordinal,
        "reason": str(attempt.get("reason") or "initial"),
        "job_id": job_id,
        "work_item_id": work_item_id,
        "pipeline_id": pipeline_id,
        "source_commit": source_commit,
        "feed_path": str(path) if stage == "feed" else None,
        "feed_commit": None,
        "created_at": _utc_now(),
        "recovered_from": {"stage": stage, "path": str(path)},
    }, None


def _recover_unrecorded_generation_attempt(
    config: RefillConfig,
    paths: HostPaths,
    generation: dict[str, Any],
) -> dict[str, Any] | None:
    recorded = {
        str(item.get("job_id") or "")
        for item in generation.get("attempt_sequence") or []
        if isinstance(item, dict)
    }
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for stage, path, job in _job_yaml_sources(config, paths):
        candidate, error = _extract_refill_attempt_candidate(
            generation,
            stage=stage,
            path=path,
            job=job,
        )
        if error is not None:
            errors.append({"path": str(path), "stage": stage, "error": error})
        if candidate is None:
            continue
        if candidate["job_id"] in recorded:
            continue
        candidates.append(candidate)
    if errors:
        generation["state"] = "BLOCKED"
        generation["recovery_error"] = {
            "reason": "invalid_refill_attempt_evidence",
            "errors": errors,
        }
        save_refill_generation(config, generation)
        return generation["recovery_error"]
    def candidate_key(candidate: Mapping[str, Any]) -> str:
        return json.dumps(
            {
                "attempt_ordinal": candidate.get("attempt_ordinal"),
                "retry_ordinal": candidate.get("retry_ordinal"),
                "reason": candidate.get("reason"),
                "job_id": candidate.get("job_id"),
                "work_item_id": candidate.get("work_item_id"),
                "pipeline_id": candidate.get("pipeline_id"),
                "source_commit": candidate.get("source_commit"),
            },
            sort_keys=True,
        )

    unique = {candidate_key(candidate) for candidate in candidates}
    if len(unique) > 1:
        generation["state"] = "BLOCKED"
        generation["recovery_error"] = {
            "reason": "ambiguous_refill_attempt_recovery",
            "candidates": candidates,
        }
        save_refill_generation(config, generation)
        return generation["recovery_error"]
    if not candidates:
        return None
    candidate = sorted(
        candidates,
        key=lambda item: {
            "running": 0,
            "pending": 1,
            "failed": 2,
            "completed": 3,
            "feed": 4,
        }.get(str(item.get("recovered_from", {}).get("stage")), 5),
    )[0]
    expected_ordinal = len(generation.get("attempt_sequence") or []) + 1
    if candidate["attempt_ordinal"] != expected_ordinal:
        generation["state"] = "BLOCKED"
        generation["recovery_error"] = {
            "reason": "refill_attempt_ordinal_conflict",
            "expected_attempt_ordinal": expected_ordinal,
            "candidate": candidate,
        }
        save_refill_generation(config, generation)
        return generation["recovery_error"]
    generation.setdefault("attempt_sequence", []).append(candidate)
    generation["current_attempt"] = candidate
    ids = list(generation.get("attempted_work_item_ids") or [])
    if candidate["work_item_id"] not in ids:
        ids.append(candidate["work_item_id"])
    generation["attempted_work_item_ids"] = ids
    generation["state"] = "RECOVERED"
    generation["last_attempt_recovery"] = candidate["recovered_from"]
    save_refill_generation(config, generation)
    return None


def _publisher_seen(paths: HostPaths, job_id: str) -> dict[str, Any] | None:
    raw = _read_json_file(paths.state / "controlled-publisher-seen.json") or {}
    entry = raw.get(job_id)
    return entry if isinstance(entry, dict) else None


def _gate_report(paths: HostPaths, job_id: str) -> dict[str, Any] | None:
    root = paths.state / "candidate-gate-results-repo" / "results"
    if not root.exists():
        return None
    for path in root.glob(f"*/{job_id}/gate-report.json"):
        raw = _read_json_file(path)
        if raw is not None:
            return raw
    return None


def _revision_seen(paths: HostPaths, job_id: str) -> dict[str, Any] | None:
    raw = _read_json_file(paths.state / "revision-loop-seen.json") or {}
    for key, entry in raw.items():
        if str(key).endswith(f"/{job_id}") and isinstance(entry, dict):
            return entry
        if isinstance(entry, dict) and entry.get("source_job_id") == job_id:
            return entry
    return None


def _has_trustworthy_offset(value: Any) -> bool:
    text = str(value or "").strip()
    return bool(text.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", text))


def _load_revision_ledger(paths: HostPaths) -> dict[str, Any]:
    path = paths.state / "revision-loop-seen.json"
    if not path.exists():
        return {"state": "absent", "path": str(path), "entries": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "state": "malformed",
            "path": str(path),
            "error": str(exc),
            "entries": {},
        }
    if not isinstance(raw, dict):
        return {
            "state": "non_object",
            "path": str(path),
            "raw_type": type(raw).__name__,
            "entries": {},
        }
    return {"state": "object", "path": str(path), "entries": raw}


def _revision_lineage_entries(
    ledger: Mapping[str, Any], source_job_id: str
) -> list[dict[str, Any]]:
    raw = ledger.get("entries")
    entries: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return entries
    for key, entry in raw.items():
        key_source = str(key).rsplit("/", 1)[-1]
        explicit_source = (
            str(entry.get("source_job_id"))
            if isinstance(entry, dict) and entry.get("source_job_id") is not None
            else None
        )
        targeted = key_source == source_job_id or explicit_source == source_job_id
        if not targeted:
            continue
        if not isinstance(entry, dict):
            entries.append(
                {
                    "source_job_id": explicit_source or key_source,
                    "revision_job_id": "",
                    "_lineage_error": "targeted revision entry is not an object",
                    "_ledger_key_source": key_source,
                    "_raw_type": type(entry).__name__,
                }
            )
            continue
        normalized = dict(entry)
        normalized["source_job_id"] = explicit_source or key_source
        normalized["revision_job_id"] = str(entry.get("revision_job_id") or "")
        normalized["_ledger_key_source"] = key_source
        if explicit_source is not None and explicit_source != key_source:
            normalized["_lineage_error"] = "revision source disagrees with ledger key"
        entries.append(normalized)
    return entries


def _revision_descendants(
    ledger: Mapping[str, Any], source_job_id: str
) -> list[dict[str, Any]]:
    return _revision_lineage_entries(ledger, source_job_id)


def _revision_lineage_error(entry: Mapping[str, Any], source_job_id: str) -> str | None:
    if entry.get("_lineage_error"):
        return str(entry["_lineage_error"])
    if entry.get("source_job_id") != source_job_id:
        return "revision source identity does not match source job"
    revision_job_id = str(entry.get("revision_job_id") or "")
    if not revision_job_id:
        return "revision lineage lacks revision_job_id"
    if revision_job_id == source_job_id:
        return "revision lineage points source to itself"
    gate_hash = str(entry.get("gate_report_sha256") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", gate_hash):
        return "revision lineage gate_report_sha256 is malformed"
    jobs_commit = str(entry.get("jobs_commit") or "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", jobs_commit):
        return "revision lineage jobs_commit is malformed"
    queued_at = entry.get("queued_at")
    if not (isinstance(queued_at, str) and _has_trustworthy_offset(queued_at)):
        return "revision lineage queued_at is not offset-aware"
    if _parse_utc(queued_at) is None:
        return "revision lineage queued_at is malformed"
    return None


def _revision_lineage_classification(
    config: RefillConfig,
    paths: HostPaths,
    source_job_id: str,
) -> dict[str, Any] | None:
    ledger = _load_revision_ledger(paths)
    ledger_state = ledger.get("state")
    if ledger_state == "absent":
        return None
    if ledger_state != "object":
        return {
            "category": "unknown",
            "stage": "revision_lineage",
            "evidence": {
                "error": "revision ledger is malformed",
                "ledger": ledger,
            },
        }
    entries = _revision_lineage_entries(ledger, source_job_id)
    if not entries:
        return None
    errors = [
        error
        for entry in entries
        if (error := _revision_lineage_error(entry, source_job_id))
    ]
    revision_ids = {str(entry.get("revision_job_id") or "") for entry in entries}
    if errors or source_job_id in revision_ids or len(revision_ids) != 1 or len(entries) != 1:
        return {
            "category": "unknown",
            "stage": "revision_lineage",
            "evidence": {
                "error": "malformed or conflicting revision lineage",
                "errors": errors,
                "entries": entries,
            },
        }
    entry = entries[0]
    revision_job_id = str(entry["revision_job_id"])
    descendant_entries = _revision_descendants(ledger, revision_job_id)
    if descendant_entries:
        return {
            "category": "unknown",
            "stage": "revision_lineage",
            "evidence": {
                "error": "revision descendant has its own revision lineage",
                "entries": entries,
                "descendants": descendant_entries,
            },
        }
    lineage = {
        "source_job_id": source_job_id,
        "revision_job_id": revision_job_id,
        "gate_report_sha256": entry.get("gate_report_sha256"),
        "jobs_commit": entry.get("jobs_commit"),
        "queued_at": entry.get("queued_at"),
        "seen": entry,
    }
    if _job_in_active_queue(paths, revision_job_id):
        return {"category": "in_flight", "stage": "revision_queue", "evidence": lineage}
    revision_archive_state, revision_archive = _attempt_archive(paths, revision_job_id)
    if revision_archive_state == "failed" and revision_archive is not None:
        failed = _classify_failed_attempt(revision_archive, now=config.clock())
        return {
            **failed,
            "stage": "revision_failed",
            "evidence": {**failed.get("evidence", {}), "revision": lineage},
        }
    if revision_archive_state == "completed":
        publisher = _publisher_seen(paths, revision_job_id)
        if publisher is not None:
            return {
                "category": "item_terminal",
                "stage": "revision_publisher",
                "evidence": {**lineage, "publisher": publisher},
            }
        gate = _gate_report(paths, revision_job_id)
        if gate is not None:
            status = str(gate.get("status") or "")
            state = str(gate.get("state") or "")
            evidence = {**lineage, "gate": gate}
            if status == "passed" and state in {"candidate_passed", "awaiting_review"}:
                return {
                    "category": "in_flight",
                    "stage": "revision_publisher_review",
                    "evidence": evidence,
                }
            if status == "failed" or state in {"candidate_failed", "candidate_rejected"}:
                return {
                    "category": "unknown",
                    "stage": "revision_descendant_disposition_missing",
                    "evidence": {
                        **evidence,
                        "reason": (
                            "failed revision gate is not terminal without trusted "
                            "publisher disposition"
                        ),
                    },
                }
        return {"category": "in_flight", "stage": "revision_downstream", "evidence": lineage}
    if _job_in_feed(config, revision_job_id):
        return {"category": "in_flight", "stage": "revision_awaiting_import", "evidence": lineage}
    return {"category": "in_flight", "stage": "revision_pending", "evidence": lineage}


def _structured_provider_failure(
    error: Mapping[str, Any], *, now: datetime
) -> dict[str, Any] | None:
    provider = error.get("provider_failure")
    if not isinstance(provider, dict):
        return None
    retry_at = _parse_utc(provider.get("retry_at"))
    trustworthy_retry_at = (
        isinstance(provider.get("retry_at"), str)
        and _has_trustworthy_offset(provider.get("retry_at"))
        and retry_at is not None
    )
    retryable = (
        provider.get("version") == 1
        and provider.get("scope") == "provider"
        and provider.get("temporary") is True
        and provider.get("retryable") is True
        and trustworthy_retry_at
    )
    return {
        "category": "provider_systemic",
        "retryable": retryable,
        "trustworthy_retry_at": (
            retry_at.isoformat().replace("+00:00", "Z") if retryable else None
        ),
        "provider_failure": dict(provider),
        "provider": provider.get("provider"),
        "classifier": {
            "source": (
                provider.get("classifier_source")
                or provider.get("source")
                or "structured_provider_failure"
            ),
            "version": provider.get("classifier_version") or 1,
        },
        "reason": provider.get("reason") or provider.get("message") or "provider_failure",
    }


def _classify_failed_attempt(archive: Path, *, now: datetime) -> dict[str, Any]:
    error = _read_json_file(archive / "error.json") or {}
    message = " ".join(str(error.get(key) or "") for key in ("message", "traceback"))
    structured = _structured_provider_failure(error, now=now)
    if structured is not None:
        return {
            **structured,
            "evidence": {
                "archive": str(archive),
                "error_sha256": _sha256_file(archive / "error.json"),
                "provider_failure": structured["provider_failure"],
                "provider": structured.get("provider"),
                "classifier": structured.get("classifier"),
                "retryable": structured.get("retryable"),
                "retry_at": structured.get("trustworthy_retry_at"),
                "reason": structured.get("reason"),
            },
        }
    lowered = message.lower()
    provider_tokens = ("quota", "rate limit", "rate-limit", "authentication", "auth")
    if any(token in lowered for token in provider_tokens):
        return {
            "category": "provider_systemic",
            "retryable": False,
            "trustworthy_retry_at": None,
            "evidence": {
                "archive": str(archive),
                "error_sha256": _sha256_file(archive / "error.json"),
                "provider": "codex" if "codex" in lowered else None,
                "classifier": {"source": "recognized_provider_text", "version": 1},
                "retryable": False,
                "retry_at": None,
                "reason": message[:300],
            },
        }
    return {
        "category": "unknown",
        "evidence": {
            "archive": str(archive),
            "error_sha256": _sha256_file(archive / "error.json"),
            "message_excerpt": message[:300],
        },
    }


def _classify_attempt(
    config: RefillConfig,
    paths: HostPaths,
    attempt: Mapping[str, Any],
) -> dict[str, Any]:
    job_id = str(attempt.get("job_id") or "")
    if not job_id:
        return {"category": "unknown", "evidence": {"error": "attempt lacks job_id"}}
    if config.build_next.host_root is not None:
        try:
            canonical = canonical_refill_classification(
                config.build_next.host_root,
                job_id=job_id,
                generation_id=str(attempt.get("generation_id") or "")
                or str((load_refill_generation(config) or {}).get("generation_id") or "")
                or None,
            )
        except LifecycleEvidenceError as exc:
            return {
                "category": "unknown",
                "stage": "canonical_lifecycle",
                "evidence": {
                    "reason": "canonical_lifecycle_unfresh",
                    "error": str(exc),
                    "job_id": job_id,
                    "refill_action": "block_fail_closed",
                },
            }
        if canonical is not None:
            return canonical
    if _job_in_active_queue(paths, job_id):
        return {"category": "in_flight", "stage": "host_queue"}
    archive_state, archive = _attempt_archive(paths, job_id)
    if archive_state is None or archive is None:
        if _job_in_feed(config, job_id):
            return {"category": "in_flight", "stage": "awaiting_import"}
        return {
            "category": "unknown",
            "stage": "missing_attempt_evidence",
            "evidence": {
                "error": (
                    "tracked refill attempt has no feed, pending, running, completed, "
                    "or failed evidence"
                ),
                "job_id": job_id,
            },
        }
    if archive_state == "failed":
        return _classify_failed_attempt(archive, now=config.clock())

    publisher = _publisher_seen(paths, job_id)
    if publisher is not None:
        return {"category": "item_terminal", "stage": "publisher", "evidence": publisher}
    revision_classification = _revision_lineage_classification(config, paths, job_id)
    if revision_classification is not None:
        return revision_classification
    gate = _gate_report(paths, job_id)
    if gate is not None:
        status = str(gate.get("status") or "")
        state = str(gate.get("state") or "")
        if status == "failed" or state in {"candidate_failed", "candidate_rejected"}:
            return {
                "category": "unknown",
                "stage": "revision_disposition_missing",
                "evidence": {
                    "gate": gate,
                    "reason": (
                        "failed source gate is not terminal without trusted publisher "
                        "or revision disposition"
                    ),
                },
            }
        if status == "passed" and state in {"candidate_passed", "awaiting_review"}:
            return {"category": "in_flight", "stage": "publisher_review", "evidence": gate}
    return {"category": "in_flight", "stage": "downstream_disposition"}


def _attempt_from_receipt(
    receipt: BuildNextReceipt,
    *,
    attempt_ordinal: int,
    retry_ordinal: int,
    reason: str,
    decision_basis: Mapping[str, Any] | None = None,
    recovered_from: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    attempt = {
        "attempt_ordinal": attempt_ordinal,
        "retry_ordinal": retry_ordinal,
        "reason": reason,
        "job_id": receipt.job_id,
        "work_item_id": receipt.work_item_id,
        "pipeline_id": receipt.pipeline_id,
        "source_commit": receipt.source_commit,
        "feed_path": receipt.feed_path,
        "feed_commit": receipt.feed_commit,
        "created_at": _utc_now(),
    }
    if decision_basis is not None:
        attempt["decision_basis"] = dict(decision_basis)
    if recovered_from is not None:
        attempt["recovered_from"] = dict(recovered_from)
    return attempt


def _record_attempt(generation: dict[str, Any], attempt: Mapping[str, Any]) -> None:
    job_id = str(attempt.get("job_id") or "")
    sequence = [
        item
        for item in generation.get("attempt_sequence") or []
        if isinstance(item, dict)
    ]
    if not any(str(item.get("job_id") or "") == job_id for item in sequence):
        sequence.append(dict(attempt))
    generation["attempt_sequence"] = sequence
    generation["current_attempt"] = dict(attempt)
    ids = list(generation.get("attempted_work_item_ids") or [])
    work_item_id = attempt.get("work_item_id")
    if work_item_id and work_item_id not in ids:
        ids.append(work_item_id)
    generation["attempted_work_item_ids"] = ids


def _prepared_matches_candidate(
    prepared: Mapping[str, Any], candidate: Mapping[str, Any]
) -> bool:
    return all(
        prepared.get(key) == candidate.get(key)
        for key in (
            "generation_id",
            "attempt_ordinal",
            "retry_ordinal",
            "reason",
            "selected_work_item_id",
            "pipeline_id",
            "source_commit",
            "job_id",
        )
    )


def _prepared_action_identity(prepared: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "refill_action_identity.v1",
        "generation_id": prepared.get("generation_id"),
        "attempt_ordinal": prepared.get("attempt_ordinal"),
        "retry_ordinal": prepared.get("retry_ordinal"),
        "reason": prepared.get("reason"),
        "selected_work_item_id": prepared.get("selected_work_item_id"),
        "pipeline_id": prepared.get("pipeline_id"),
        "source_commit": prepared.get("source_commit"),
        "job_id": prepared.get("job_id"),
        "feed_path": prepared.get("feed_path"),
    }


def _bind_decision_basis_v1(
    basis: Mapping[str, Any] | None,
    prepared: Mapping[str, Any],
) -> dict[str, Any] | None:
    if basis is None:
        return None
    bound = dict(basis)
    if bound.get("decision_basis_schema_version") == "decision_basis.v1":
        bound["new_action_identity"] = _prepared_action_identity(prepared)
    return bound


def _prepared_decision_basis_error(
    config: RefillConfig,
    prepared: Mapping[str, Any],
    *,
    evidence_heads_locked: bool = False,
) -> dict[str, Any] | None:
    basis = prepared.get("decision_basis")
    if not isinstance(basis, Mapping):
        return None
    if basis.get("decision_basis_schema_version") != "decision_basis.v1":
        return {"reason": "prepared_dispatch_basis_schema_mismatch", "basis": dict(basis)}
    expected_identity = _prepared_action_identity(prepared)
    if basis.get("new_action_identity") != expected_identity:
        return {
            "reason": "prepared_dispatch_action_identity_mismatch",
            "basis": dict(basis),
            "expected_action_identity": expected_identity,
        }
    prior = basis.get("prior_canonical_identity")
    if not isinstance(prior, Mapping):
        return {"reason": "prepared_dispatch_basis_missing_prior_identity", "basis": dict(basis)}
    host_root = config.build_next.host_root
    if host_root is None:
        return None
    try:
        classifier = (
            canonical_refill_classification_locked
            if evidence_heads_locked
            else canonical_refill_classification
        )
        canonical = classifier(
            host_root,
            job_id=str(prior.get("job_id") or ""),
            generation_id=str(prior.get("generation_id") or "") or None,
        )
    except LifecycleEvidenceError as exc:
        return {
            "reason": "prepared_dispatch_basis_canonical_unfresh",
            "error": str(exc),
            "basis": dict(basis),
        }
    evidence = canonical.get("evidence") if isinstance(canonical, Mapping) else None
    if not isinstance(evidence, Mapping):
        return {"reason": "prepared_dispatch_basis_canonical_missing", "basis": dict(basis)}
    if (
        canonical.get("category") != "item_terminal"
        or basis.get("prior_snapshot_sha256") != evidence.get("snapshot_sha256")
        or basis.get("prior_latest_evidence_set_sha256")
        != evidence.get("latest_evidence_set_sha256")
        or basis.get("prior_refill_action") != evidence.get("refill_action")
    ):
        return {
            "reason": "prepared_dispatch_basis_canonical_mismatch",
            "basis": dict(basis),
            "canonical": dict(canonical),
        }
    return None


def _commit_prepared_dispatch_under_evidence_lock(
    config: RefillConfig,
    generation: dict[str, Any],
    prepared: dict[str, Any],
    *,
    decision_basis: Mapping[str, Any] | None,
    consume_provider_retry: bool,
    source_evidence: Mapping[str, Any],
) -> dict[str, Any] | None:
    host_root = config.build_next.host_root
    if host_root is None:
        bound_basis = _bind_decision_basis_v1(decision_basis, prepared)
        if bound_basis is not None:
            prepared["decision_basis"] = bound_basis
            generation["last_refill_action_basis"] = bound_basis
        generation["prepared_dispatch"] = prepared
        generation["state"] = "DISPATCHING"
        if consume_provider_retry:
            generation["provider_retry_consumed"] = True
        if source_evidence.get("source"):
            generation["source_ppe_identity"] = source_evidence["source"]
        save_refill_generation(config, generation)
        return None
    with EvidenceHeadsLock(host_root):
        bound_basis = _bind_decision_basis_v1(decision_basis, prepared)
        if bound_basis is not None:
            prepared["decision_basis"] = bound_basis
            basis_error = _prepared_decision_basis_error(
                config,
                prepared,
                evidence_heads_locked=True,
            )
            if basis_error is not None:
                generation["state"] = "BLOCKED"
                generation["dispatch_error"] = basis_error
                save_refill_generation(config, generation)
                return basis_error
            generation["last_refill_action_basis"] = bound_basis
        generation["prepared_dispatch"] = prepared
        generation["state"] = "DISPATCHING"
        if consume_provider_retry:
            generation["provider_retry_consumed"] = True
        if source_evidence.get("source"):
            generation["source_ppe_identity"] = source_evidence["source"]
        save_refill_generation(config, generation)
    return None


def _candidate_from_prepared_job(
    generation: Mapping[str, Any],
    prepared: Mapping[str, Any],
    *,
    stage: str,
    path: Path,
    job: Mapping[str, Any],
) -> dict[str, Any] | None:
    candidate, error = _extract_refill_attempt_candidate(
        generation,
        stage=stage,
        path=path,
        job=job,
    )
    if error is not None or candidate is None:
        return None
    candidate["generation_id"] = generation.get("generation_id")
    candidate["selected_work_item_id"] = candidate.get("work_item_id")
    return candidate if _prepared_matches_candidate(prepared, candidate) else None


def _matching_prepared_side_effects(
    config: RefillConfig,
    paths: HostPaths,
    generation: Mapping[str, Any],
    prepared: Mapping[str, Any],
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    conflicts: list[dict[str, str]] = []
    for stage, path, job in _job_yaml_sources(config, paths):
        job_id = str(job.get("job_id") or "")
        candidate = _candidate_from_prepared_job(
            generation,
            prepared,
            stage=stage,
            path=path,
            job=job,
        )
        if candidate is not None:
            matches.append(candidate)
        elif job_id == prepared.get("job_id"):
            conflicts.append({"stage": stage, "path": str(path)})
    if conflicts:
        return [{"conflict": True, "conflicts": conflicts}]
    unique = {
        json.dumps(
            {key: item.get(key) for key in ("job_id", "attempt_ordinal", "retry_ordinal")},
            sort_keys=True,
        )
        for item in matches
    }
    if len(unique) > 1:
        return [{"conflict": True, "conflicts": matches}]
    return matches


def _commit_prepared_attempt(
    config: RefillConfig,
    generation: dict[str, Any],
    candidate: Mapping[str, Any],
) -> None:
    attempt = dict(candidate)
    attempt.pop("generation_id", None)
    attempt.pop("selected_work_item_id", None)
    _record_attempt(generation, attempt)
    generation.pop("prepared_dispatch", None)
    generation["state"] = "QUEUED"
    generation["last_attempt_recovery"] = dict(attempt.get("recovered_from") or {})
    save_refill_generation(config, generation)


def _prepare_dispatch(
    config: RefillConfig,
    generation: dict[str, Any],
    *,
    exclusions: tuple[str, ...],
    attempt_ordinal: int,
    retry_ordinal: int,
    reason: str,
    selected_work_item_id: str | None,
    pinned_commit: str | None,
    active_running: int = 0,
    active_queued: int = 0,
    consume_provider_retry: bool = False,
    decision_basis: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], BuildNextReceipt]:
    dry_config = replace(
        config.build_next,
        requested_by="capacity-one refill controller",
        submit=False,
        exclude_work_item_ids=exclusions,
        expected_source_commit=pinned_commit,
        refill_attempt=_attempt_context(
            generation,
            attempt_ordinal=attempt_ordinal,
            retry_ordinal=retry_ordinal,
            reason=reason,
            selected_work_item_id=selected_work_item_id,
        ),
    )
    receipt = build_next(dry_config)
    if receipt.projected_status != "QUEUED" or not receipt.job_id:
        return {}, receipt
    prepared = {
        "generation_id": generation.get("generation_id"),
        "attempt_ordinal": attempt_ordinal,
        "retry_ordinal": retry_ordinal,
        "reason": reason,
        "selected_work_item_id": receipt.work_item_id,
        "pipeline_id": receipt.pipeline_id,
        "source_commit": receipt.source_commit,
        "job_id": receipt.job_id,
        "exclusions": list(exclusions),
        "prepared_at": _utc_now(),
        "feed_path": receipt.feed_path,
        "feed_commit": receipt.feed_commit,
    }
    basis_error = _commit_prepared_dispatch_under_evidence_lock(
        config,
        generation,
        prepared,
        decision_basis=decision_basis,
        consume_provider_retry=consume_provider_retry,
        source_evidence=receipt.evidence,
    )
    if basis_error is not None:
        return {}, BuildNextReceipt(
            status="BLOCKED",
            pipeline_id=receipt.pipeline_id,
            work_item_id=receipt.work_item_id,
            job_id=receipt.job_id,
            repository=receipt.repository,
            source_commit=receipt.source_commit,
            feed_path=receipt.feed_path,
            feed_commit=receipt.feed_commit,
            message="Prepared refill dispatch is blocked by stale canonical basis.",
            evidence={"reason": basis_error.get("reason"), "dispatch_error": basis_error},
            submitted=False,
            projected_status=receipt.projected_status,
            publication_enabled=receipt.publication_enabled,
            merge_enabled=receipt.merge_enabled,
            product_main_write_enabled=receipt.product_main_write_enabled,
        )
    identity = None
    work_item_digest = receipt.evidence.get("work_item_source_sha256_v1")
    if isinstance(work_item_digest, str) and config.build_next.host_root is not None:
        try:
            source_receipt = _prepared_dispatch_receipt_path(config, prepared)
            _immutable_write_json(
                source_receipt,
                {
                    "version": 1,
                    "receipt_type": "dispatch.prepared.source",
                    "prepared_dispatch": prepared,
                    "generation_id": generation.get("generation_id"),
                    "job_id": receipt.job_id,
                    "recorded_at": prepared["prepared_at"],
                },
            )
            identity = attempt_identity(
                pipeline_id=str(receipt.pipeline_id or ""),
                work_item_id=str(receipt.work_item_id or ""),
                work_item_digest=work_item_digest,
                generation_id=str(generation.get("generation_id") or ""),
                job_id=str(receipt.job_id or ""),
                attempt_ordinal=attempt_ordinal,
                retry_ordinal=retry_ordinal,
            )
            dispatch_intent_sha256 = hashlib.sha256(
                json.dumps(prepared, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            emit_lifecycle_evidence(
                config.build_next.host_root,
                evidence_kind="dispatch.prepared",
                identity=identity,
                source_path=source_receipt,
                payload={
                    "selected_work_item": {
                        "pipeline_id": str(receipt.pipeline_id or ""),
                        "work_item_id": str(receipt.work_item_id or ""),
                        "work_item_source_sha256_v1": work_item_digest,
                    },
                    "generation_id": str(generation.get("generation_id") or ""),
                    "dispatch_intent_sha256": dispatch_intent_sha256,
                    "capacity_slot": {
                        "slot_id": "capacity-one",
                        "desired_capacity": 1,
                        "active_running": active_running,
                        "active_queued": active_queued,
                    },
                },
                final=True,
                closed_status="final",
                observed_at=str(prepared["prepared_at"]),
            )
        except Exception as exc:
            try:
                record_producer_evidence_error(
                    config.build_next.host_root,
                    producer="refill_controller",
                    evidence_kind="dispatch.prepared",
                    error=exc,
                    identity=identity,
                    primary_outcome={"state": "DISPATCHING", "job_id": receipt.job_id},
                )
            except Exception:
                pass
    return prepared, receipt


def _submit_prepared_dispatch(
    config: RefillConfig,
    generation: dict[str, Any],
    prepared: Mapping[str, Any],
) -> BuildNextReceipt:
    build_config = replace(
        config.build_next,
        requested_by="capacity-one refill controller",
        exclude_work_item_ids=tuple(str(item) for item in prepared.get("exclusions") or ()),
        expected_source_commit=str(prepared.get("source_commit") or "") or None,
        refill_attempt=_attempt_context(
            generation,
            attempt_ordinal=int(prepared["attempt_ordinal"]),
            retry_ordinal=int(prepared.get("retry_ordinal") or 0),
            reason=str(prepared.get("reason") or "initial"),
            selected_work_item_id=str(prepared.get("selected_work_item_id") or ""),
        ),
    )
    receipt = build_next(build_config)
    if receipt.status == "QUEUED" and any(
        receipt_value != prepared_value
        for receipt_value, prepared_value in (
            (receipt.job_id, prepared.get("job_id")),
            (receipt.work_item_id, prepared.get("selected_work_item_id")),
            (receipt.pipeline_id, prepared.get("pipeline_id")),
            (receipt.source_commit, prepared.get("source_commit")),
        )
    ):
        generation["state"] = "BLOCKED"
        generation["dispatch_error"] = {
            "reason": "prepared_dispatch_identity_mismatch",
            "prepared": dict(prepared),
            "receipt": asdict(receipt),
        }
        save_refill_generation(config, generation)
    return receipt


def _recover_or_replay_prepared_dispatch(
    config: RefillConfig,
    paths: HostPaths,
    generation: dict[str, Any],
    *,
    allow_replay: bool,
) -> BuildNextReceipt | dict[str, Any] | None:
    prepared = generation.get("prepared_dispatch")
    if not isinstance(prepared, dict):
        return None
    basis_error = _prepared_decision_basis_error(config, prepared)
    if basis_error is not None:
        generation["state"] = "BLOCKED"
        generation["dispatch_error"] = basis_error
        save_refill_generation(config, generation)
        return generation["dispatch_error"]
    matches = _matching_prepared_side_effects(config, paths, generation, prepared)
    if matches and matches[0].get("conflict") is True:
        generation["state"] = "BLOCKED"
        generation["dispatch_error"] = {"reason": "prepared_dispatch_conflict", **matches[0]}
        save_refill_generation(config, generation)
        return generation["dispatch_error"]
    if matches:
        stage_order = {"running": 0, "pending": 1, "completed": 2, "failed": 3, "feed": 4}
        candidate = sorted(
            matches,
            key=lambda item: stage_order.get(
                str(item.get("recovered_from", {}).get("stage")), 5
            ),
        )[0]
        if isinstance(prepared.get("decision_basis"), Mapping):
            candidate["decision_basis"] = dict(prepared["decision_basis"])
        _commit_prepared_attempt(config, generation, candidate)
        return None
    if not allow_replay:
        return None
    receipt = _submit_prepared_dispatch(config, generation, prepared)
    if receipt.status == "QUEUED" and generation.get("state") != "BLOCKED":
        attempt = _attempt_from_receipt(
            receipt,
            attempt_ordinal=int(prepared["attempt_ordinal"]),
            retry_ordinal=int(prepared.get("retry_ordinal") or 0),
            reason=str(prepared.get("reason") or "initial"),
            decision_basis=(
                prepared.get("decision_basis")
                if isinstance(prepared.get("decision_basis"), Mapping)
                else None
            ),
        )
        _record_attempt(generation, attempt)
        generation.pop("prepared_dispatch", None)
        generation["state"] = "QUEUED"
        if receipt.evidence.get("source"):
            generation["source_ppe_identity"] = receipt.evidence["source"]
        save_refill_generation(config, generation)
    return receipt


def _append_attempt(
    generation: dict[str, Any],
    receipt: BuildNextReceipt,
    *,
    retry_ordinal: int,
    reason: str,
) -> None:
    attempt = {
        "attempt_ordinal": len(generation.get("attempt_sequence") or []) + 1,
        "retry_ordinal": retry_ordinal,
        "reason": reason,
        "job_id": receipt.job_id,
        "work_item_id": receipt.work_item_id,
        "pipeline_id": receipt.pipeline_id,
        "source_commit": receipt.source_commit,
        "feed_path": receipt.feed_path,
        "feed_commit": receipt.feed_commit,
        "created_at": _utc_now(),
    }
    _record_attempt(generation, attempt)
    if receipt.evidence.get("source"):
        generation["source_ppe_identity"] = receipt.evidence["source"]


def _pinned_source_commit(generation: Mapping[str, Any]) -> str | None:
    source = generation.get("source_ppe_identity")
    if isinstance(source, dict) and isinstance(source.get("commit"), str):
        commit = source["commit"]
        if commit:
            return commit
    return None


def _source_pin_block(config: RefillConfig, generation: Mapping[str, Any]) -> dict[str, Any] | None:
    pinned = _pinned_source_commit(generation)
    if pinned is None:
        return None
    try:
        identity = _source_identity(
            replace(config.build_next, expected_source_commit=pinned),
            config.build_next.ppe_repo.expanduser().resolve(),
        )
    except Exception as exc:
        return {"reason": "source_pin_mismatch", "pinned_commit": pinned, "error": str(exc)}
    return None if identity.commit == pinned else {
        "reason": "source_pin_mismatch",
        "pinned_commit": pinned,
        "observed_commit": identity.commit,
    }


def _prove_refill_source_freshness(
    config: RefillConfig,
    *,
    expected_source_commit: str | None,
) -> dict[str, Any]:
    result = ensure_managed_source_fresh(
        config.build_next.ppe_repo,
        source_remote=config.build_next.source_remote,
        source_ref=config.build_next.source_ref,
        expected_source_repository=config.build_next.expected_source_repository,
        allow_test_local_source_remote=config.build_next.allow_test_local_source_remote,
        expected_source_commit=expected_source_commit,
    )
    return result.evidence


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
    service_error_checks = {
        spec.service: evaluate_service_error_marker(
            state_root=paths.state,
            service_checks=service_checks,
            spec=spec,
        )
        for spec in (PUBLISHER_ERROR_SPEC, GATE_ERROR_SPEC, REVISION_ERROR_SPEC)
    }
    checks["service_error_markers"] = {
        "ok": all(item["ok"] for item in service_error_checks.values()),
        "services": service_error_checks,
    }
    checks["publisher_state"] = service_error_checks["publisher"]
    if not checks["service_error_markers"]["ok"]:
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
        completed_supersession_id=policy.completed_supersession_id,
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


def _superseded_policy(
    policy: RefillPolicy,
    *,
    completed_supersession_id: str,
) -> RefillPolicy:
    return RefillPolicy(
        enabled=True,
        desired_capacity=1,
        resume_desired_capacity=1,
        completed_supersession_id=completed_supersession_id,
        dispatch_window=policy.dispatch_window,
        queue_cap=policy.queue_cap,
        review_cap_per_repository=policy.review_cap_per_repository,
        last_decision_evidence=policy.last_decision_evidence,
    )


def _supersession_current_attempt(generation: Mapping[str, Any]) -> dict[str, Any]:
    current = generation.get("current_attempt")
    if not isinstance(current, dict):
        raise RefillControllerError("refill supersession requires current_attempt")
    return current


def _assert_supersession_preconditions(
    *,
    policy: RefillPolicy,
    snapshot: CapacitySnapshot,
    generation: Mapping[str, Any],
    classification: Mapping[str, Any],
) -> None:
    if policy.enabled or policy.desired_capacity != 0:
        raise RefillControllerError("refill supersession requires paused desired capacity zero")
    if not snapshot.health.get("ok"):
        raise RefillControllerError("refill supersession requires green runtime health")
    if snapshot.running:
        raise RefillControllerError("refill supersession requires zero running jobs")
    if snapshot.queued:
        raise RefillControllerError("refill supersession requires zero pending jobs")
    if snapshot.feed_awaiting_import:
        raise RefillControllerError("refill supersession requires zero feed-awaiting-import jobs")
    if isinstance(generation.get("prepared_dispatch"), dict):
        raise RefillControllerError("refill supersession refuses prepared_dispatch")

    category = classification.get("category")
    if category == "in_flight":
        raise RefillControllerError("refill supersession refuses in-flight attempt")
    if category == "item_terminal":
        raise RefillControllerError("refill supersession refuses item-terminal attempt")
    if (
        category == "provider_systemic"
        and classification.get("retryable") is True
        and generation.get("provider_retry_consumed") is not True
    ):
        raise RefillControllerError("refill supersession refuses authorized provider retry")
    if category != "unknown":
        raise RefillControllerError("refill supersession requires acknowledged ambiguity")


def _supersede_refill_generation_locked(
    config: RefillConfig,
    *,
    expected_generation_id: str,
    expected_generation_sha256: str,
) -> RefillReport:
    expected_generation_sha256 = validate_expected_generation_sha256(
        expected_generation_sha256
    )
    generation_path = _generation_path(config)
    if not generation_path.exists():
        raise RefillControllerError("refill supersession requires an active generation")
    old_generation_bytes = generation_path.read_bytes()
    old_generation_sha256 = hashlib.sha256(old_generation_bytes).hexdigest()
    retry_receipt_path = _supersession_receipt_path(
        config,
        old_generation_id=expected_generation_id,
        old_generation_sha256=expected_generation_sha256,
    )
    retry_receipt = _load_supersession_receipt(retry_receipt_path)
    retry_active = load_refill_generation(config)
    if old_generation_sha256 != expected_generation_sha256 and retry_receipt is not None:
        policy = load_refill_policy(config)
        snapshot = _capacity_snapshot(config, _host_paths(config))
        active_release_commit = str(
            snapshot.health.get("checks", {}).get("active_release", {}).get("commit") or ""
        )
        retry_archive_path = _generation_history_path(
            config,
            {"generation_id": expected_generation_id},
        )
        completion_id = _supersession_completion_id(
            old_generation_id=expected_generation_id,
            old_generation_sha256=expected_generation_sha256,
        )
        _assert_completed_supersession_receipt_matches(
            retry_receipt,
            old_generation_id=expected_generation_id,
            old_generation_sha256=expected_generation_sha256,
            archive_path=retry_archive_path,
            requested_by=config.build_next.requested_by,
            active_release_commit=active_release_commit,
        )
        _verify_supersession_archive_sha(
            archive_path=retry_archive_path,
            old_generation_sha256=expected_generation_sha256,
        )
        (
            archived_generation,
            archived_current,
            archived_old_job_id,
            archived_old_work_item_id,
        ) = _archived_supersession_attempt(
            archive_path=retry_archive_path,
            old_generation_id=expected_generation_id,
            old_generation_sha256=expected_generation_sha256,
        )
        _assert_supersession_receipt_archive_identity(
            retry_receipt,
            old_job_id=archived_old_job_id,
            old_work_item_id=archived_old_work_item_id,
        )
        if (
            retry_active is not None
            and retry_active.get("generation_id") == retry_receipt.get("new_generation_id")
        ):
            if policy.completed_supersession_id == completion_id:
                return _read_only_supersession_report(
                    policy=policy,
                    snapshot=snapshot,
                    receipt=retry_receipt,
                )
            if retry_active != retry_receipt.get("new_generation"):
                raise RefillControllerError(
                    "refill supersession active generation conflicts with receipt"
                )
            if policy.completed_supersession_id is not None:
                raise RefillControllerError(
                    "refill supersession completion binding conflicts with request"
                )
            fresh_classification = _classify_attempt(
                config, _host_paths(config), archived_current
            )
            if (
                fresh_classification != retry_receipt.get("classification")
                or retry_receipt.get("ambiguity_evidence")
                != dict(fresh_classification.get("evidence") or {})
            ):
                raise RefillControllerError(
                    "refill supersession archived attempt classification changed"
                )
            _assert_supersession_preconditions(
                policy=policy,
                snapshot=snapshot,
                generation=archived_generation,
                classification=fresh_classification,
            )
            saved_policy = save_refill_policy(
                config,
                _superseded_policy(policy, completed_supersession_id=completion_id),
            )
            return _supersession_report(
                config=config,
                policy=saved_policy,
                snapshot=snapshot,
                receipt=retry_receipt,
                idempotent=True,
            )
        raise RefillControllerError("refill supersession active generation conflicts with receipt")
    if old_generation_sha256 != expected_generation_sha256:
        raise RefillControllerError("refill supersession generation SHA-256 mismatch")
    generation = retry_active
    if generation is None:
        raise RefillControllerError("refill supersession requires an active generation")
    old_generation_id = str(generation.get("generation_id") or "")
    if old_generation_id != expected_generation_id:
        raise RefillControllerError("refill supersession generation ID mismatch")

    archive_path = _generation_history_path(config, generation)
    receipt_path = _supersession_receipt_path(
        config,
        old_generation_id=old_generation_id,
        old_generation_sha256=old_generation_sha256,
    )
    policy = load_refill_policy(config)
    snapshot = _capacity_snapshot(config, _host_paths(config))
    active_release_commit = str(
        snapshot.health.get("checks", {}).get("active_release", {}).get("commit") or ""
    )
    existing_receipt = _load_supersession_receipt(receipt_path)
    current = _supersession_current_attempt(generation)
    classification = _classify_attempt(config, _host_paths(config), current)
    old_job_id = str(current.get("job_id") or "") or None
    old_work_item_id = str(current.get("work_item_id") or "") or None
    completion_id = _supersession_completion_id(
        old_generation_id=old_generation_id,
        old_generation_sha256=old_generation_sha256,
    )

    if existing_receipt is not None:
        if (
            policy.completed_supersession_id is not None
            and policy.completed_supersession_id != completion_id
        ):
            raise RefillControllerError(
                "refill supersession completion binding conflicts with request"
            )
        _assert_supersession_receipt_matches(
            existing_receipt,
            old_generation_id=old_generation_id,
            old_generation_sha256=old_generation_sha256,
            archive_path=archive_path,
            requested_by=config.build_next.requested_by,
            active_release_commit=active_release_commit,
            old_job_id=old_job_id,
            old_work_item_id=old_work_item_id,
            classification=classification,
        )
        _verify_supersession_archive(
            archive_path=archive_path,
            old_generation_bytes=old_generation_bytes,
            old_generation_sha256=old_generation_sha256,
        )
        _assert_supersession_preconditions(
            policy=policy,
            snapshot=snapshot,
            generation=generation,
            classification=classification,
        )
        new_generation = dict(existing_receipt["new_generation"])
        save_refill_generation(config, new_generation)
        saved_policy = save_refill_policy(
            config,
            _superseded_policy(policy, completed_supersession_id=completion_id),
        )
        return _supersession_report(
            config=config,
            policy=saved_policy,
            snapshot=snapshot,
            receipt=existing_receipt,
            idempotent=True,
        )

    _assert_supersession_preconditions(
        policy=policy,
        snapshot=snapshot,
        generation=generation,
        classification=classification,
    )

    saved_policy = _superseded_policy(policy, completed_supersession_id=completion_id)
    now = config.clock().isoformat()
    new_generation = _new_superseded_generation(
        saved_policy,
        now=now,
        old_generation_id=old_generation_id,
        old_generation_sha256=old_generation_sha256,
    )
    receipt = RefillSupersessionReceipt(
        version=1,
        old_generation_id=old_generation_id,
        old_generation_sha256=old_generation_sha256,
        old_job_id=old_job_id,
        old_work_item_id=old_work_item_id,
        classification=classification,
        ambiguity_evidence=dict(classification.get("evidence") or {}),
        requested_by=config.build_next.requested_by,
        active_release_commit=active_release_commit,
        superseded_at=now,
        new_generation_id=str(new_generation["generation_id"]),
        archive_path=str(archive_path),
        new_generation=new_generation,
    )
    _save_or_verify_supersession_archive(
        archive_path=archive_path,
        old_generation_bytes=old_generation_bytes,
        old_generation_sha256=old_generation_sha256,
    )
    _atomic_write_json(receipt_path, asdict(receipt))
    loaded_receipt = _load_supersession_receipt(receipt_path)
    if loaded_receipt is None:
        raise RefillControllerError("refill supersession receipt was not persisted")
    _assert_supersession_receipt_matches(
        loaded_receipt,
        old_generation_id=old_generation_id,
        old_generation_sha256=old_generation_sha256,
        archive_path=archive_path,
        requested_by=config.build_next.requested_by,
        active_release_commit=active_release_commit,
        old_job_id=old_job_id,
        old_work_item_id=old_work_item_id,
        classification=classification,
    )
    save_refill_generation(config, new_generation)
    saved_policy = save_refill_policy(config, saved_policy)
    return _supersession_report(
        config=config,
        policy=saved_policy,
        snapshot=snapshot,
        receipt=loaded_receipt,
        idempotent=False,
    )


def reconcile_refill(config: RefillConfig) -> RefillReport:
    with ReconcileFileLock(_reconcile_lock_path(config)):
        return _reconcile_refill_locked(config)


def _reconcile_refill_locked(config: RefillConfig) -> RefillReport:
    policy = load_refill_policy(config)
    paths = _host_paths(config)
    generation = load_refill_generation(config)
    snapshot = _capacity_snapshot(config, paths)
    active_running = snapshot.running
    active_queued = snapshot.queued
    feed_awaiting_import = snapshot.feed_awaiting_import
    awaiting_review = snapshot.awaiting_review

    if not policy.enabled or policy.desired_capacity == 0:
        if generation is not None and generation.get("state") != "PAUSED":
            generation["state"] = "PAUSED"
            save_refill_generation(config, generation)
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
    if generation is not None and isinstance(generation.get("prepared_dispatch"), dict):
        prepared_result = _recover_or_replay_prepared_dispatch(
            config, paths, generation, allow_replay=False
        )
        if isinstance(prepared_result, dict):
            return _report(
                config=config,
                policy=policy,
                status="BLOCKED",
                message="Prepared refill dispatch has conflicting side-effect evidence.",
                active_running=active_running,
                active_queued=active_queued,
                feed_awaiting_import=feed_awaiting_import,
                awaiting_review=awaiting_review,
                evidence={
                    "reason": prepared_result.get("reason", "prepared_dispatch_conflict"),
                    "generation": generation,
                    "health": snapshot.health,
                },
            )
        if isinstance(prepared_result, BuildNextReceipt):
            if prepared_result.status == "QUEUED":
                feed_awaiting_import = max(feed_awaiting_import, 1)
            return _report(
                config=config,
                policy=policy,
                status=prepared_result.status,
                message=prepared_result.message,
                active_running=active_running,
                active_queued=active_queued,
                feed_awaiting_import=feed_awaiting_import,
                awaiting_review=awaiting_review,
                evidence={
                    "reason": "prepared_dispatch_reconciled",
                    "build_next": asdict(prepared_result),
                    "generation": generation,
                    "health": snapshot.health,
                },
                receipt=prepared_result,
            )
        generation = load_refill_generation(config) or generation

    if generation is not None:
        recovery_error = _recover_unrecorded_generation_attempt(config, paths, generation)
        if recovery_error is not None:
            return _report(
                config=config,
                policy=policy,
                status="BLOCKED",
                message="Refill generation attempt recovery found conflicting evidence.",
                active_running=active_running,
                active_queued=active_queued,
                feed_awaiting_import=feed_awaiting_import,
                awaiting_review=awaiting_review,
                evidence={
                    "reason": recovery_error["reason"],
                    "generation": generation,
                    "health": snapshot.health,
                },
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
    if generation is None:
        return _report(
            config=config,
            policy=policy,
            status="BLOCKED",
            message="Refill is enabled but no founder-created generation exists.",
            active_running=active_running,
            active_queued=active_queued,
            feed_awaiting_import=feed_awaiting_import,
            awaiting_review=awaiting_review,
            evidence={"reason": "missing_generation", "health": snapshot.health},
        )
    if isinstance(generation.get("prepared_dispatch"), dict):
        prepared_result = _recover_or_replay_prepared_dispatch(
            config, paths, generation, allow_replay=True
        )
        if isinstance(prepared_result, dict):
            return _report(
                config=config,
                policy=policy,
                status="BLOCKED",
                message="Prepared refill dispatch has conflicting side-effect evidence.",
                active_running=active_running,
                active_queued=active_queued,
                feed_awaiting_import=feed_awaiting_import,
                awaiting_review=awaiting_review,
                evidence={
                    "reason": prepared_result.get("reason", "prepared_dispatch_conflict"),
                    "generation": generation,
                    "health": snapshot.health,
                },
            )
        if isinstance(prepared_result, BuildNextReceipt):
            if prepared_result.status == "QUEUED":
                feed_awaiting_import = max(feed_awaiting_import, 1)
            return _report(
                config=config,
                policy=policy,
                status=prepared_result.status,
                message=prepared_result.message,
                active_running=active_running,
                active_queued=active_queued,
                feed_awaiting_import=feed_awaiting_import,
                awaiting_review=awaiting_review,
                evidence={
                    "reason": "prepared_dispatch_reconciled",
                    "build_next": asdict(prepared_result),
                    "generation": generation,
                    "health": snapshot.health,
                },
                receipt=prepared_result,
            )

    current = generation.get("current_attempt")
    retry_same_item: str | None = None
    retry_reason = "initial"
    retry_ordinal = 0
    decision_basis: dict[str, Any] | None = None
    if isinstance(current, dict):
        pin_block = _source_pin_block(config, generation)
        if pin_block is not None:
            generation["state"] = "BLOCKED"
            save_refill_generation(config, generation)
            return _report(
                config=config,
                policy=policy,
                status="BLOCKED",
                message="Pinned PPE source moved; refill dispatch is blocked.",
                active_running=active_running,
                active_queued=active_queued,
                feed_awaiting_import=feed_awaiting_import,
                awaiting_review=awaiting_review,
                evidence={
                    **pin_block,
                    "generation": generation,
                    "health": snapshot.health,
                },
            )
        classification = _classify_attempt(config, paths, current)
        generation["last_attempt_classification"] = classification
        category = classification.get("category")
        if category == "in_flight":
            generation["state"] = "OCCUPIED"
            save_refill_generation(config, generation)
            return _report(
                config=config,
                policy=policy,
                status="RUNNING",
                message="Capacity one is occupied by a tracked refill attempt.",
                active_running=active_running,
                active_queued=active_queued,
                feed_awaiting_import=feed_awaiting_import,
                awaiting_review=awaiting_review,
                evidence={
                    "reason": "tracked_attempt_capacity_full",
                    "generation": generation,
                    "health": snapshot.health,
                },
            )
        if category == "provider_systemic":
            provider_state = {
                "work_item_id": current.get("work_item_id"),
                "job_id": current.get("job_id"),
                "retryable": classification.get("retryable") is True,
                "trustworthy_retry_at": classification.get("trustworthy_retry_at")
                or generation.get("trustworthy_retry_at"),
                "evidence": classification.get("evidence"),
            }
            generation["provider_failure"] = provider_state
            generation["trustworthy_retry_at"] = provider_state["trustworthy_retry_at"]
            retry_at = _parse_utc(provider_state["trustworthy_retry_at"])
            now = config.clock()
            if (
                provider_state["retryable"]
                and retry_at is not None
                and now >= retry_at
                and generation.get("provider_retry_consumed") is not True
            ):
                retry_same_item = str(current.get("work_item_id") or "")
                retry_reason = "provider_retry"
                retry_ordinal = 1
            else:
                generation["state"] = "BACKPRESSURE"
                save_refill_generation(config, generation)
                return _report(
                    config=config,
                    policy=policy,
                    status="BACKPRESSURE",
                    message="Provider-wide backpressure blocks refill dispatch.",
                    active_running=active_running,
                    active_queued=active_queued,
                    feed_awaiting_import=feed_awaiting_import,
                    awaiting_review=awaiting_review,
                    evidence={
                        "reason": "provider_backpressure",
                        "generation": generation,
                        "health": snapshot.health,
                    },
                )
        elif category == "item_terminal":
            evidence = classification.get("evidence")
            if (
                classification.get("stage") == "canonical_lifecycle"
                and isinstance(evidence, Mapping)
            ):
                decision_basis = {
                    "decision_basis_schema_version": "decision_basis.v1",
                    "basis_kind": "prior_work_item",
                    "prior_canonical_identity": evidence.get("attempt_identity"),
                    "prior_snapshot_sha256": evidence.get("snapshot_sha256"),
                    "prior_latest_evidence_set_sha256": evidence.get(
                        "latest_evidence_set_sha256"
                    ),
                    "prior_item_disposition": evidence.get("reason"),
                    "prior_refill_action": evidence.get("refill_action"),
                    "action_type": "exclude_and_dispatch_next",
                    "new_action_identity": None,
                }
            exclusions = list(generation.get("item_scoped_terminal_exclusions") or [])
            work_item_id = str(current.get("work_item_id") or "")
            if work_item_id and work_item_id not in exclusions:
                exclusions.append(work_item_id)
            generation["item_scoped_terminal_exclusions"] = exclusions
            if decision_basis is not None:
                generation["last_refill_action_basis"] = decision_basis
            generation["current_attempt"] = None
            generation["provider_failure"] = None
            generation["trustworthy_retry_at"] = None
            generation["state"] = "READY"
            save_refill_generation(config, generation)
        else:
            classification_evidence = classification.get("evidence")
            block_reason = "ambiguous_attempt"
            if (
                str(classification.get("stage") or "").startswith("canonical_lifecycle")
                and isinstance(classification_evidence, Mapping)
                and classification_evidence.get("reason")
            ):
                block_reason = str(classification_evidence["reason"])
            generation["state"] = "BLOCKED"
            save_refill_generation(config, generation)
            return _report(
                config=config,
                policy=policy,
                status="BLOCKED",
                message="Tracked refill attempt is blocked by canonical terminal evidence.",
                active_running=active_running,
                active_queued=active_queued,
                feed_awaiting_import=feed_awaiting_import,
                awaiting_review=awaiting_review,
                evidence={
                    "reason": block_reason,
                    "generation": generation,
                    "health": snapshot.health,
                },
            )

    exclusions = tuple(
        str(item) for item in generation.get("item_scoped_terminal_exclusions") or ()
    )
    if retry_same_item:
        exclusions = tuple(item for item in exclusions if item != retry_same_item)
    attempt_ordinal = len(generation.get("attempt_sequence") or []) + 1
    pinned_commit = _pinned_source_commit(generation)
    try:
        source_freshness = _prove_refill_source_freshness(
            config,
            expected_source_commit=pinned_commit,
        )
    except ManagedSourceSyncError as exc:
        generation["state"] = "BLOCKED"
        save_refill_generation(config, generation)
        return _report(
            config=config,
            policy=policy,
            status="BLOCKED",
            message=str(exc),
            active_running=active_running,
            active_queued=active_queued,
            feed_awaiting_import=feed_awaiting_import,
            awaiting_review=awaiting_review,
            evidence={
                "reason": "source_freshness",
                "source_freshness": exc.evidence,
                "generation": generation,
                "health": snapshot.health,
            },
        )
    prepared, dry_receipt = _prepare_dispatch(
        config,
        generation,
        exclusions=exclusions,
        attempt_ordinal=attempt_ordinal,
        retry_ordinal=retry_ordinal,
        reason=retry_reason,
        selected_work_item_id=retry_same_item,
        pinned_commit=pinned_commit,
        active_running=active_running,
        active_queued=active_queued,
        consume_provider_retry=retry_reason == "provider_retry",
        decision_basis=decision_basis or generation.get("last_refill_action_basis"),
    )
    if (
        dry_receipt.source_commit
        and source_freshness.get("new_commit") != dry_receipt.source_commit
    ):
        generation["state"] = "BLOCKED"
        save_refill_generation(config, generation)
        return _report(
            config=config,
            policy=policy,
            status="BLOCKED",
            message="Refill source freshness proof disagrees with build-next source identity.",
            active_running=active_running,
            active_queued=active_queued,
            feed_awaiting_import=feed_awaiting_import,
            awaiting_review=awaiting_review,
            evidence={
                "reason": "source_freshness_mismatch",
                "source_freshness": source_freshness,
                "build_next": asdict(dry_receipt),
                "generation": generation,
                "health": snapshot.health,
            },
        )
    if not prepared:
        receipt = dry_receipt
    else:
        receipt = _submit_prepared_dispatch(config, generation, prepared)
    status = receipt.status
    message = receipt.message
    if status == "QUEUED" and generation.get("state") != "BLOCKED":
        queued_decision_basis = (
            prepared.get("decision_basis")
            if isinstance(prepared, Mapping)
            and isinstance(prepared.get("decision_basis"), Mapping)
            else decision_basis or generation.get("last_refill_action_basis")
        )
        attempt = _attempt_from_receipt(
            receipt,
            attempt_ordinal=attempt_ordinal,
            retry_ordinal=retry_ordinal,
            reason=retry_reason,
            decision_basis=queued_decision_basis,
        )
        _record_attempt(generation, attempt)
        generation.pop("prepared_dispatch", None)
        generation["state"] = "QUEUED"
        if receipt.evidence.get("source"):
            generation["source_ppe_identity"] = receipt.evidence["source"]
        save_refill_generation(config, generation)
        feed_awaiting_import = max(feed_awaiting_import, 1)
    elif status == "RUNNING":
        active_running = max(active_running, 1)
    elif status in {"BLOCKED", "UNFILLED"}:
        generation["state"] = status
        save_refill_generation(config, generation)
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
            "generation": generation,
            "health": snapshot.health,
        },
        receipt=receipt,
    )


class RefillService:
    _STOP_REQUEST_POLL_SECONDS = 0.25

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
        self.service_generation_id = secrets.token_hex(16)
        self.last_reconcile: dict[str, Any] | None = None
        self._stop_requested = threading.Event()

    def _write_status(self, state: str, errors: tuple[str, ...] = ()) -> RefillServiceStatus:
        status = RefillServiceStatus(
            version=1,
            state=state,
            pid=os.getpid() if state != "stopped" else None,
            started_at=self.started_at,
            heartbeat_at=_utc_now(),
            last_reconcile=self.last_reconcile,
            errors=errors,
            service_generation_id=self.service_generation_id,
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
            service_generation_id=(
                str(raw["service_generation_id"])
                if raw.get("service_generation_id") is not None
                else None
            ),
        )

    def request_stop(self) -> Path:
        path = _refill_stop_path(self.config)
        status = self.read_status()
        lock_owner = _read_refill_service_lock_owner(self.config)
        lock_pid = lock_owner.get("pid")
        service_pid = status.pid
        owner_matches_status = (
            isinstance(lock_pid, int)
            and service_pid is not None
            and lock_pid == service_pid
            and _pid_alive(service_pid)
        )
        target_generation = (
            status.service_generation_id
            if (
                status.state == "running"
                and status.service_generation_id is not None
                and owner_matches_status
            )
            else None
        )
        existing = _read_refill_stop_request(path)
        if existing is not None:
            existing_generation = existing.get("service_generation_id")
            if existing_generation is None or (
                target_generation is not None
                and _refill_stop_applies_to_generation(existing, target_generation)
            ):
                self._stop_requested.set()
                return path
        payload: dict[str, Any] = {
            "version": 1,
            "requested_at": _utc_now(),
            "token": secrets.token_hex(8),
        }
        if target_generation is not None:
            payload.update(
                {
                    "service_generation_id": target_generation,
                    "service_pid": status.pid,
                    "service_started_at": status.started_at,
                }
            )
        _atomic_write_json(path, payload)
        self._stop_requested.set()
        return path

    def run_once(self) -> RefillReport:
        report = reconcile_refill(self.config)
        self.last_reconcile = report.decision_evidence
        self._write_status("running")
        return report

    def _wait_for_stop_request(self, stop_path: Path) -> bool:
        deadline = time.monotonic() + self.interval_seconds
        while True:
            if _refill_stop_applies_to_generation(
                _read_refill_stop_request(stop_path), self.service_generation_id
            ):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            wait_seconds = min(remaining, self._STOP_REQUEST_POLL_SECONDS)
            if self._stop_requested.wait(wait_seconds):
                self._stop_requested.clear()

    def run_forever(self) -> None:
        self.started_at = _utc_now()
        stop_path = _refill_stop_path(self.config)
        with HostProcessLock(_service_lock_path(self.config)):
            try:
                self._write_status("running")
                while True:
                    if _refill_stop_applies_to_generation(
                        _read_refill_stop_request(stop_path), self.service_generation_id
                    ):
                        break
                    errors: list[str] = []
                    try:
                        self.run_once()
                    except BaseException as exc:
                        errors.append(str(exc))
                        self._write_status("running", tuple(errors))
                    if _refill_stop_applies_to_generation(
                        _read_refill_stop_request(stop_path), self.service_generation_id
                    ):
                        break
                    if self._wait_for_stop_request(stop_path):
                        break
            finally:
                if _refill_stop_applies_to_generation(
                    _read_refill_stop_request(stop_path), self.service_generation_id
                ):
                    stop_path.unlink(missing_ok=True)
                self._write_status("stopped")


def render_refill_report_json(report: RefillReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True) + "\n"


def render_refill_service_status_json(status: RefillServiceStatus) -> str:
    return json.dumps(asdict(status), indent=2, sort_keys=True) + "\n"
