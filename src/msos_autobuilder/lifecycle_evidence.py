"""Canonical attempt lifecycle evidence envelopes and producer heads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
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

_SHA256_HEX = set("0123456789abcdef")
_CLOSED_STATUSES = {"open", "final", "not_applicable"}
_PAYLOAD_CODES: dict[str, dict[str, set[str]]] = {
    "dispatch.prepared": {
        "required": {
            "selected_work_item",
            "generation_id",
            "dispatch_intent_sha256",
            "capacity_slot",
        },
        "optional": set(),
    },
    "dispatch.submitted": {
        "required": {"feed_commit", "feed_path", "submitted_job_sha256"},
        "optional": set(),
    },
    "host.execution": {
        "required": {"execution_outcome"},
        "optional": {"host_archive_path", "error_class"},
    },
    "relay.result": {
        "required": {
            "relay_disposition",
            "relayed_commit",
            "canonical_report_sha256",
            "source_report_sha256",
            "complete_patch_reconstruction",
        },
        "optional": set(),
    },
    "gate.validation": {
        "required": {"validation_outcome", "gate_report_sha256", "results_commit"},
        "optional": {"validation_state", "validation_contract_sha256"},
    },
    "revision.disposition": {
        "required": {"revision_disposition"},
        "optional": {"descendant_job_id", "gate_report_sha256", "jobs_commit", "reason_code"},
    },
    "publication_review.disposition": {
        "required": {"publication_review_disposition"},
        "optional": {
            "reason_code",
            "draft_pr",
            "product_branch",
            "product_commit",
            "results_commit",
        },
    },
}

_GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)

_PAYLOAD_ALLOWED_VALUES: dict[str, dict[str, set[str]]] = {
    "host.execution": {
        "execution_outcome": {
            "imported",
            "pending",
            "running",
            "completed",
            "failed",
            "interrupted",
        }
    },
    "relay.result": {"relay_disposition": {"relayed", "not_applicable"}},
    "gate.validation": {
        "validation_outcome": {"passed", "failed", "blocked", "missing", "conflict"}
    },
    "revision.disposition": {
        "revision_disposition": {
            "queued",
            "exhausted",
            "blocked",
            "not_applicable",
            "missing",
            "conflict",
        }
    },
    "publication_review.disposition": {
        "publication_review_disposition": {
            "awaiting_review",
            "drafted",
            "rejected",
            "terminal_no_publication",
            "blocked",
            "not_applicable",
            "missing",
            "conflict",
        }
    },
}


@dataclass(frozen=True)
class EvidenceWriteResult:
    envelope_path: Path
    head_path: Path
    evidence_id: str
    identity_digest: str
    envelope_sha256: str


@dataclass(frozen=True)
class SourceRef:
    repository: str
    ref: str
    commit: str
    path: str
    sha256: str

    def as_payload(self) -> dict[str, str]:
        return {
            "repository": self.repository,
            "ref": self.ref,
            "commit": self.commit,
            "path": self.path,
            "sha256": self.sha256,
        }


def record_producer_evidence_error(
    host_root: Path,
    *,
    producer: str,
    evidence_kind: str,
    error: BaseException,
    identity: Mapping[str, Any] | None = None,
    primary_outcome: Mapping[str, Any] | None = None,
    primary_outcome_preserved: bool = True,
) -> Path | None:
    try:
        state = host_root / "state" / "producer-evidence-errors" / producer
        state.mkdir(parents=True, exist_ok=True)
        digest = "unknown"
        if identity is not None:
            try:
                digest = identity_digest(identity)
            except Exception:
                try:
                    digest = sha256_bytes(canonical_json_bytes(dict(identity)))[:32]
                except Exception:
                    digest = "unknown"
        payload = {
            "schema_version": "producer_evidence_error.v1",
            "producer": producer,
            "evidence_kind": evidence_kind,
            "identity_digest": digest,
            "error_type": type(error).__name__,
            "message": str(error),
            "primary_outcome_preserved": primary_outcome_preserved,
            "primary_outcome": dict(primary_outcome or {}),
        }
        suffix = sha256_bytes(canonical_json_bytes(payload))[:16]
        path = state / f"{evidence_kind.replace('.', '-')}.{digest}.{suffix}.json"
        _atomic_json(path, payload)
        return path
    except Exception:
        return None


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
    raise LifecycleEvidenceError(
        "work_item_source_sha256_v1 requires exact UTF-8 source bytes, not a parsed mapping"
    )


def work_item_source_bytes_from_snapshot_json(snapshot_text: str, work_item_id: str) -> bytes:
    matches: list[str] = []
    for start, end in _json_object_spans(snapshot_text):
        candidate = snapshot_text[start:end]
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(value, Mapping)
            and value.get("work_item_id") == work_item_id
            and value.get("state") == "READY_TO_BUILD"
            and "trace" in value
            and "evidence" in value
        ):
            matches.append(candidate)
    if len(matches) != 1:
        raise LifecycleEvidenceError("exact work-item source byte boundary is ambiguous")
    return matches[0].encode("utf-8")


def _json_object_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    in_string = False
    escape = False
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append(index)
        elif char == "}":
            if not stack:
                raise LifecycleEvidenceError("PPE portfolio output has unbalanced JSON objects")
            spans.append((stack.pop(), index + 1))
    if stack or in_string:
        raise LifecycleEvidenceError("PPE portfolio output has unbalanced JSON objects")
    return spans


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
    seen: set[tuple[str, str]] = set()
    for head in heads:
        validate_producer_head(head)
        key = (str(head.get("identity_digest")), str(head.get("evidence_kind")))
        if key in seen:
            raise LifecycleEvidenceError("duplicate producer head for identity/evidence kind")
        seen.add(key)
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
    if identity["attempt_ordinal"] < 1 or identity["retry_ordinal"] < 0:
        raise LifecycleEvidenceError("attempt and retry ordinals are invalid")
    if not _is_sha256(identity["work_item_digest"]):
        raise LifecycleEvidenceError("work item digest must be a SHA-256 hex string")


def attempt_identity_from_job(job: Mapping[str, Any]) -> dict[str, Any] | None:
    founder = job.get("founder_build_next")
    if not isinstance(founder, Mapping):
        return None
    if "refill_attempt" not in founder:
        return None
    refill = founder.get("refill_attempt")
    if not isinstance(refill, Mapping):
        raise LifecycleEvidenceError("refill attempt metadata is malformed")
    digest = founder.get(WORK_ITEM_DIGEST_CONTRACT)
    if not isinstance(digest, str) or not digest:
        raise LifecycleEvidenceError("refill attempt identity is missing work item digest")
    try:
        return attempt_identity(
            pipeline_id=str(founder.get("pipeline_id") or ""),
            work_item_id=str(founder.get("work_item_id") or ""),
            work_item_digest=digest,
            generation_id=str(refill.get("generation_id") or ""),
            job_id=str(job.get("job_id") or ""),
            attempt_ordinal=int(refill.get("attempt_ordinal")),
            retry_ordinal=int(refill.get("retry_ordinal") or 0),
        )
    except (TypeError, ValueError, LifecycleEvidenceError):
        raise LifecycleEvidenceError("refill attempt identity is malformed") from None


def attempt_identity_from_job_yaml(path: Path) -> dict[str, Any] | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise LifecycleEvidenceError("job YAML is unreadable") from exc
    except (TypeError, ValueError, yaml.YAMLError) as exc:
        raise LifecycleEvidenceError("job YAML is invalid") from exc
    if not isinstance(raw, Mapping):
        raise LifecycleEvidenceError("job YAML must be a mapping")
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


class AttemptLifecycleLock(AbstractContextManager["AttemptLifecycleLock"]):
    def __init__(self, host_root: Path) -> None:
        self.path = host_root / "state" / "attempt-lifecycle.lock"
        self._handle: Any = None

    def __enter__(self) -> AttemptLifecycleLock:
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
            raise LifecycleEvidenceError("could not acquire state/attempt-lifecycle.lock") from exc
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
    payload: Mapping[str, Any],
    source_path: Path | None = None,
    source_ref: SourceRef | Mapping[str, Any] | None = None,
    producer_sequence: int = 1,
    final: bool,
    closed_status: str,
    observed_at: str,
) -> EvidenceWriteResult:
    if evidence_kind not in PRODUCER_OWNERS:
        raise LifecycleEvidenceError(f"unknown evidence kind: {evidence_kind}")
    validate_attempt_identity(identity)
    if not _is_canonical_timestamp(observed_at):
        raise LifecycleEvidenceError("observed_at must be a stable source-recorded value")
    if not isinstance(producer_sequence, int) or isinstance(producer_sequence, bool):
        raise LifecycleEvidenceError("producer_sequence must be an integer")
    if producer_sequence < 1:
        raise LifecycleEvidenceError("producer_sequence must be positive")
    if closed_status not in {"open", "final", "not_applicable"}:
        raise LifecycleEvidenceError("closed_status must be open, final, or not_applicable")
    if closed_status in {"final", "not_applicable"} and final is not True:
        raise LifecycleEvidenceError("closed producer streams must set final=true")
    try:
        source_payload, source_sha = _source_payload(
            host_root,
            source_path=source_path,
            source_ref=source_ref,
        )
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
            "source": source_payload,
            "source_sha256": source_sha,
            "producer": {
                "name": PRODUCER_OWNERS[evidence_kind],
                "release": "observational-v1",
            },
            "observed_at": observed_at,
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
            "envelope_path": _host_relative_path(host_root, envelope_path),
            "envelope_sha256": envelope_sha,
            "final": final,
            "closed_status": closed_status,
        }
        validate_lifecycle_evidence_envelope(envelope)
        validate_producer_head(head, envelope=envelope)
        with EvidenceHeadsLock(host_root):
            _write_immutable(envelope_path, envelope_bytes)
            _replace_head(head_path, head)
        validate_producer_head(head, envelope=envelope, host_root=host_root)
        return EvidenceWriteResult(envelope_path, head_path, evidence_id, digest, envelope_sha)
    except LifecycleEvidenceError:
        raise
    except Exception as exc:
        raise LifecycleEvidenceError("lifecycle evidence operation failed") from exc


def reduce_attempt_lifecycle(host_root: Path) -> dict[str, Any]:
    """Reduce producer heads into canonical attempt/work-item snapshots."""
    with EvidenceHeadsLock(host_root):
        with AttemptLifecycleLock(host_root):
            _recover_lifecycle_materialized_state(host_root)
            groups = _load_current_producer_heads_contained(host_root)
            reduced: list[dict[str, Any]] = []
            blocked: list[dict[str, Any]] = []
            for digest, heads in sorted(groups.items()):
                try:
                    snapshot = _reduce_identity_heads(host_root, heads)
                    _publish_lifecycle_snapshot(host_root, digest, snapshot)
                    reduced.append(
                        {
                            "identity_digest": digest,
                            "lifecycle_phase": snapshot["lifecycle_phase"],
                            "item_disposition": snapshot["item_disposition"],
                            "refill_action": snapshot["refill_action"],
                        }
                    )
                except Exception as exc:
                    identity = None
                    if heads:
                        candidate = heads[0].get("attempt_identity")
                        identity = candidate if isinstance(candidate, Mapping) else None
                    record_producer_evidence_error(
                        host_root,
                        producer="attempt_lifecycle_recorder",
                        evidence_kind="canonical.reduce",
                        error=exc,
                        identity=identity,
                        primary_outcome={
                            "identity_digest": digest,
                            "head_count": len(heads),
                            "head_paths": [
                                str(head.get("envelope_path") or "") for head in heads
                            ],
                        },
                        primary_outcome_preserved=False,
                    )
                    blocked.append(
                        {
                            "identity_digest": digest,
                            "error_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
            report = {
                "schema_version": "attempt_lifecycle_reduce_report.v1",
                "reduced_at": datetime.now().astimezone().isoformat(),
                "identity_count": len(reduced),
                "reduced": reduced,
                "blocked": blocked,
            }
            _atomic_json(host_root / "state" / "attempt-lifecycle" / "last-reduce.json", report)
            return report


def canonical_refill_classification(
    host_root: Path,
    *,
    job_id: str,
    generation_id: str | None = None,
) -> dict[str, Any] | None:
    """Return a freshness-verified canonical refill classification for a job if one exists."""
    with EvidenceHeadsLock(host_root):
        with AttemptLifecycleLock(host_root):
            _recover_lifecycle_materialized_state(host_root)
        snapshot_path = _find_attempt_snapshot(
            host_root,
            job_id=job_id,
            generation_id=generation_id,
        )
        if snapshot_path is None:
            kinds = _head_kinds_for_job(host_root, job_id=job_id, generation_id=generation_id)
            if not kinds:
                return None
            if not _generation_requires_canonical(host_root, generation_id):
                return None
            return {
                "category": "unknown",
                "stage": "canonical_lifecycle_missing",
                "evidence": {
                    "reason": "canonical_snapshot_missing",
                    "job_id": job_id,
                    "refill_action": "block_fail_closed",
                },
            }
        snapshot = _read_json_mapping(snapshot_path)
        _verify_snapshot_freshness(host_root, snapshot)
        refill_action = str(snapshot.get("refill_action") or "block_fail_closed")
        item_terminal = snapshot.get("item_terminal") is True
        if refill_action == "exclude_item_and_select_next" and item_terminal:
            category = "item_terminal"
        elif str(snapshot.get("attempt_terminality")) == "in_flight":
            category = "in_flight"
        else:
            category = "unknown"
        return {
            "category": category,
            "stage": "canonical_lifecycle",
            "evidence": {
                "reason": str(snapshot.get("item_disposition") or "canonical_disposition"),
                "refill_action": refill_action,
                "snapshot_path": _host_relative_path(host_root, snapshot_path),
                "snapshot_sha256": _snapshot_sha256(snapshot),
                "latest_evidence_set_sha256": snapshot.get("latest_evidence_set_sha256"),
                "reduced_through": snapshot.get("reduced_through"),
                "lifecycle_phase": snapshot.get("lifecycle_phase"),
                "attempt_terminal": snapshot.get("attempt_terminal"),
                "item_terminal": snapshot.get("item_terminal"),
                "attempt_identity": dict(
                    _mapping(snapshot.get("attempt_identity"), "attempt_identity")
                ),
            },
        }


def _load_current_producer_heads(host_root: Path) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for parts in HEAD_PATH_PARTS.values():
        root = host_root.joinpath(*parts)
        if not root.exists():
            continue
        for path in sorted(root.glob("*.json")):
            head = _read_json_mapping(path)
            validate_producer_head(head, host_root=host_root)
            digest = str(head["identity_digest"])
            groups.setdefault(digest, []).append(dict(head))
    return groups


def _load_current_producer_heads_contained(host_root: Path) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for parts in HEAD_PATH_PARTS.values():
        root = host_root.joinpath(*parts)
        if not root.exists():
            continue
        for path in sorted(root.glob("*.json")):
            try:
                head = _read_json_mapping(path)
                validate_producer_head(head, host_root=host_root)
                digest = str(head["identity_digest"])
                groups.setdefault(digest, []).append(dict(head))
            except Exception as exc:
                identity = None
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, Mapping) and isinstance(
                        raw.get("attempt_identity"), Mapping
                    ):
                        identity = raw["attempt_identity"]
                except Exception:
                    identity = None
                record_producer_evidence_error(
                    host_root,
                    producer="attempt_lifecycle_recorder",
                    evidence_kind="canonical.head",
                    error=exc,
                    identity=identity,
                    primary_outcome={"head_path": _host_relative_path(host_root, path)},
                    primary_outcome_preserved=False,
                )
    return groups


def _reduce_identity_heads(host_root: Path, heads: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not heads:
        raise LifecycleEvidenceError("cannot reduce empty producer head set")
    identity = dict(_mapping(heads[0].get("attempt_identity"), "attempt_identity"))
    digest = identity_digest(identity)
    if any(head.get("identity_digest") != digest for head in heads):
        raise LifecycleEvidenceError("producer head identity group mismatch")
    envelopes = {
        str(head["evidence_kind"]): _load_envelope_for_head(host_root, head) for head in heads
    }
    latest_heads = sorted(
        (dict(head) for head in heads),
        key=lambda head: str(head["evidence_kind"]),
    )
    latest_digest = latest_evidence_set_sha256_v1(latest_heads)
    base: dict[str, Any] = {
        "schema_version": "attempt_lifecycle_snapshot.v1",
        "attempt_identity": identity,
        "identity_digest": digest,
        "lifecycle_phase": "dispatch_prepared",
        "execution_outcome": "not_started",
        "validation_outcome": "not_recorded",
        "revision_disposition": "not_recorded",
        "publication_review_disposition": "not_recorded",
        "attempt_terminal": False,
        "attempt_terminality": "in_flight",
        "item_terminal": False,
        "item_disposition": "in_flight",
        "retry_eligibility": "not_applicable",
        "evidence_integrity": "complete",
        "refill_action": "block_fail_closed",
        "latest_evidence_set": latest_heads,
        "latest_evidence_set_sha256": latest_digest,
        "reduced_through": {
            "schema_version": "attempt_lifecycle_reduced_through.v1",
            "identity_digest": digest,
            "latest_evidence_set_sha256": latest_digest,
        },
    }

    def payload(kind: str) -> Mapping[str, Any] | None:
        envelope = envelopes.get(kind)
        if envelope is None:
            return None
        return _mapping(envelope.get("payload"), "payload")

    prepared = payload("dispatch.prepared")
    if prepared is None:
        return _blocked(base, "evidence_missing", "operator_required_evidence_missing")

    submitted = payload("dispatch.submitted")
    if submitted is None:
        return base
    base["lifecycle_phase"] = "dispatch_submitted"

    host = payload("host.execution")
    if host is None:
        base["lifecycle_phase"] = "host_awaiting_import"
        return base
    execution = str(host.get("execution_outcome") or "")
    if execution in {"imported", "pending", "running"}:
        base["lifecycle_phase"] = "host_" + ("pending" if execution == "imported" else execution)
        base["execution_outcome"] = "running" if execution == "running" else "not_started"
        return base
    if execution in {"failed", "interrupted"}:
        base["lifecycle_phase"] = "execution_archived"
        base["execution_outcome"] = execution
        base["attempt_terminal"] = True
        base["attempt_terminality"] = "terminal"
        base["retry_eligibility"] = "operator_required"
        return _blocked(base, "complete", "operator_required_execution_failed")
    if execution != "completed":
        return _blocked(base, "evidence_conflict", "operator_required_evidence_conflict")
    base["lifecycle_phase"] = "execution_archived"
    base["execution_outcome"] = "completed"

    relay = payload("relay.result")
    if relay is None or relay.get("relay_disposition") != "relayed":
        return _blocked(base, "evidence_missing", "operator_required_evidence_missing")
    base["lifecycle_phase"] = "result_relayed"

    gate = payload("gate.validation")
    if gate is None:
        return _blocked(base, "evidence_missing", "operator_required_evidence_missing")
    validation = str(gate.get("validation_outcome") or "")
    base["lifecycle_phase"] = "validation_recorded"
    base["validation_outcome"] = validation
    if validation in {"blocked", "missing", "conflict"}:
        return _blocked(base, f"evidence_{validation}", "operator_required_validation_blocked")

    revision = payload("revision.disposition")
    publication = payload("publication_review.disposition")
    if validation == "failed":
        if revision is None:
            return _blocked(base, "evidence_missing", "operator_required_revision_blocked")
        disposition = str(revision.get("revision_disposition") or "")
        base["lifecycle_phase"] = "revision_recorded"
        base["revision_disposition"] = disposition
        if disposition == "queued":
            return base
        if disposition == "exhausted":
            reason = str(revision.get("reason_code") or "")
            return _terminal_item(base, "item_terminal_revision_exhausted", reason)
        return _blocked(
            base,
            "evidence_" + (disposition or "missing"),
            "operator_required_revision_blocked",
        )

    if validation != "passed":
        return _blocked(base, "evidence_conflict", "operator_required_validation_blocked")
    if revision is None or revision.get("revision_disposition") != "not_applicable":
        return _blocked(base, "evidence_missing", "operator_required_revision_blocked")
    base["revision_disposition"] = "not_applicable"
    if publication is None:
        return _blocked(base, "evidence_missing", "operator_required_publication_blocked")
    disposition = str(publication.get("publication_review_disposition") or "")
    base["lifecycle_phase"] = "publication_review_recorded"
    base["publication_review_disposition"] = disposition
    if disposition == "awaiting_review":
        return base
    if disposition == "drafted":
        return _terminal_item(
            base,
            "item_terminal_success_drafted",
            str(publication.get("reason_code") or ""),
        )
    if disposition == "rejected":
        return _terminal_item(
            base,
            "item_terminal_rejected",
            str(publication.get("reason_code") or ""),
        )
    if disposition == "terminal_no_publication":
        return _terminal_item(
            base,
            "item_terminal_no_publication",
            str(publication.get("reason_code") or ""),
        )
    return _blocked(
        base,
        "evidence_" + (disposition or "missing"),
        "operator_required_publication_blocked",
    )


def _blocked(snapshot: dict[str, Any], integrity: str, disposition: str) -> dict[str, Any]:
    snapshot["lifecycle_phase"] = "blocked"
    snapshot["evidence_integrity"] = integrity
    snapshot["attempt_terminality"] = (
        "terminal" if snapshot.get("attempt_terminal") is True else "blocked"
    )
    snapshot["item_terminal"] = False
    snapshot["item_disposition"] = disposition
    snapshot["refill_action"] = "block_fail_closed"
    return snapshot


def _terminal_item(snapshot: dict[str, Any], disposition: str, reason_code: str) -> dict[str, Any]:
    if reason_code not in TERMINAL_REASON_CODES_V1:
        return _blocked(snapshot, "evidence_conflict", "operator_required_unknown_terminal_reason")
    expected = TERMINAL_REASON_CODES_V1[reason_code]
    if expected["item_disposition"] != disposition:
        return _blocked(snapshot, "evidence_conflict", "operator_required_unknown_terminal_reason")
    snapshot["attempt_terminal"] = True
    snapshot["attempt_terminality"] = "terminal"
    snapshot["item_terminal"] = True
    snapshot["item_disposition"] = disposition
    snapshot["retry_eligibility"] = "not_applicable"
    snapshot["refill_action"] = str(expected["refill_action"])
    return snapshot


def _load_envelope_for_head(host_root: Path, head: Mapping[str, Any]) -> dict[str, Any]:
    path = host_root.joinpath(*Path(str(head["envelope_path"])).parts)
    envelope = _read_json_mapping(path)
    validate_lifecycle_evidence_envelope(envelope)
    validate_producer_head(head, envelope=envelope, host_root=host_root)
    return dict(envelope)


def _publish_lifecycle_snapshot(host_root: Path, digest: str, snapshot: Mapping[str, Any]) -> None:
    root = host_root / "state" / "attempt-lifecycle"
    previous_snapshot = _read_existing_snapshot(host_root, digest)
    previous_snapshot_sha = (
        _snapshot_sha256(previous_snapshot) if previous_snapshot is not None else None
    )
    snapshot_payload = dict(snapshot)
    snapshot_sha = _snapshot_sha256(snapshot_payload)
    snapshot_payload["snapshot_sha256"] = snapshot_sha
    sequence, previous_transition_sha = _next_transition_identity(root, digest)
    transition = {
        "schema_version": "attempt_lifecycle_transition.v1",
        "sequence": sequence,
        "identity_digest": digest,
        "previous_transition_sha256": previous_transition_sha,
        "from_snapshot_sha256": previous_snapshot_sha,
        "to_snapshot_sha256": snapshot_sha,
        "snapshot": snapshot_payload,
        "latest_evidence_set_sha256": snapshot_payload["latest_evidence_set_sha256"],
        "lifecycle_phase": snapshot_payload["lifecycle_phase"],
        "item_disposition": snapshot_payload["item_disposition"],
        "refill_action": snapshot_payload["refill_action"],
        "source_evidence": list(snapshot_payload["latest_evidence_set"]),
        "recorded_at": datetime.now().astimezone().isoformat(),
        "reducer_version": "attempt_lifecycle_reducer.v1",
    }
    transition_sha = sha256_bytes(canonical_json_bytes(transition))
    transition_with_hash = {**transition, "transition_sha256": transition_sha}
    journal_path = root / "transitions" / digest / f"{sequence:020d}.json"
    _write_immutable(journal_path, canonical_json_bytes(transition_with_hash) + b"\n")
    _atomic_json(root / "attempts" / f"{digest}.json", snapshot_payload)
    _atomic_json(root / "work-items" / f"{digest}.json", snapshot_payload)
    generation_id = _safe_path_segment(str(snapshot_payload["attempt_identity"]["generation_id"]))
    _atomic_json(root / "generations" / generation_id / f"{digest}.json", snapshot_payload)
    watermark = {
        **dict(_mapping(snapshot_payload["reduced_through"], "reduced_through")),
        "attempt_snapshot_sha256": snapshot_sha,
        "journal_transition_sha256": transition_sha,
        "journal_sequence": sequence,
    }
    _atomic_json(root / "reduced-through" / f"{digest}.json", watermark)


def _snapshot_sha256(snapshot: Mapping[str, Any]) -> str:
    payload = dict(snapshot)
    payload.pop("snapshot_sha256", None)
    return sha256_bytes(canonical_json_bytes(payload))


def _read_existing_snapshot(host_root: Path, digest: str) -> dict[str, Any] | None:
    path = host_root / "state" / "attempt-lifecycle" / "attempts" / f"{digest}.json"
    if not path.exists():
        return None
    return _read_json_mapping(path)


def _next_transition_identity(root: Path, digest: str) -> tuple[int, str | None]:
    transition_root = root / "transitions" / digest
    if not transition_root.exists():
        return 1, None
    existing = sorted(transition_root.glob("*.json"))
    if not existing:
        return 1, None
    previous_sha: str | None = None
    last: dict[str, Any] | None = None
    for path in existing:
        last = _read_json_mapping(path)
        _validate_transition_record(last, previous_sha=previous_sha)
        previous_sha = str(last["transition_sha256"])
    assert last is not None
    sequence = last.get("sequence")
    if not isinstance(sequence, int) or sequence < 1:
        raise LifecycleEvidenceError("lifecycle transition sequence is malformed")
    return sequence + 1, str(last["transition_sha256"])


def _validate_transition_record(
    transition: Mapping[str, Any],
    *,
    previous_sha: str | None,
) -> None:
    required = {
        "schema_version",
        "sequence",
        "identity_digest",
        "previous_transition_sha256",
        "from_snapshot_sha256",
        "to_snapshot_sha256",
        "snapshot",
        "latest_evidence_set_sha256",
        "lifecycle_phase",
        "item_disposition",
        "refill_action",
        "source_evidence",
        "recorded_at",
        "reducer_version",
        "transition_sha256",
    }
    if set(transition) != required:
        raise LifecycleEvidenceError("lifecycle transition has unexpected fields")
    if transition.get("schema_version") != "attempt_lifecycle_transition.v1":
        raise LifecycleEvidenceError("unsupported lifecycle transition schema")
    if transition.get("previous_transition_sha256") != previous_sha:
        raise LifecycleEvidenceError("lifecycle transition hash chain mismatch")
    snapshot = _mapping(transition.get("snapshot"), "snapshot")
    snapshot_sha = _snapshot_sha256(snapshot)
    if transition.get("to_snapshot_sha256") != snapshot_sha:
        raise LifecycleEvidenceError("lifecycle transition snapshot hash mismatch")
    transition_payload = dict(transition)
    transition_sha = str(transition_payload.pop("transition_sha256"))
    if sha256_bytes(canonical_json_bytes(transition_payload)) != transition_sha:
        raise LifecycleEvidenceError("lifecycle transition SHA mismatch")


def _recover_lifecycle_materialized_state(host_root: Path) -> None:
    root = host_root / "state" / "attempt-lifecycle"
    transitions_root = root / "transitions"
    if not transitions_root.exists():
        return
    recovered: list[dict[str, Any]] = []
    for identity_root in sorted(path for path in transitions_root.iterdir() if path.is_dir()):
        previous_sha: str | None = None
        last_transition: dict[str, Any] | None = None
        for path in sorted(identity_root.glob("*.json")):
            transition = _read_json_mapping(path)
            _validate_transition_record(transition, previous_sha=previous_sha)
            previous_sha = str(transition["transition_sha256"])
            last_transition = transition
        if last_transition is None:
            continue
        snapshot = dict(_mapping(last_transition.get("snapshot"), "snapshot"))
        digest = str(last_transition["identity_digest"])
        snapshot_sha = _snapshot_sha256(snapshot)
        snapshot["snapshot_sha256"] = snapshot_sha
        _atomic_json(root / "attempts" / f"{digest}.json", snapshot)
        _atomic_json(root / "work-items" / f"{digest}.json", snapshot)
        generation_id = _safe_path_segment(str(snapshot["attempt_identity"]["generation_id"]))
        _atomic_json(root / "generations" / generation_id / f"{digest}.json", snapshot)
        watermark = {
            **dict(_mapping(snapshot["reduced_through"], "reduced_through")),
            "attempt_snapshot_sha256": snapshot_sha,
            "journal_transition_sha256": last_transition["transition_sha256"],
            "journal_sequence": last_transition["sequence"],
        }
        _atomic_json(root / "reduced-through" / f"{digest}.json", watermark)
        recovered.append(
            {
                "identity_digest": digest,
                "journal_sequence": last_transition["sequence"],
                "transition_sha256": last_transition["transition_sha256"],
            }
        )
    _atomic_json(
        root / "recovery-ledger.json",
        {
            "schema_version": "attempt_lifecycle_recovery_ledger.v1",
            "recovered_at": datetime.now().astimezone().isoformat(),
            "recovered": recovered,
        },
    )


def _find_attempt_snapshot(
    host_root: Path,
    *,
    job_id: str,
    generation_id: str | None,
) -> Path | None:
    root = host_root / "state" / "attempt-lifecycle" / "attempts"
    if not root.exists():
        return None
    matches: list[Path] = []
    for path in sorted(root.glob("*.json")):
        try:
            snapshot = _read_json_mapping(path)
            identity = _mapping(snapshot.get("attempt_identity"), "attempt_identity")
        except LifecycleEvidenceError:
            continue
        if identity.get("job_id") == job_id and (
            generation_id is None or identity.get("generation_id") == generation_id
        ):
            matches.append(path)
    if len(matches) > 1:
        raise LifecycleEvidenceError("multiple canonical snapshots match current job")
    return matches[0] if matches else None


def _head_kinds_for_job(
    host_root: Path,
    *,
    job_id: str,
    generation_id: str | None,
) -> set[str]:
    kinds: set[str] = set()
    for parts in HEAD_PATH_PARTS.values():
        root = host_root.joinpath(*parts)
        if not root.exists():
            continue
        for path in sorted(root.glob("*.json")):
            try:
                head = _read_json_mapping(path)
                identity = _mapping(head.get("attempt_identity"), "attempt_identity")
            except LifecycleEvidenceError:
                continue
            if identity.get("job_id") == job_id and (
                generation_id is None or identity.get("generation_id") == generation_id
            ):
                kind = head.get("evidence_kind")
                if isinstance(kind, str):
                    kinds.add(kind)
    return kinds


def _generation_requires_canonical(host_root: Path, generation_id: str | None) -> bool:
    if not generation_id:
        return False
    path = host_root / "state" / "refill-generation.json"
    if not path.exists():
        return False
    try:
        generation = _read_json_mapping(path)
    except LifecycleEvidenceError:
        return False
    return (
        generation.get("generation_id") == generation_id
        and generation.get("canonical_lifecycle_boundary") == "post_recorder"
    )


def _verify_snapshot_freshness(host_root: Path, snapshot: Mapping[str, Any]) -> None:
    identity = _mapping(snapshot.get("attempt_identity"), "attempt_identity")
    digest = identity_digest(identity)
    heads = _load_current_producer_heads(host_root).get(digest, [])
    latest_digest = latest_evidence_set_sha256_v1(heads)
    if latest_digest != snapshot.get("latest_evidence_set_sha256"):
        raise LifecycleEvidenceError("canonical lifecycle snapshot is stale")
    watermark = _read_json_mapping(
        host_root / "state" / "attempt-lifecycle" / "reduced-through" / f"{digest}.json"
    )
    if watermark.get("latest_evidence_set_sha256") != latest_digest:
        raise LifecycleEvidenceError("canonical lifecycle watermark is stale")
    if watermark.get("attempt_snapshot_sha256") != _snapshot_sha256(snapshot):
        raise LifecycleEvidenceError("canonical lifecycle snapshot hash mismatch")
    if snapshot.get("snapshot_sha256") not in {None, _snapshot_sha256(snapshot)}:
        raise LifecycleEvidenceError("canonical lifecycle embedded snapshot hash mismatch")


def _read_json_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LifecycleEvidenceError(f"JSON evidence is unreadable: {path}") from exc
    if not isinstance(raw, Mapping):
        raise LifecycleEvidenceError(f"JSON evidence must be an object: {path}")
    return dict(raw)


def _safe_path_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return cleaned or "unknown"


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


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    data = canonical_json_bytes(dict(payload)) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
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
        validate_producer_head(current)
        _validate_head_update(current, head)
    elif head.get("producer_sequence") != 1:
        raise LifecycleEvidenceError("evidence_conflict: producer sequence gap")
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
    if (
        not isinstance(current_sequence, int)
        or isinstance(current_sequence, bool)
        or not isinstance(new_sequence, int)
        or isinstance(new_sequence, bool)
    ):
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


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _SHA256_HEX


def _host_relative_path(host_root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(host_root.resolve())
    except ValueError as exc:
        raise LifecycleEvidenceError("path is not host-root-relative") from exc
    return relative.as_posix()


def _validate_canonical_relative_path(value: Any, *, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise LifecycleEvidenceError(f"{label} must be a non-empty canonical relative path")
    path = Path(value)
    if path.is_absolute() or "\\" in value or ".." in path.parts:
        raise LifecycleEvidenceError(f"{label} must be host-root-relative")


def _source_payload(
    host_root: Path,
    *,
    source_path: Path | None,
    source_ref: SourceRef | Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    if (source_path is None) == (source_ref is None):
        raise LifecycleEvidenceError("exactly one source_path or source_ref is required")
    if source_ref is not None:
        payload = source_ref.as_payload() if isinstance(source_ref, SourceRef) else dict(source_ref)
        validate_source_ref(payload)
        return {"type": "git_ref", **payload}, str(payload["sha256"])
    assert source_path is not None
    resolved = source_path.resolve()
    if not resolved.is_file():
        raise LifecycleEvidenceError(f"source evidence does not exist: {resolved}")
    return {
        "type": "host_path",
        "path": _host_relative_path(host_root, resolved),
    }, sha256_file(resolved)


def validate_source_ref(source_ref: Mapping[str, Any]) -> None:
    required = {"repository", "ref", "commit", "path", "sha256"}
    if set(source_ref) != required:
        raise LifecycleEvidenceError("source_ref has unexpected fields")
    for field in ("repository", "ref", "commit", "path"):
        if not isinstance(source_ref.get(field), str) or not source_ref.get(field):
            raise LifecycleEvidenceError("source_ref fields must be non-empty strings")
    if not _is_git_commit(source_ref.get("commit")):
        raise LifecycleEvidenceError("source_ref commit must be a lowercase Git commit SHA")
    _validate_canonical_relative_path(source_ref["path"], label="source_ref.path")
    if not _is_sha256(source_ref.get("sha256")):
        raise LifecycleEvidenceError("source_ref sha256 must be SHA-256 hex")


def validate_lifecycle_evidence_envelope(envelope: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "evidence_kind",
        "evidence_id",
        "producer_sequence",
        "attempt_identity",
        "identity_digest",
        "source",
        "source_sha256",
        "producer",
        "observed_at",
        "final",
        "closed_status",
        "payload",
    }
    if set(envelope) != required:
        raise LifecycleEvidenceError("lifecycle evidence envelope has unexpected fields")
    if envelope["schema_version"] != ENVELOPE_SCHEMA_VERSION:
        raise LifecycleEvidenceError("unsupported lifecycle evidence envelope schema")
    kind = envelope.get("evidence_kind")
    if kind not in PRODUCER_OWNERS:
        raise LifecycleEvidenceError("unknown evidence kind")
    validate_attempt_identity(_mapping(envelope.get("attempt_identity"), "attempt_identity"))
    if envelope.get("identity_digest") != identity_digest(envelope["attempt_identity"]):
        raise LifecycleEvidenceError("identity digest mismatch")
    if (
        not isinstance(envelope.get("producer_sequence"), int)
        or isinstance(envelope.get("producer_sequence"), bool)
        or envelope["producer_sequence"] < 1
    ):
        raise LifecycleEvidenceError("producer sequence must be positive")
    if not _is_lower_hex(envelope.get("evidence_id"), length=32):
        raise LifecycleEvidenceError("evidence_id is malformed")
    if not _is_sha256(envelope.get("source_sha256")):
        raise LifecycleEvidenceError("source_sha256 is malformed")
    source = _mapping(envelope.get("source"), "source")
    if source.get("type") == "host_path":
        _validate_canonical_relative_path(source.get("path"), label="source.path")
    elif source.get("type") == "git_ref":
        validate_source_ref(
            {k: source[k] for k in ("repository", "ref", "commit", "path", "sha256")}
        )
        if source["sha256"] != envelope["source_sha256"]:
            raise LifecycleEvidenceError("source_ref SHA binding mismatch")
    else:
        raise LifecycleEvidenceError("source type is unsupported")
    producer = _mapping(envelope.get("producer"), "producer")
    if producer.get("name") != PRODUCER_OWNERS[str(kind)] or not isinstance(
        producer.get("release"),
        str,
    ):
        raise LifecycleEvidenceError("producer ownership is invalid")
    if not _is_canonical_timestamp(envelope.get("observed_at")):
        raise LifecycleEvidenceError("observed_at is malformed")
    _validate_final_closed(envelope.get("final"), envelope.get("closed_status"))
    _validate_payload(
        str(kind),
        _mapping(envelope.get("payload"), "payload"),
        identity=envelope["attempt_identity"],
        source=source,
        final=bool(envelope["final"]),
        closed_status=str(envelope["closed_status"]),
    )


def validate_producer_head(
    head: Mapping[str, Any],
    *,
    envelope: Mapping[str, Any] | None = None,
    host_root: Path | None = None,
) -> None:
    required = {
        "head_schema_version",
        "attempt_identity",
        "identity_digest",
        "evidence_kind",
        "producer",
        "producer_sequence",
        "evidence_id",
        "envelope_path",
        "envelope_sha256",
        "final",
        "closed_status",
    }
    if set(head) != required:
        raise LifecycleEvidenceError("producer head has unexpected fields")
    if head["head_schema_version"] != HEAD_SCHEMA_VERSION:
        raise LifecycleEvidenceError("unsupported producer head schema")
    kind = head.get("evidence_kind")
    if kind not in PRODUCER_OWNERS:
        raise LifecycleEvidenceError("unknown producer head kind")
    validate_attempt_identity(_mapping(head.get("attempt_identity"), "attempt_identity"))
    if head.get("identity_digest") != identity_digest(head["attempt_identity"]):
        raise LifecycleEvidenceError("producer head identity digest mismatch")
    if head.get("producer") != PRODUCER_OWNERS[str(kind)]:
        raise LifecycleEvidenceError("producer head ownership conflict")
    if (
        not isinstance(head.get("producer_sequence"), int)
        or isinstance(head.get("producer_sequence"), bool)
        or head["producer_sequence"] < 1
    ):
        raise LifecycleEvidenceError("producer head sequence is malformed")
    if not _is_lower_hex(head.get("evidence_id"), length=32):
        raise LifecycleEvidenceError("producer head evidence_id is malformed")
    _validate_canonical_relative_path(head.get("envelope_path"), label="envelope_path")
    expected_path = producer_envelope_path(
        Path("."),
        evidence_kind=str(kind),
        identity=head["attempt_identity"],
        evidence_id=str(head["evidence_id"]),
    ).as_posix()
    if head["envelope_path"] != expected_path:
        raise LifecycleEvidenceError("producer head envelope path does not match identity/evidence")
    if not _is_sha256(head.get("envelope_sha256")):
        raise LifecycleEvidenceError("producer head envelope_sha256 is malformed")
    _validate_final_closed(head.get("final"), head.get("closed_status"))
    if envelope is not None:
        envelope_bytes = canonical_json_bytes(envelope) + b"\n"
        if head["envelope_sha256"] != sha256_bytes(envelope_bytes):
            raise LifecycleEvidenceError("producer head envelope SHA binding mismatch")
        for field in (
            "evidence_kind",
            "evidence_id",
            "producer_sequence",
            "identity_digest",
            "final",
            "closed_status",
        ):
            if head[field] != envelope[field]:
                raise LifecycleEvidenceError("producer head envelope binding mismatch")
    if host_root is not None:
        bound = host_root.joinpath(*Path(str(head["envelope_path"])).parts)
        if not bound.exists():
            raise LifecycleEvidenceError("producer head envelope file is missing")
        if sha256_file(bound) != head["envelope_sha256"]:
            raise LifecycleEvidenceError("producer head envelope-path/SHA binding mismatch")


def _validate_final_closed(final: Any, closed_status: Any) -> None:
    if not isinstance(final, bool):
        raise LifecycleEvidenceError("final must be a boolean")
    if closed_status not in _CLOSED_STATUSES:
        raise LifecycleEvidenceError("closed_status must be open, final, or not_applicable")
    if closed_status in {"final", "not_applicable"} and final is not True:
        raise LifecycleEvidenceError("closed producer streams must set final=true")
    if closed_status == "open" and final is True:
        raise LifecycleEvidenceError("open producer streams must set final=false")


def _validate_payload(
    kind: str,
    payload: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    source: Mapping[str, Any],
    final: bool,
    closed_status: str,
) -> None:
    spec = _PAYLOAD_CODES[kind]
    allowed = spec["required"] | spec["optional"]
    if not spec["required"] <= set(payload):
        raise LifecycleEvidenceError("producer payload is missing required codes")
    if not set(payload) <= allowed:
        raise LifecycleEvidenceError("producer payload has unsupported codes")
    for field, values in _PAYLOAD_ALLOWED_VALUES.get(kind, {}).items():
        if field in payload and payload[field] not in values:
            raise LifecycleEvidenceError("producer payload code is invalid")
    for key, value in payload.items():
        if key in {
            "dispatch_intent_sha256",
            "submitted_job_sha256",
            "canonical_report_sha256",
            "source_report_sha256",
            "gate_report_sha256",
            "validation_contract_sha256",
        } and value is not None and not _is_sha256(value):
            raise LifecycleEvidenceError(f"{key} must be SHA-256 hex or null")
        if key in {
            "feed_commit",
            "relayed_commit",
            "results_commit",
            "jobs_commit",
            "product_commit",
        }:
            if value is not None and not _is_git_commit(value):
                raise LifecycleEvidenceError(f"{key} must be a Git commit SHA or null")
        if key in {"feed_path", "host_archive_path"} and value is not None:
            _validate_canonical_relative_path(value, label=key)
        if key in {"complete_patch_reconstruction"} and not isinstance(value, bool):
            raise LifecycleEvidenceError(f"{key} must be a boolean")
        if key in {"selected_work_item", "capacity_slot"} and not isinstance(value, Mapping):
            raise LifecycleEvidenceError(f"{key} must be an object")
        if key in {"generation_id", "descendant_job_id", "draft_pr", "product_branch"}:
            if value is not None and (not isinstance(value, str) or not value):
                raise LifecycleEvidenceError(f"{key} must be a non-empty string or null")
    if kind == "dispatch.prepared":
        selected = _mapping(payload.get("selected_work_item"), "selected_work_item")
        capacity = _mapping(payload.get("capacity_slot"), "capacity_slot")
        if set(selected) != {"pipeline_id", "work_item_id", "work_item_source_sha256_v1"}:
            raise LifecycleEvidenceError("selected_work_item has unexpected fields")
        if not isinstance(selected["pipeline_id"], str) or not selected["pipeline_id"]:
            raise LifecycleEvidenceError("selected_work_item.pipeline_id must be non-empty")
        if not isinstance(selected["work_item_id"], str) or not selected["work_item_id"]:
            raise LifecycleEvidenceError("selected_work_item.work_item_id must be non-empty")
        if not _is_sha256(selected["work_item_source_sha256_v1"]):
            raise LifecycleEvidenceError("selected_work_item digest must be SHA-256 hex")
        if set(capacity) != {"slot_id", "desired_capacity", "active_running", "active_queued"}:
            raise LifecycleEvidenceError("capacity_slot has unexpected fields")
        if not isinstance(capacity["slot_id"], str) or not capacity["slot_id"]:
            raise LifecycleEvidenceError("capacity_slot.slot_id must be non-empty")
        for key in ("desired_capacity", "active_running", "active_queued"):
            if not isinstance(capacity[key], int) or isinstance(capacity[key], bool):
                raise LifecycleEvidenceError(f"capacity_slot.{key} must be an integer")
            if capacity[key] < 0:
                raise LifecycleEvidenceError(f"capacity_slot.{key} must be non-negative")
        if capacity["desired_capacity"] != 1:
            raise LifecycleEvidenceError("capacity_slot.desired_capacity must be 1")
        if (
            selected["pipeline_id"] != identity["pipeline_id"]
            or selected["work_item_id"] != identity["work_item_id"]
            or selected["work_item_source_sha256_v1"] != identity["work_item_digest"]
            or payload["generation_id"] != identity["generation_id"]
        ):
            raise LifecycleEvidenceError(
                "dispatch.prepared payload does not match attempt identity"
            )
        _require_finality(kind, final, closed_status, final_expected=True, status="final")
    elif kind == "dispatch.submitted":
        if source.get("type") != "git_ref":
            raise LifecycleEvidenceError("dispatch.submitted source must be a git_ref")
        if (
            payload["feed_commit"] != source.get("commit")
            or payload["feed_path"] != source.get("path")
            or payload["submitted_job_sha256"] != source.get("sha256")
        ):
            raise LifecycleEvidenceError("dispatch.submitted payload does not match source_ref")
        _require_finality(kind, final, closed_status, final_expected=True, status="final")
    elif kind == "host.execution":
        _validate_host_execution_payload(payload, final, closed_status)
    elif kind == "relay.result":
        _validate_relay_result_payload(payload, final, closed_status)
    elif kind == "gate.validation":
        _validate_gate_validation_payload(payload, final, closed_status)
    elif kind == "revision.disposition":
        _validate_revision_disposition_payload(payload, final, closed_status)
    elif kind == "publication_review.disposition":
        _validate_publication_review_payload(payload, final, closed_status)
    reason = payload.get("reason_code")
    if reason is not None:
        if not isinstance(reason, str) or reason not in TERMINAL_REASON_CODES_V1:
            raise LifecycleEvidenceError("terminal reason code is unsupported")
        canonical = TERMINAL_REASON_CODES_V1[reason]["canonical_outcome"]
        if reason.startswith("publication_review.") and "publication_review_disposition" in payload:
            expected = canonical.split("=")[-1]
            if payload["publication_review_disposition"] != expected:
                raise LifecycleEvidenceError("terminal reason code is incompatible")
        if reason.startswith("revision.") and payload.get("revision_disposition") != "exhausted":
            raise LifecycleEvidenceError("terminal reason code is incompatible")
    if closed_status == "not_applicable" and not final:
        raise LifecycleEvidenceError("not_applicable streams must be final")


def _require_finality(
    kind: str,
    final: bool,
    closed_status: str,
    *,
    final_expected: bool,
    status: str,
) -> None:
    if final is not final_expected or closed_status != status:
        raise LifecycleEvidenceError(f"{kind} finality is incompatible with outcome")


def _null_or_absent(payload: Mapping[str, Any], *fields: str) -> bool:
    return all(payload.get(field) is None for field in fields)


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _validate_host_execution_payload(
    payload: Mapping[str, Any],
    final: bool,
    closed_status: str,
) -> None:
    outcome = payload["execution_outcome"]
    if outcome in {"imported", "pending", "running"}:
        _require_finality(
            "host.execution",
            final,
            closed_status,
            final_expected=False,
            status="open",
        )
        if not _null_or_absent(payload, "host_archive_path", "error_class"):
            raise LifecycleEvidenceError("open host.execution outcomes prohibit terminal proof")
        return
    _require_finality("host.execution", final, closed_status, final_expected=True, status="final")
    if not _non_empty_string(payload.get("host_archive_path")):
        raise LifecycleEvidenceError("terminal host.execution requires archive path")
    if outcome == "completed":
        if payload.get("error_class") is not None:
            raise LifecycleEvidenceError("completed host.execution prohibits error_class")
        return
    if not _non_empty_string(payload.get("error_class")):
        raise LifecycleEvidenceError("failed host.execution requires error_class")


def _validate_relay_result_payload(
    payload: Mapping[str, Any],
    final: bool,
    closed_status: str,
) -> None:
    disposition = payload["relay_disposition"]
    if disposition == "relayed":
        _require_finality("relay.result", final, closed_status, final_expected=True, status="final")
        if (
            not _is_git_commit(payload.get("relayed_commit"))
            or not _is_sha256(payload.get("canonical_report_sha256"))
            or not _is_sha256(payload.get("source_report_sha256"))
            or payload.get("complete_patch_reconstruction") is not True
        ):
            raise LifecycleEvidenceError("relayed relay.result requires commit/report proof")
        return
    _require_finality(
        "relay.result",
        final,
        closed_status,
        final_expected=True,
        status="not_applicable",
    )
    if not _null_or_absent(
        payload,
        "relayed_commit",
        "canonical_report_sha256",
        "source_report_sha256",
    ) or payload.get("complete_patch_reconstruction") is not False:
        raise LifecycleEvidenceError("not_applicable relay.result prohibits relay proof")


def _validate_gate_validation_payload(
    payload: Mapping[str, Any],
    final: bool,
    closed_status: str,
) -> None:
    _require_finality("gate.validation", final, closed_status, final_expected=True, status="final")
    outcome = payload["validation_outcome"]
    if outcome in {"passed", "failed"}:
        if (
            not _is_sha256(payload.get("validation_contract_sha256"))
            or not _is_sha256(payload.get("gate_report_sha256"))
            or not _is_git_commit(payload.get("results_commit"))
        ):
            raise LifecycleEvidenceError("gate.validation requires report and results proof")
        state = payload.get("validation_state")
        expected = f"candidate_{outcome}"
        if state != expected:
            raise LifecycleEvidenceError("gate.validation state is incompatible")


def _validate_revision_disposition_payload(
    payload: Mapping[str, Any],
    final: bool,
    closed_status: str,
) -> None:
    disposition = payload["revision_disposition"]
    if disposition == "queued":
        _require_finality(
            "revision.disposition", final, closed_status, final_expected=True, status="final"
        )
        if (
            not _non_empty_string(payload.get("descendant_job_id"))
            or not _is_sha256(payload.get("gate_report_sha256"))
            or not _is_git_commit(payload.get("jobs_commit"))
        ):
            raise LifecycleEvidenceError("queued revision.disposition requires descendant proof")
        if payload.get("reason_code") is not None:
            raise LifecycleEvidenceError("queued revision.disposition prohibits reason_code")
    elif disposition == "not_applicable":
        _require_finality(
            "revision.disposition",
            final,
            closed_status,
            final_expected=True,
            status="not_applicable",
        )
        if not _null_or_absent(payload, "descendant_job_id", "jobs_commit", "reason_code"):
            raise LifecycleEvidenceError("not_applicable revision.disposition prohibits job proof")
        if not _is_sha256(payload.get("gate_report_sha256")):
            raise LifecycleEvidenceError("not_applicable revision.disposition requires gate proof")
    elif disposition == "exhausted":
        _require_finality(
            "revision.disposition", final, closed_status, final_expected=True, status="final"
        )
        reason = payload.get("reason_code")
        if not (isinstance(reason, str) and reason.startswith("revision.exhausted.")):
            raise LifecycleEvidenceError("exhausted revision.disposition requires terminal reason")
    else:
        _require_finality(
            "revision.disposition", final, closed_status, final_expected=True, status="final"
        )


def _validate_publication_review_payload(
    payload: Mapping[str, Any],
    final: bool,
    closed_status: str,
) -> None:
    disposition = payload["publication_review_disposition"]
    terminal_fields = (
        "reason_code",
        "draft_pr",
        "product_branch",
        "product_commit",
        "results_commit",
    )
    if disposition == "awaiting_review":
        _require_finality(
            "publication_review.disposition",
            final,
            closed_status,
            final_expected=False,
            status="open",
        )
        if not _null_or_absent(payload, *terminal_fields):
            raise LifecycleEvidenceError("awaiting_review publication proof is terminal")
    elif disposition == "drafted":
        _require_finality(
            "publication_review.disposition",
            final,
            closed_status,
            final_expected=True,
            status="final",
        )
        if payload.get("reason_code") != "publication_review.drafted.v1":
            raise LifecycleEvidenceError("drafted publication_review requires drafted reason")
        if (
            not _non_empty_string(payload.get("draft_pr"))
            or not _non_empty_string(payload.get("product_branch"))
            or not _is_git_commit(payload.get("product_commit"))
            or not _is_git_commit(payload.get("results_commit"))
        ):
            raise LifecycleEvidenceError("drafted publication_review requires publication proof")
    elif disposition in {"rejected", "terminal_no_publication"}:
        _require_finality(
            "publication_review.disposition",
            final,
            closed_status,
            final_expected=True,
            status="final",
        )
        reason = payload.get("reason_code")
        if not isinstance(reason, str) or reason not in TERMINAL_REASON_CODES_V1:
            raise LifecycleEvidenceError("terminal publication_review requires accepted reason")
        expected = TERMINAL_REASON_CODES_V1[reason]["canonical_outcome"].split("=")[-1]
        if expected != disposition:
            raise LifecycleEvidenceError("terminal publication_review reason is incompatible")
    elif disposition == "not_applicable":
        _require_finality(
            "publication_review.disposition",
            final,
            closed_status,
            final_expected=True,
            status="not_applicable",
        )
        if not _null_or_absent(payload, *terminal_fields):
            raise LifecycleEvidenceError(
                "not_applicable publication_review prohibits terminal proof"
            )
    else:
        _require_finality(
            "publication_review.disposition",
            final,
            closed_status,
            final_expected=True,
            status="final",
        )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LifecycleEvidenceError(f"{label} must be an object")
    return value


def _is_lower_hex(value: Any, *, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and set(value) <= _SHA256_HEX


def _is_git_commit(value: Any) -> bool:
    return isinstance(value, str) and _GIT_COMMIT_RE.fullmatch(value) is not None


def _is_canonical_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True
