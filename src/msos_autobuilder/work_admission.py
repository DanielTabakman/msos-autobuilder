"""Work admission and durable single-writer ownership claims."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class AdmissionError(RuntimeError):
    """Raised when work admission state cannot be trusted."""


class AdmissionStatus(StrEnum):
    CONTINUE_EXISTING_WORK = "CONTINUE_EXISTING_WORK"
    REVIEW_EXISTING_WORK = "REVIEW_EXISTING_WORK"
    NEW_WORK_ADMITTED = "NEW_WORK_ADMITTED"
    FOUNDER_RECONCILIATION_REQUIRED = "FOUNDER_RECONCILIATION_REQUIRED"
    BLOCKED_BY_OWNERSHIP_CONFLICT = "BLOCKED_BY_OWNERSHIP_CONFLICT"


class WorkClassification(StrEnum):
    CONTINUE = "continue"
    REVIEW_AND_MERGE = "review_and_merge"
    REPAIR = "repair"
    SUPERSEDED_DUPLICATE = "superseded_duplicate"
    COMBINE_WITH_RECONCILIATION_PLAN = "combine_with_reconciliation_plan"
    FOUNDER_DECISION_REQUIRED = "founder_decision_required"
    PRESERVE_UNIQUE_WORK = "preserve_unique_work"


ACTIVE_CLAIM_STATES = frozenset({"active"})
TERMINAL_CLAIM_STATES = frozenset(
    {"merged", "superseded", "abandoned", "failed", "released"}
)


@dataclass(frozen=True)
class ObjectiveIdentity:
    repository: str
    linked_issue: int | None
    work_item_id: str
    stable_key: str
    acceptance_contract_sha256: str | None = None
    error_signature: str | None = None
    release_identity: str | None = None

    @property
    def objective_sha256(self) -> str:
        payload = {
            "repository": self.repository,
            "linked_issue": self.linked_issue,
            "work_item_id": self.work_item_id,
            "stable_key": self.stable_key,
            "acceptance_contract_sha256": self.acceptance_contract_sha256,
            "error_signature": self.error_signature,
            "release_identity": self.release_identity,
        }
        return _sha256_json(payload)


@dataclass(frozen=True)
class WorkCandidate:
    kind: str
    number: int | None
    title: str
    state: str
    branch: str | None
    linked_issue: int | None
    objective_sha256: str | None
    acceptance_contract_sha256: str | None
    changed_paths: tuple[str, ...]
    canonical: bool = False
    merged: bool = False
    unique_required_change: bool = False
    url: str | None = None


@dataclass(frozen=True)
class WriterClaim:
    version: int
    objective_sha256: str
    repository: str
    linked_issue: int | None
    writer_id: str
    branch: str | None
    pr_number: int | None
    authorized_paths: tuple[str, ...]
    generation: int
    state: str
    evidence: Mapping[str, Any]
    claimed_at: str
    updated_at: str


@dataclass(frozen=True)
class AdmissionRequest:
    objective: ObjectiveIdentity
    writer_id: str
    branch: str | None
    authorized_paths: tuple[str, ...]
    claim_root: Path | None
    pr_number: int | None = None
    candidates: tuple[WorkCandidate, ...] = ()
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AdmissionDecision:
    status: AdmissionStatus
    objective_sha256: str
    canonical: WorkCandidate | None
    claim: WriterClaim | None
    classifications: tuple[Mapping[str, Any], ...]
    evidence: Mapping[str, Any]
    message: str


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned[:96] or "work"


def normalize_paths(paths: Sequence[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in paths:
        text = str(raw).strip().replace("\\", "/").strip("/")
        if not text or text.startswith("../") or "/../" in f"/{text}/":
            raise AdmissionError(f"unsafe ownership path: {raw!r}")
        normalized.add(text)
    return tuple(sorted(normalized))


def paths_overlap(left: Sequence[str], right: Sequence[str]) -> tuple[str, ...]:
    overlaps: set[str] = set()
    for first in normalize_paths(left):
        for second in normalize_paths(right):
            if (
                first == second
                or first.startswith(second.rstrip("/") + "/")
                or second.startswith(first.rstrip("/") + "/")
            ):
                overlaps.add(first if len(first) >= len(second) else second)
    return tuple(sorted(overlaps))


def classify_candidate(
    request: AdmissionRequest,
    candidate: WorkCandidate,
) -> dict[str, Any]:
    requested_paths = normalize_paths(request.authorized_paths)
    candidate_paths = normalize_paths(candidate.changed_paths)
    overlap = paths_overlap(requested_paths, candidate_paths)
    same_issue = (
        request.objective.linked_issue is not None
        and candidate.linked_issue == request.objective.linked_issue
    )
    same_objective = candidate.objective_sha256 == request.objective.objective_sha256
    same_contract = (
        request.objective.acceptance_contract_sha256 is not None
        and candidate.acceptance_contract_sha256
        == request.objective.acceptance_contract_sha256
    )
    equivalent = same_issue or same_objective or same_contract
    complete_path_overlap = bool(overlap) and set(overlap) >= set(requested_paths)

    if candidate.unique_required_change:
        classification = WorkClassification.PRESERVE_UNIQUE_WORK
    elif equivalent and complete_path_overlap and (candidate.canonical or candidate.merged):
        classification = WorkClassification.CONTINUE
    elif equivalent and overlap:
        classification = WorkClassification.REVIEW_AND_MERGE
    elif overlap and not equivalent:
        classification = WorkClassification.FOUNDER_DECISION_REQUIRED
    else:
        classification = WorkClassification.PRESERVE_UNIQUE_WORK

    return {
        "candidate_kind": candidate.kind,
        "candidate_number": candidate.number,
        "candidate_title": candidate.title,
        "candidate_state": candidate.state,
        "candidate_branch": candidate.branch,
        "candidate_url": candidate.url,
        "classification": classification.value,
        "same_issue": same_issue,
        "same_objective": same_objective,
        "same_acceptance_contract": same_contract,
        "path_overlap": list(overlap),
        "unique_required_change": candidate.unique_required_change,
    }


class ClaimFileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> ClaimFileLock:
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
            raise AdmissionError("could not acquire work-admission claim lock") from exc
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


def _claim_path(root: Path, objective_sha256: str) -> Path:
    return root / "work-admission" / "claims" / f"{objective_sha256}.json"


def _load_claim(path: Path) -> WriterClaim | None:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AdmissionError("work-admission claim is not valid JSON") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise AdmissionError("work-admission claim must be a version 1 object")
    paths = raw.get("authorized_paths")
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        raise AdmissionError("work-admission claim authorized_paths is malformed")
    return WriterClaim(
        version=1,
        objective_sha256=str(raw.get("objective_sha256") or ""),
        repository=str(raw.get("repository") or ""),
        linked_issue=(
            int(raw["linked_issue"]) if raw.get("linked_issue") is not None else None
        ),
        writer_id=str(raw.get("writer_id") or ""),
        branch=str(raw["branch"]) if raw.get("branch") is not None else None,
        pr_number=int(raw["pr_number"]) if raw.get("pr_number") is not None else None,
        authorized_paths=tuple(paths),
        generation=int(raw.get("generation") or 0),
        state=str(raw.get("state") or ""),
        evidence=dict(raw.get("evidence") or {}),
        claimed_at=str(raw.get("claimed_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
    )


def _write_claim(path: Path, claim: WriterClaim) -> None:
    data = json.dumps(asdict(claim), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
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
        temporary.unlink(missing_ok=True)


def release_claim(
    claim_root: Path,
    objective_sha256: str,
    *,
    writer_id: str,
    terminal_state: str,
    evidence: Mapping[str, Any] | None = None,
) -> WriterClaim:
    if terminal_state not in TERMINAL_CLAIM_STATES:
        raise AdmissionError(f"unsupported terminal claim state: {terminal_state}")
    path = _claim_path(claim_root, objective_sha256)
    lock_path = path.with_suffix(".lock")
    with ClaimFileLock(lock_path):
        claim = _load_claim(path)
        if claim is None:
            raise AdmissionError("cannot release missing work-admission claim")
        if claim.writer_id != writer_id:
            raise AdmissionError("only the current writer may terminalize its claim")
        now = _utc_now()
        updated = WriterClaim(
            **{
                **asdict(claim),
                "state": terminal_state,
                "evidence": {**dict(claim.evidence), **dict(evidence or {})},
                "updated_at": now,
            }
        )
        _write_claim(path, updated)
        return updated


def admit_work(request: AdmissionRequest) -> AdmissionDecision:
    authorized_paths = normalize_paths(request.authorized_paths)
    objective_sha = request.objective.objective_sha256
    classifications = tuple(classify_candidate(request, item) for item in request.candidates)
    canonical = _canonical_candidate(request, classifications)
    unresolved = [
        item
        for item in classifications
        if item["classification"]
        in {
            WorkClassification.REVIEW_AND_MERGE.value,
            WorkClassification.FOUNDER_DECISION_REQUIRED.value,
        }
    ]
    evidence = {
        **dict(request.evidence or {}),
        "objective": asdict(request.objective),
        "objective_sha256": objective_sha,
        "authorized_paths": list(authorized_paths),
        "classifications": list(classifications),
    }

    if canonical is not None:
        return AdmissionDecision(
            status=AdmissionStatus.CONTINUE_EXISTING_WORK,
            objective_sha256=objective_sha,
            canonical=canonical,
            claim=None,
            classifications=classifications,
            evidence=evidence,
            message="Canonical work already exists; continue that branch/PR instead.",
        )
    if unresolved:
        status = (
            AdmissionStatus.FOUNDER_RECONCILIATION_REQUIRED
            if any(
                item["classification"]
                == WorkClassification.FOUNDER_DECISION_REQUIRED.value
                for item in unresolved
            )
            else AdmissionStatus.REVIEW_EXISTING_WORK
        )
        return AdmissionDecision(
            status=status,
            objective_sha256=objective_sha,
            canonical=None,
            claim=None,
            classifications=classifications,
            evidence=evidence,
            message="Existing work overlaps this request and requires reconciliation.",
        )
    if request.claim_root is None:
        return AdmissionDecision(
            status=AdmissionStatus.NEW_WORK_ADMITTED,
            objective_sha256=objective_sha,
            canonical=None,
            claim=None,
            classifications=classifications,
            evidence={**evidence, "claim_persisted": False},
            message="Read-only admission passed; no durable claim root was configured.",
        )

    path = _claim_path(request.claim_root, objective_sha)
    lock_path = path.with_suffix(".lock")
    with ClaimFileLock(lock_path):
        existing = _load_claim(path)
        now = _utc_now()
        if existing is not None and existing.state in ACTIVE_CLAIM_STATES:
            overlap = paths_overlap(existing.authorized_paths, authorized_paths)
            if existing.writer_id != request.writer_id and overlap:
                return AdmissionDecision(
                    status=AdmissionStatus.BLOCKED_BY_OWNERSHIP_CONFLICT,
                    objective_sha256=objective_sha,
                    canonical=None,
                    claim=existing,
                    classifications=classifications,
                    evidence={
                        **evidence,
                        "conflicting_claim": asdict(existing),
                        "claim_path": path.as_posix(),
                        "path_overlap": list(overlap),
                    },
                    message="Active durable writer claim owns this objective/path set.",
                )
            if existing.writer_id == request.writer_id:
                return AdmissionDecision(
                    status=AdmissionStatus.NEW_WORK_ADMITTED,
                    objective_sha256=objective_sha,
                    canonical=None,
                    claim=existing,
                    classifications=classifications,
                    evidence={
                        **evidence,
                        "existing_claim": asdict(existing),
                        "claim_path": path.as_posix(),
                    },
                    message="Existing durable writer claim matches this writer.",
                )
        generation = 1 if existing is None else existing.generation + 1
        claim = WriterClaim(
            version=1,
            objective_sha256=objective_sha,
            repository=request.objective.repository,
            linked_issue=request.objective.linked_issue,
            writer_id=request.writer_id,
            branch=request.branch,
            pr_number=request.pr_number,
            authorized_paths=authorized_paths,
            generation=generation,
            state="active",
            evidence=evidence,
            claimed_at=now,
            updated_at=now,
        )
        _write_claim(path, claim)
        return AdmissionDecision(
            status=AdmissionStatus.NEW_WORK_ADMITTED,
            objective_sha256=objective_sha,
            canonical=None,
            claim=claim,
            classifications=classifications,
            evidence={**evidence, "claim_path": path.as_posix(), "claim": asdict(claim)},
            message="New work admitted and durable writer claim acquired.",
        )


def _canonical_candidate(
    request: AdmissionRequest,
    classifications: Sequence[Mapping[str, Any]],
) -> WorkCandidate | None:
    by_key = {
        (item.kind, item.number, item.branch): item
        for item in request.candidates
    }
    for classified in classifications:
        if classified.get("classification") != WorkClassification.CONTINUE.value:
            continue
        key = (
            str(classified.get("candidate_kind") or ""),
            classified.get("candidate_number"),
            classified.get("candidate_branch"),
        )
        candidate = by_key.get(key)
        if candidate is not None:
            return candidate
    return None


def candidate_from_pr(
    *,
    number: int,
    title: str,
    state: str,
    branch: str,
    changed_paths: Sequence[str],
    linked_issue: int | None = None,
    objective_sha256: str | None = None,
    acceptance_contract_sha256: str | None = None,
    canonical: bool = False,
    merged: bool = False,
    unique_required_change: bool = False,
    url: str | None = None,
) -> WorkCandidate:
    return WorkCandidate(
        kind="pull_request",
        number=number,
        title=title,
        state=state,
        branch=branch,
        linked_issue=linked_issue,
        objective_sha256=objective_sha256,
        acceptance_contract_sha256=acceptance_contract_sha256,
        changed_paths=normalize_paths(changed_paths),
        canonical=canonical,
        merged=merged,
        unique_required_change=unique_required_change,
        url=url,
    )


def objective_identity_from_work(
    *,
    repository: str,
    linked_issue: int | None,
    work_item_id: str,
    stable_parts: Mapping[str, Any],
    acceptance_contract_sha256: str | None = None,
    error_signature: str | None = None,
    release_identity: str | None = None,
) -> ObjectiveIdentity:
    stable_key = _safe_segment(_sha256_json(stable_parts))
    return ObjectiveIdentity(
        repository=repository,
        linked_issue=linked_issue,
        work_item_id=work_item_id,
        stable_key=stable_key,
        acceptance_contract_sha256=acceptance_contract_sha256,
        error_signature=error_signature,
        release_identity=release_identity,
    )
