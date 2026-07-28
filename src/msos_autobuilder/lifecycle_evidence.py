"""Canonical attempt lifecycle evidence envelopes and producer heads."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


class LifecycleEvidenceError(RuntimeError):
    """Raised when lifecycle evidence cannot be written without ambiguity."""


ATTEMPT_IDENTITY_SCHEMA_VERSION = "attempt_identity.v1"
WORK_ITEM_DIGEST_CONTRACT = "work_item_source_sha256_v1"
ENVELOPE_SCHEMA_VERSION = "lifecycle_evidence_envelope.v1"
HEAD_SCHEMA_VERSION = "producer_head.v1"

REPOSITORY_IDENTITY = "danieltabakman/probability-prediction-engine"

TERMINAL_REASON_CODES_V1: dict[str, dict[str, Any]] = {
    "publication_review.drafted.v1": {
        "canonical_outcome": "publication_review_disposition=drafted",
        "item_disposition": "item_terminal_success_drafted",
        "item_terminal": True,
        "refill_action": "exclude_item_and_select_next",
    },
    "publication_review.no_publication.not_required.v1": {
        "canonical_outcome": "publication_review_disposition=terminal_no_publication",
        "item_disposition": "item_terminal_no_publication",
        "item_terminal": True,
        "refill_action": "exclude_item_and_select_next",
    },
    "publication_review.no_publication.out_of_scope.v1": {
        "canonical_outcome": "publication_review_disposition=terminal_no_publication",
        "item_disposition": "item_terminal_no_publication",
        "item_terminal": True,
        "refill_action": "exclude_item_and_select_next",
    },
    "publication_review.rejected.duplicate_or_obsolete.v1": {
        "canonical_outcome": "publication_review_disposition=rejected",
        "item_disposition": "item_terminal_rejected",
        "item_terminal": True,
        "refill_action": "exclude_item_and_select_next",
    },
    "publication_review.rejected.founder_declined.v1": {
        "canonical_outcome": "publication_review_disposition=rejected",
        "item_disposition": "item_terminal_rejected",
        "item_terminal": True,
        "refill_action": "exclude_item_and_select_next",
    },
    "revision.exhausted.contract_limit.v1": {
        "canonical_outcome": "validation_outcome=failed,revision_disposition=exhausted",
        "item_disposition": "item_terminal_revision_exhausted",
        "item_terminal": True,
        "refill_action": "exclude_item_and_select_next",
    },
    "revision.exhausted.no_valid_repair.v1": {
        "canonical_outcome": "validation_outcome=failed,revision_disposition=exhausted",
        "item_disposition": "item_terminal_revision_exhausted",
        "item_terminal": True,
        "refill_action": "exclude_item_and_select_next",
    },
}

PRODUCER_OWNERS: dict[str, str] = {
    "dispatch.prepared": "refill_controller",
    "dispatch.submitted": "build_next",
    "host.execution": "persistent_host",
    "relay.result": "results_relay",
    "gate.validation": "candidate_gate",
    "revision.disposition": "revision_loop",
    "publication_review.disposition": "controlled_publisher",
}

ENVELOPE_PATH_PARTS: dict[str, tuple[str, ...]] = {
    "dispatch.prepared": ("state", "refill-evidence", "dispatch", "prepared"),
    "dispatch.submitted": ("state", "refill-evidence", "dispatch", "submitted"),
    "host.execution": ("state", "host-evidence", "execution"),
    "relay.result": ("state", "relay-evidence", "result"),
    "gate.validation": ("state", "gate-evidence", "validation"),
    "revision.disposition": ("state", "revision-evidence", "disposition"),
    "publication_review.disposition": (
        "state",
        "publisher-evidence",
        "publication-review",
    ),
}

HEAD_PATH_PARTS: dict[str, tuple[str, ...]] = {
    "dispatch.prepared": ("state", "refill-evidence", "heads", "dispatch", "prepared"),
    "dispatch.submitted": ("state", "refill-evidence", "heads", "dispatch", "submitted"),
    "host.execution": ("state", "host-evidence", "heads", "execution"),
    "relay.result": ("state", "relay-evidence", "heads", "result"),
    "gate.validation": ("state", "gate-evidence", "heads", "validation"),
    "revision.disposition": ("state", "revision-evidence", "heads", "disposition"),
    "publication_review.disposition": (
        "state",
        "publisher-evidence",
        "heads",
        "publication-review",
    ),
}

_HEAD_COMPARE_FIELDS = (
    "producer_sequence",
    "evidence_id",
    "envelope_path",
    "envelope_sha256",
    "final",
    "closed_status",
)


@dataclass(frozen=True)
class EvidenceWriteResult:
    envelope_path: Path
    head_path: Path
    evidence_id: str
    identity_digest: str
    envelope_sha256: str


def canonical_json_bytes(value: Mapping[str, Any] | Sequence[Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def work_item_source_sha256_v1(source: bytes) -> str:
    return sha256_bytes(source.replace(b"\r\n", b"\n").replace(b"\r", b"\n"))


def work_item_digest_from_mapping(work: Mapping[str, Any]) -> str:
    return work_item_source_sha256_v1(canonical_json_bytes(dict(work)))


def attempt_identity(
    *,
    pipeline_id: str,
    work_item_id: str,
    work_item_digest: str,
    generation_id: str,
    job_id: str,
    attempt_ordinal: int,
    retry_ordinal: int,
    repository_identity: str = REPOSITORY_IDENTITY,
) -> dict[str, Any]:
    if not work_item_digest or len(work_item_digest) != 64:
        raise LifecycleEvidenceError("work item digest must be a SHA-256 hex string")
    if attempt_ordinal < 1 or retry_ordinal < 0:
        raise LifecycleEvidenceError("attempt and retry ordinals are invalid")
    return {
        "schema_version": ATTEMPT_IDENTITY_SCHEMA_VERSION,
        "repository_identity": repository_identity.strip().lower().removesuffix(".git"),
        "pipeline_id": pipeline_id,
        "work_item_id": work_item_id,
        "work_item_digest_contract": WORK_ITEM_DIGEST_CONTRACT,
        "work_item_digest": work_item_digest,
        "generation_id": generation_id,
        "job_id": job_id,
        "attempt_ordinal": attempt_ordinal,
        "retry_ordinal": retry_ordinal,
    }


def identity_digest(identity: Mapping[str, Any]) -> str:
    validate_attempt_identity(identity)
    return sha256_bytes(canonical_json_bytes(dict(identity)))


def latest_evidence_set_sha256_v1(heads: Sequence[Mapping[str, Any]]) -> str:
    stable = sorted(
        (dict(head) for head in heads),
        key=lambda head: (str(head.get("evidence_kind")), str(head.get("identity_digest"))),
    )
    return sha256_bytes(canonical_json_bytes(stable))


def validate_attempt_identity(identity: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "repository_identity",
        "pipeline_id",
        "work_item_id",
        "work_item_digest_contract",
        "work_item_digest",
        "generation_id",
        "job_id",
        "attempt_ordinal",
        "retry_ordinal",
    }
    if set(identity) != required:
        raise LifecycleEvidenceError("attempt identity has unexpected fields")
    if identity.get("schema_version") != ATTEMPT_IDENTITY_SCHEMA_VERSION:
        raise LifecycleEvidenceError("unsupported attempt identity schema")
    if identity.get("work_item_digest_contract") != WORK_ITEM_DIGEST_CONTRACT:
        raise LifecycleEvidenceError("unsupported work item digest contract")
    string_fields = required - {"attempt_ordinal", "retry_ordinal"}
    if not all(isinstance(identity.get(key), str) and identity.get(key) for key in string_fields):
        raise LifecycleEvidenceError("attempt identity string fields must be non-empty")
    if not isinstance(identity.get("attempt_ordinal"), int) or isinstance(
        identity.get("attempt_ordinal"), bool
    ):
        raise LifecycleEvidenceError("attempt ordinal must be an integer")
    if not isinstance(identity.get("retry_ordinal"), int) or isinstance(
        identity.get("retry_ordinal"), bool
    ):
        raise LifecycleEvidenceError("retry ordinal must be an integer")


def attempt_identity_from_job(job: Mapping[str, Any]) -> dict[str, Any] | None:
    founder = job.get("founder_build_next")
    if not isinstance(founder, Mapping):
        return None
    refill = founder.get("refill_attempt")
    if not isinstance(refill, Mapping):
        return None
    digest = founder.get(WORK_ITEM_DIGEST_CONTRACT)
    if not isinstance(digest, str) or not digest:
        return None
    return attempt_identity(
        pipeline_id=str(founder.get("pipeline_id") or ""),
        work_item_id=str(founder.get("work_item_id") or ""),
        work_item_digest=digest,
        generation_id=str(refill.get("generation_id") or ""),
        job_id=str(job.get("job_id") or ""),
        attempt_ordinal=int(refill.get("attempt_ordinal")),
        retry_ordinal=int(refill.get("retry_ordinal") or 0),
    )


def attempt_identity_from_job_yaml(path: Path) -> dict[str, Any] | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return None
    if not isinstance(raw, Mapping):
        return None
    return attempt_identity_from_job(raw)


def producer_envelope_path(
    root: Path,
    *,
    evidence_kind: str,
    identity: Mapping[str, Any],
    evidence_id: str,
) -> Path:
    digest = identity_digest(identity)
    return (
        root.joinpath(*ENVELOPE_PATH_PARTS[evidence_kind])
        / str(identity["generation_id"])
        / str(identity["job_id"])
        / f"{digest}.{evidence_id}.json"
    )


def producer_head_path(root: Path, *, evidence_kind: str, identity: Mapping[str, Any]) -> Path:
    return root.joinpath(*HEAD_PATH_PARTS[evidence_kind]) / f"{identity_digest(identity)}.json"


class EvidenceHeadsLock(AbstractContextManager["EvidenceHeadsLock"]):
    def __init__(self, host_root: Path) -> None:
        self.path = host_root / "state" / "evidence-heads.lock"
        self._handle: Any = None

    def __enter__(self) -> EvidenceHeadsLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0)
        self._handle.write(b"0")
        self._handle.flush()
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            raise LifecycleEvidenceError("could not acquire state/evidence-heads.lock") from exc
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def emit_lifecycle_evidence(
    host_root: Path,
    *,
    evidence_kind: str,
    identity: Mapping[str, Any],
    source_path: Path,
    payload: Mapping[str, Any],
    producer_sequence: int = 1,
    final: bool,
    closed_status: str,
    observed_at: str | None = None,
) -> EvidenceWriteResult:
    if evidence_kind not in PRODUCER_OWNERS:
        raise LifecycleEvidenceError(f"unknown evidence kind: {evidence_kind}")
    validate_attempt_identity(identity)
    if producer_sequence < 1:
        raise LifecycleEvidenceError("producer_sequence must be positive")
    if closed_status not in {"open", "final", "not_applicable"}:
        raise LifecycleEvidenceError("closed_status must be open, final, or not_applicable")
    if closed_status in {"final", "not_applicable"} and final is not True:
        raise LifecycleEvidenceError("closed producer streams must set final=true")
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise LifecycleEvidenceError(f"source evidence does not exist: {source_path}")
    source_sha = sha256_file(source_path)
    digest = identity_digest(identity)
    evidence_seed = {
        "evidence_kind": evidence_kind,
        "identity_digest": digest,
        "producer_sequence": producer_sequence,
        "source_sha256": source_sha,
        "payload": dict(payload),
        "final": final,
        "closed_status": closed_status,
    }
    evidence_id = sha256_bytes(canonical_json_bytes(evidence_seed))[:32]
    envelope = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "evidence_kind": evidence_kind,
        "evidence_id": evidence_id,
        "producer_sequence": producer_sequence,
        "attempt_identity": dict(identity),
        "identity_digest": digest,
        "source_path": str(source_path),
        "source_sha256": source_sha,
        "producer": {
            "name": PRODUCER_OWNERS[evidence_kind],
            "release": "observational-v1",
        },
        "observed_at": observed_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "final": final,
        "closed_status": closed_status,
        "payload": dict(payload),
    }
    envelope_bytes = canonical_json_bytes(envelope) + b"\n"
    envelope_sha = sha256_bytes(envelope_bytes)
    envelope_path = producer_envelope_path(
        host_root,
        evidence_kind=evidence_kind,
        identity=identity,
        evidence_id=evidence_id,
    )
    head_path = producer_head_path(host_root, evidence_kind=evidence_kind, identity=identity)
    head = {
        "head_schema_version": HEAD_SCHEMA_VERSION,
        "attempt_identity": dict(identity),
        "identity_digest": digest,
        "evidence_kind": evidence_kind,
        "producer": PRODUCER_OWNERS[evidence_kind],
        "producer_sequence": producer_sequence,
        "evidence_id": evidence_id,
        "envelope_path": str(envelope_path.resolve()),
        "envelope_sha256": envelope_sha,
        "final": final,
        "closed_status": closed_status,
    }
    with EvidenceHeadsLock(host_root):
        _write_immutable(envelope_path, envelope_bytes)
        _replace_head(head_path, head)
    return EvidenceWriteResult(envelope_path, head_path, evidence_id, digest, envelope_sha)


def _write_immutable(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise LifecycleEvidenceError(f"immutable evidence envelope conflict: {path}")
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _replace_head(path: Path, head: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, Mapping):
            raise LifecycleEvidenceError("producer head is malformed")
        _validate_head_update(current, head)
    data = canonical_json_bytes(dict(head)) + b"\n"
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp_path = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _validate_head_update(current: Mapping[str, Any], new: Mapping[str, Any]) -> None:
    for field in (
        "head_schema_version",
        "attempt_identity",
        "identity_digest",
        "evidence_kind",
        "producer",
    ):
        if current.get(field) != new.get(field):
            raise LifecycleEvidenceError("producer head ownership conflict")
    current_sequence = current.get("producer_sequence")
    new_sequence = new.get("producer_sequence")
    if not isinstance(current_sequence, int) or not isinstance(new_sequence, int):
        raise LifecycleEvidenceError("producer head sequence is malformed")
    if current.get("closed_status") in {"final", "not_applicable"}:
        if all(current.get(field) == new.get(field) for field in _HEAD_COMPARE_FIELDS):
            return
        raise LifecycleEvidenceError("evidence_conflict: closed producer stream mutated")
    if new_sequence < current_sequence:
        raise LifecycleEvidenceError("evidence_conflict: producer sequence regression")
    if new_sequence == current_sequence:
        if all(current.get(field) == new.get(field) for field in _HEAD_COMPARE_FIELDS):
            return
        raise LifecycleEvidenceError("evidence_conflict: conflicting same-sequence evidence")
    if new_sequence > current_sequence + 1:
        raise LifecycleEvidenceError("evidence_conflict: producer sequence gap")
