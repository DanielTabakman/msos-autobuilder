"""One-shot founder ``build next`` dispatcher.

The dispatcher consumes PPE's read-only founder portfolio output and submits one
already-approved immutable job to the existing Autobuilder feed. It does not own
portfolio readiness or priority policy.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .codex_shadow import load_codex_host_config
from .persistent_host import HostPaths, load_persistent_host_config, parse_host_job
from .validation_contract import build_ppe_validation_contract


class BuildNextError(RuntimeError):
    """Raised when build-next validation or feed submission fails closed."""


SOURCE_REPOSITORY = "DanielTabakman/Probability-prediction-engine"

FORBIDDEN_AUTHORITY_PATHS = (
    ".git/**",
    ".github/workflows/**",
    "artifacts/**",
    "runtime/**",
    "state/**",
    "queue/**",
    "config/founder_pipeline_registry.json",
    "docs/SOP/PHASE_QUEUE.json",
    "docs/SOP/ACTIVE_PHASE_MANIFEST.json",
    "docs/SOP/FOUNDER_PIPELINE_COMMANDS_V1.md",
    "docs/SOP/PIPELINE_CREATION_SOP_V1.md",
    "docs/SOP/SCHEDULED_AUTOBUILDER_LANE_POLICY_V1.md",
    "docs/SOP/CHATGPT_GITHUB_CODEX_CONTROL_PLANE_V1.md",
    "docs/SOP/SPRINT_*.md",
    "docs/SOP/*SELECTION*.md",
    "docs/SOP/POST_*_SELECTION*.md",
    "docs/SOP/*PRIORITY*.md",
    "docs/SOP/*FRONTIER*.md",
    "docs/SOP/*MANIFEST*.json",
    "artifacts/orchestrator/**",
    "artifacts/control_plane/**",
    "artifacts/relay/**",
    "artifacts/leases/**",
)

BROAD_WRITABLE_ROOTS = {
    ".",
    "",
    "docs",
    "docs/",
    "docs/SOP",
    "docs/SOP/",
    "config",
    "config/",
    "artifacts",
    "artifacts/",
}


@dataclass(frozen=True)
class SourceIdentity:
    remote: str
    remote_url: str
    repository: str
    ref: str
    remote_ref: str
    commit: str


@dataclass(frozen=True)
class NativeSlicePacket:
    slice_id: str
    build_branch: str
    layer_preset: str
    worker_mode: str | None
    declared_plane: str
    touch_set: tuple[str, ...]
    sequence_index: int
    total_slices: int
    previous_slices: tuple[str, ...]
    following_slices: tuple[str, ...]
    sprint_spec_path: str | None
    selection_record: str | None
    raw_slice: Mapping[str, Any]


class FeedMutationLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> FeedMutationLock:
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
            raise BuildNextError("could not acquire build-next feed mutation lock") from exc
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


@dataclass(frozen=True)
class RefillAttemptContext:
    generation_id: str
    attempt_ordinal: int
    retry_ordinal: int = 0
    reason: str = "initial"
    selected_work_item_id: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,128}", self.generation_id):
            raise ValueError("generation_id must be a safe immutable identifier")
        if self.attempt_ordinal < 1:
            raise ValueError("attempt_ordinal must be positive")
        if self.retry_ordinal < 0:
            raise ValueError("retry_ordinal must be non-negative")

    def evidence(self, selected_work_item_id: str) -> dict[str, Any]:
        return {
            "version": 1,
            "generation_id": self.generation_id,
            "attempt_ordinal": self.attempt_ordinal,
            "retry_ordinal": self.retry_ordinal,
            "reason": self.reason,
            "selected_work_item_id": self.selected_work_item_id or selected_work_item_id,
        }


@dataclass(frozen=True)
class BuildNextConfig:
    ppe_repo: Path
    feed_repo_url: str
    jobs_branch: str = "jobs"
    jobs_path: str = "jobs/approved"
    checkout_root: Path | None = None
    host_root: Path | None = None
    max_snapshot_age_seconds: int = 600
    requested_by: str = "founder build next"
    submit: bool = True
    source_remote: str = "origin"
    source_ref: str = "main"
    expected_source_repository: str = SOURCE_REPOSITORY
    allow_test_local_source_remote: bool = False
    exclude_work_item_ids: tuple[str, ...] = ()
    refill_attempt: RefillAttemptContext | None = None

    def __post_init__(self) -> None:
        if not self.feed_repo_url.strip():
            raise ValueError("feed_repo_url is required")
        if self.jobs_branch in {"main", "master"}:
            raise ValueError("jobs_branch must not be a product/default branch")
        rel = Path(self.jobs_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("jobs_path must be a safe relative path")
        if self.max_snapshot_age_seconds < 1:
            raise ValueError("max_snapshot_age_seconds must be positive")
        if not self.source_remote.strip() or not self.source_ref.strip():
            raise ValueError("source remote/ref are required")
        if not self.expected_source_repository.strip():
            raise ValueError("expected_source_repository is required")

    @classmethod
    def from_service_config(
        cls,
        service_config: str | Path,
        *,
        checkout_root: Path | None = None,
        max_snapshot_age_seconds: int = 600,
        requested_by: str = "founder build next",
        submit: bool = True,
        allow_test_local_source_remote: bool = False,
    ) -> BuildNextConfig:
        service = load_persistent_host_config(service_config)
        if service.feed is None:
            raise ValueError("persistent host service config does not enable a job feed")
        host_config = load_codex_host_config(service.codex_host_config)
        return cls(
            ppe_repo=host_config.source_repo,
            feed_repo_url=service.feed.repo_url,
            jobs_branch=service.feed.branch,
            jobs_path=service.feed.relative_path,
            checkout_root=checkout_root,
            host_root=service.host_root,
            max_snapshot_age_seconds=max_snapshot_age_seconds,
            requested_by=requested_by,
            submit=submit,
            allow_test_local_source_remote=allow_test_local_source_remote,
        )


@dataclass(frozen=True)
class BuildNextReceipt:
    status: str
    pipeline_id: str | None
    work_item_id: str | None
    job_id: str | None
    repository: str | None
    source_commit: str | None
    feed_path: str | None
    feed_commit: str | None
    message: str
    evidence: Mapping[str, Any]
    submitted: bool = False
    projected_status: str | None = None
    publication_enabled: bool = False
    merge_enabled: bool = False
    product_main_write_enabled: bool = False


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


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    accepted: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if proc.returncode not in accepted:
        detail = (proc.stderr or proc.stdout or "command failed").strip()
        raise BuildNextError(f"{' '.join(argv)}: {detail}")
    return proc


def _git(repo: Path | None, *args: str, accepted: tuple[int, ...] = (0,)) -> str:
    argv = ["git"]
    if repo is not None:
        argv.extend(["-C", str(repo)])
    argv.extend(args)
    return _run(argv, accepted=accepted).stdout.strip()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildNextError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise BuildNextError(f"{label} must be a JSON object")
    return data


def _safe_id(value: str, *, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned[:120] or fallback


def _normalize_work_item_id(value: Any) -> str:
    return _safe_id(str(value or ""), fallback="work-item")


def _safe_relative(value: Any, label: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise BuildNextError(f"{label} must be a safe relative path")
    return path.as_posix()


def _collect_snapshot(ppe_repo: Path, exclude_work_item_ids: Sequence[str] = ()) -> dict[str, Any]:
    script = ppe_repo / "scripts" / "founder_portfolio.py"
    if not script.is_file():
        raise BuildNextError(f"PPE founder portfolio script is missing: {script}")
    argv = [
        sys.executable,
        str(script),
        "what's next",
        "--repo-root",
        str(ppe_repo),
        "--json",
    ]
    for work_item_id in exclude_work_item_ids:
        argv.extend(["--exclude-work-item-id", _normalize_work_item_id(work_item_id)])
    proc = _run(
        argv,
        cwd=ppe_repo,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise BuildNextError("PPE founder portfolio output was not JSON") from exc
    if not isinstance(payload, dict):
        raise BuildNextError("PPE founder portfolio output must be an object")
    return payload


def _validate_selection_context(
    snapshot: Mapping[str, Any],
    requested_exclusions: Sequence[str],
) -> None:
    requested = [_normalize_work_item_id(item) for item in requested_exclusions]
    if not requested:
        return
    context = snapshot.get("selection_context")
    if not isinstance(context, dict):
        raise BuildNextError("PPE selection_context is missing for requested exclusions")
    if context.get("scope") != "request":
        raise BuildNextError("PPE selection_context scope must be request-scoped")
    returned_raw = context.get("requested_exclusions")
    if not isinstance(returned_raw, list):
        raise BuildNextError("PPE selection_context requested_exclusions is malformed")
    returned = [_normalize_work_item_id(item) for item in returned_raw]
    if returned != requested:
        raise BuildNextError("PPE selection_context did not echo requested exclusions")


def _normalize_github_repository(url: str) -> str | None:
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


def _pipeline(registry: Mapping[str, Any], pipeline_id: str) -> dict[str, Any]:
    for raw in registry.get("pipelines") or []:
        if isinstance(raw, dict) and str(raw.get("pipeline_id") or "") == pipeline_id:
            return raw
    raise BuildNextError(f"pipeline {pipeline_id!r} is not registered")


def _snapshot_pipeline(snapshot: Mapping[str, Any], pipeline_id: str) -> dict[str, Any]:
    for raw in snapshot.get("pipelines") or []:
        if isinstance(raw, dict) and str(raw.get("pipeline_id") or "") == pipeline_id:
            return raw
    raise BuildNextError(f"pipeline {pipeline_id!r} is missing from selection output")


def _ready_work(pipe: Mapping[str, Any], work_item_id: str) -> dict[str, Any]:
    matches = [
        item
        for item in pipe.get("ready_work") or []
        if isinstance(item, dict) and str(item.get("work_item_id") or "") == work_item_id
    ]
    if len(matches) != 1:
        raise BuildNextError(f"selected work item {work_item_id!r} is not exactly one READY item")
    work = dict(matches[0])
    if work.get("state") != "READY_TO_BUILD":
        raise BuildNextError(f"selected work item {work_item_id!r} is not READY_TO_BUILD")
    return work


def _validate_snapshot(snapshot: Mapping[str, Any], max_age_seconds: int) -> None:
    if snapshot.get("version") != 1 or snapshot.get("read_only") is not True:
        raise BuildNextError("PPE selection output must be read-only version 1")
    errors = snapshot.get("registry_errors") or []
    if errors:
        raise BuildNextError(f"PPE registry validation errors: {errors}")
    as_of = _parse_utc(snapshot.get("as_of"))
    if as_of is None:
        raise BuildNextError("PPE selection output is missing a valid as_of timestamp")
    age = (datetime.now(UTC) - as_of).total_seconds()
    if age > max_age_seconds:
        raise BuildNextError(f"PPE selection output is stale: age {int(age)}s")


def _validate_registry_adapter(registry_pipe: Mapping[str, Any]) -> None:
    if registry_pipe.get("registration_stage") not in {"EXECUTION_READY", "SCHEDULE_READY"}:
        raise BuildNextError("selected pipeline is not execution-ready")
    if registry_pipe.get("canonical_repo") != SOURCE_REPOSITORY:
        raise BuildNextError("v1 supports only the registered PPE/MSOS product repository")
    scheduling = (
        registry_pipe.get("scheduling")
        if isinstance(registry_pipe.get("scheduling"), dict)
        else {}
    )
    if scheduling.get("build_next_eligible") is not True:
        raise BuildNextError("selected pipeline is not build-next eligible")
    adapter = (
        registry_pipe.get("build_adapter")
        if isinstance(registry_pipe.get("build_adapter"), dict)
        else {}
    )
    if adapter.get("adapter") != "ppe_operator":
        raise BuildNextError("selected pipeline does not use the registered PPE build adapter")
    if adapter.get("readiness") != "READY_FOR_MANUAL_OR_SINGLE_DISPATCH":
        raise BuildNextError("selected pipeline build adapter is not single-dispatch ready")
    if adapter.get("dispatch_commands_enabled") is not True:
        raise BuildNextError(
            "selected pipeline build adapter has dispatch_commands_enabled disabled; "
            "PPE issue #5366 must explicitly enable dispatch before Autobuilder can submit"
        )
    authority = (
        registry_pipe.get("authority")
        if isinstance(registry_pipe.get("authority"), dict)
        else {}
    )
    publication = str(authority.get("publication_authority") or "").lower()
    if "draft" not in publication or "controlled publisher" not in publication:
        raise BuildNextError(
            "selected pipeline authority does not preserve controlled draft publication"
        )


def _validate_pipeline_runtime(pipe: Mapping[str, Any]) -> None:
    if pipe.get("state") != "READY_TO_BUILD":
        raise BuildNextError("selected pipeline is not READY_TO_BUILD")
    if pipe.get("running_work"):
        raise BuildNextError("selected pipeline already has running work")
    if pipe.get("queued_work"):
        raise BuildNextError("selected pipeline already has queued work")
    if pipe.get("backpressure"):
        raise BuildNextError("selected pipeline has backpressure")
    stale = pipe.get("stale_evidence") or []
    if stale:
        raise BuildNextError(f"selected pipeline has stale evidence: {stale}")
    for item in pipe.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "stale":
            raise BuildNextError("selected pipeline evidence is stale")


def _path_covers(authority_path: str, forbidden_path: str) -> bool:
    grant = authority_path.rstrip("/")
    forbidden = forbidden_path.rstrip("/")
    if not grant or grant in BROAD_WRITABLE_ROOTS:
        return True
    if forbidden.endswith("/**"):
        forbidden = forbidden[:-3]
    if "*" in grant:
        return False
    if "*" in forbidden:
        prefix = forbidden.split("*", 1)[0].rstrip("/")
        return bool(prefix) and (prefix == grant or prefix.startswith(grant + "/"))
    return forbidden == grant or forbidden.startswith(grant + "/")


def _validate_writable_path(path: str) -> str:
    rel = _safe_relative(path, "touchSet entry")
    normalized = rel.rstrip("/")
    if any(char in rel for char in "*?["):
        raise BuildNextError(f"wildcard writable path is not allowed in v1: {rel}")
    if rel in BROAD_WRITABLE_ROOTS or normalized in BROAD_WRITABLE_ROOTS:
        raise BuildNextError(f"broad writable path is not allowed: {rel}")
    for forbidden in FORBIDDEN_AUTHORITY_PATHS:
        if rel == forbidden or fnmatch.fnmatchcase(rel, forbidden) or _path_covers(rel, forbidden):
            raise BuildNextError(
                f"writable path {rel!r} overlaps forbidden authority {forbidden!r}"
            )
    return rel


def _select_native_slice(plan: Mapping[str, Any]) -> NativeSlicePacket:
    raw_slices = plan.get("slices")
    if not isinstance(raw_slices, list) or not raw_slices:
        raise BuildNextError("selected phase plan does not declare native slices")
    slices = [item for item in raw_slices if isinstance(item, dict)]
    if len(slices) != len(raw_slices):
        raise BuildNextError("selected phase plan contains invalid native slice entries")

    candidates: list[tuple[int, dict[str, Any]]] = []
    for index, item in enumerate(slices):
        plane = str(item.get("declaredPlane") or "").strip().upper()
        layer = str(item.get("layerPreset") or "").strip().upper()
        touch_set = item.get("touchSet")
        if item.get("closeout") or "SMOKE" in str(item.get("sliceId") or "").upper():
            continue
        if plane == "PRODUCT-PLANE" and layer != "CONTROL" and isinstance(touch_set, list):
            candidates.append((index, item))
    if not candidates:
        raise BuildNextError("selected phase plan has no bounded native implementation slice")
    index, selected = candidates[0]
    touch_set_raw = selected.get("touchSet")
    if not isinstance(touch_set_raw, list) or not touch_set_raw:
        raise BuildNextError("selected native implementation slice lacks a writable touch set")
    touch_set = tuple(_validate_writable_path(str(item)) for item in touch_set_raw)
    if len(set(touch_set)) != len(touch_set):
        raise BuildNextError("selected native implementation slice has duplicate touch paths")

    slice_id = str(selected.get("sliceId") or "").strip()
    build_branch = str(selected.get("buildBranch") or "").strip()
    layer_preset = str(selected.get("layerPreset") or "").strip()
    if not slice_id or not build_branch or not layer_preset:
        raise BuildNextError("selected native implementation slice is missing identity fields")
    return NativeSlicePacket(
        slice_id=slice_id,
        build_branch=build_branch,
        layer_preset=layer_preset,
        worker_mode=str(selected.get("workerMode") or "").strip() or None,
        declared_plane=str(selected.get("declaredPlane") or "").strip(),
        touch_set=touch_set,
        sequence_index=index,
        total_slices=len(slices),
        previous_slices=tuple(str(item.get("sliceId") or "") for item in slices[:index]),
        following_slices=tuple(str(item.get("sliceId") or "") for item in slices[index + 1 :]),
        sprint_spec_path=(
            _safe_relative(plan.get("sprintSpecPath"), "sprintSpecPath")
            if plan.get("sprintSpecPath")
            else None
        ),
        selection_record=(
            _safe_relative(plan.get("selectionRecord"), "selectionRecord")
            if plan.get("selectionRecord")
            else None
        ),
        raw_slice=dict(selected),
    )


def _is_smoke_or_closeout_slice(raw_slice: Mapping[str, Any]) -> bool:
    slice_id = str(raw_slice.get("sliceId") or "").upper()
    return bool(raw_slice.get("closeout")) or "SMOKE" in slice_id or "CLOSEOUT" in slice_id


def _prerequisite_packet(work: Mapping[str, Any]) -> Mapping[str, Any]:
    packet = work.get("native_prerequisites") or work.get("prerequisite_status")
    if not isinstance(packet, dict):
        raise BuildNextError("missing pipeline-native prerequisite evidence for selected slice")
    if packet.get("read_only") is not True:
        raise BuildNextError("pipeline-native prerequisite evidence is not read-only")
    if packet.get("source") not in {"ppe_native_read_only", "pipeline_native"}:
        raise BuildNextError("prerequisite evidence is not pipeline-native")
    return packet


def _status_by_slice(packet: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    statuses = packet.get("statuses")
    if isinstance(statuses, dict):
        return {
            str(slice_id): status
            for slice_id, status in statuses.items()
            if isinstance(status, Mapping)
        }
    if isinstance(statuses, list):
        return {
            str(status.get("slice_id") or status.get("sliceId") or ""): status
            for status in statuses
            if isinstance(status, Mapping)
        }
    return {}


def _validate_native_prerequisites(
    work: Mapping[str, Any],
    plan: Mapping[str, Any],
    native_slice: NativeSlicePacket,
) -> Mapping[str, Any]:
    raw_slices = [item for item in plan.get("slices") or [] if isinstance(item, dict)]
    required = [
        str(item.get("sliceId") or "")
        for item in raw_slices[: native_slice.sequence_index]
        if str(item.get("sliceId") or "").strip() and not _is_smoke_or_closeout_slice(item)
    ]
    if not required:
        return {
            "source": "not_required",
            "required_slices": [],
            "satisfied_slices": [],
            "non_blocking_slices": [],
        }
    packet = _prerequisite_packet(work)
    statuses = _status_by_slice(packet)
    satisfied: list[str] = []
    non_blocking: list[str] = []
    for slice_id in required:
        status = statuses.get(slice_id)
        if status is None:
            raise BuildNextError(
                f"missing pipeline-native prerequisite evidence for unmet slice {slice_id}"
            )
        state = str(status.get("status") or status.get("state") or "").strip().lower()
        if state in {"complete", "completed"}:
            satisfied.append(slice_id)
            continue
        if status.get("non_blocking") is True or status.get("nonBlocking") is True:
            non_blocking.append(slice_id)
            continue
        raise BuildNextError(f"unmet prerequisite slice {slice_id} is not complete")
    return {
        "source": packet.get("source"),
        "evidence": packet.get("evidence"),
        "required_slices": required,
        "satisfied_slices": satisfied,
        "non_blocking_slices": non_blocking,
    }


def _plan_text(ppe_repo: Path, rel: str) -> tuple[dict[str, Any], str, str]:
    safe_rel = _safe_relative(rel, "phase plan trace")
    path = (ppe_repo / safe_rel).resolve()
    try:
        path.relative_to(ppe_repo.resolve())
    except ValueError as exc:
        raise BuildNextError("phase plan trace escapes the PPE checkout") from exc
    if not path.is_file():
        raise BuildNextError(f"selected phase plan is missing: {safe_rel}")
    text = path.read_text(encoding="utf-8")
    data = _load_json(path, "phase plan")
    return data, safe_rel, text


def _source_identity(config: BuildNextConfig, ppe_repo: Path) -> SourceIdentity:
    remote_url = _git(ppe_repo, "remote", "get-url", config.source_remote)
    repository = _normalize_github_repository(remote_url)
    if repository is None:
        if config.allow_test_local_source_remote:
            repository = config.expected_source_repository
        else:
            raise BuildNextError(
                f"PPE source remote {config.source_remote!r} is not a canonical GitHub URL"
            )
    if repository != config.expected_source_repository:
        raise BuildNextError(
            "PPE source remote resolves to "
            f"{repository!r}, expected {config.expected_source_repository!r}"
        )
    _git(ppe_repo, "fetch", "--no-tags", config.source_remote, config.source_ref)
    remote_ref = f"{config.source_remote}/{config.source_ref}"
    remote_commit = _git(ppe_repo, "rev-parse", remote_ref)
    commit = _git(ppe_repo, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BuildNextError("PPE source commit is not a full SHA")
    if not re.fullmatch(r"[0-9a-f]{40}", remote_commit):
        raise BuildNextError("PPE remote source commit is not a full SHA")
    dirty = _git(ppe_repo, "status", "--porcelain")
    if dirty:
        raise BuildNextError("PPE source checkout is dirty; cannot pin an exact source identity")
    if commit != remote_commit:
        raise BuildNextError(
            f"PPE source HEAD {commit} does not match freshly fetched {remote_ref} {remote_commit}"
        )
    return SourceIdentity(
        remote=config.source_remote,
        remote_url=remote_url,
        repository=repository,
        ref=config.source_ref,
        remote_ref=remote_ref,
        commit=commit,
    )


def _evidence_identity(
    ppe_repo: Path,
    *,
    source_identity: SourceIdentity,
    snapshot: Mapping[str, Any],
    selected: Mapping[str, Any],
    plan_rel: str,
    native_slice: NativeSlicePacket,
    prerequisite_evidence: Mapping[str, Any],
    refill_attempt: Mapping[str, Any] | None = None,
    requested_exclusions: Sequence[str] = (),
) -> dict[str, Any]:
    files = {
        "registry": "config/founder_pipeline_registry.json",
        "phase_queue": "docs/SOP/PHASE_QUEUE.json",
        "active_manifest": "docs/SOP/ACTIVE_PHASE_MANIFEST.json",
        "phase_plan": plan_rel,
    }
    file_hashes = {
        key: _sha256_file(ppe_repo / rel)
        for key, rel in files.items()
        if (ppe_repo / rel).is_file()
    }
    stable = {
        "version": 1,
        "source": asdict(source_identity),
        "selected": selected,
        "native_slice": {
            "slice_id": native_slice.slice_id,
            "build_branch": native_slice.build_branch,
            "layer_preset": native_slice.layer_preset,
            "worker_mode": native_slice.worker_mode,
            "declared_plane": native_slice.declared_plane,
            "touch_set": list(native_slice.touch_set),
            "sequence_index": native_slice.sequence_index,
            "total_slices": native_slice.total_slices,
            "previous_slices": list(native_slice.previous_slices),
            "following_slices": list(native_slice.following_slices),
        },
        "prerequisites": dict(prerequisite_evidence),
        "file_hashes": file_hashes,
        "selection_explanation": snapshot.get("recommended_next_action", {}).get(
            "selection_explanation"
        ),
        "requested_exclusions": [_normalize_work_item_id(item) for item in requested_exclusions],
    }
    if refill_attempt is not None:
        stable["refill_attempt"] = dict(refill_attempt)
    return {
        **stable,
        "identity_sha256": _sha256_text(json.dumps(stable, sort_keys=True, separators=(",", ":"))),
    }


def _instruction(
    *,
    pipeline_id: str,
    work: Mapping[str, Any],
    plan_rel: str,
    native_slice: NativeSlicePacket,
    forbidden_paths: Sequence[str],
    source_identity: SourceIdentity,
    evidence_identity: Mapping[str, Any],
    prerequisite_evidence: Mapping[str, Any],
    refill_attempt: Mapping[str, Any] | None = None,
) -> str:
    refill_lines = []
    if refill_attempt is not None:
        refill_lines = [
            "",
            "Refill attempt context:",
            json.dumps(dict(refill_attempt), indent=2, sort_keys=True),
        ]
    return "\n".join(
        [
            "Implementation thread. THREAD_ROLE: codex_build.",
            "",
            "Implement only the bounded PPE/MSOS work item selected by the accepted "
            "founder portfolio registry.",
            f"Pipeline ID: {pipeline_id}",
            f"Work-item ID: {work.get('work_item_id')}",
            "Source repository: DanielTabakman/Probability-prediction-engine",
            f"Exact source commit: {source_identity.commit}",
            f"Canonical source ref: {source_identity.remote_ref}",
            f"Canonical source remote: {source_identity.remote_url}",
            f"Registered phase plan: {plan_rel}",
            f"Native sliceId: {native_slice.slice_id}",
            f"Native buildBranch: {native_slice.build_branch}",
            f"Native layerPreset: {native_slice.layer_preset}",
            f"Native workerMode: {native_slice.worker_mode or 'default'}",
            f"Native sequence: {native_slice.sequence_index + 1} of {native_slice.total_slices}",
            "",
            "Authority and publication boundary:",
            "- Do not write product main or merge.",
            "- Do not force-push, enable automerge, mark a PR ready, or publish directly.",
            "- Produce only workspace changes for the Autobuilder relay/gate/publisher path.",
            "- Preserve the controlled draft publisher as the only product publisher.",
            "- Treat sprint specs, selection records, queues, manifests, registries, "
            "leases, and operator state as read-only canon/evidence.",
            "- Do not perform smoke, closeout, selection, queue, or control-plane updates.",
            "",
            "Allowed paths:",
            *[f"- {path}" for path in native_slice.touch_set],
            "",
            "Forbidden paths:",
            *[f"- {path}" for path in forbidden_paths],
            "",
            "Acceptance criteria and validation requirements:",
            "- Implement only the selected native PPE implementation slice.",
            "- Preserve native PPE sequencing; do not execute later smoke or closeout slices.",
            f"- Read sprint spec as canon: {native_slice.sprint_spec_path or 'not declared'}",
            f"- Read selection record as canon: {native_slice.selection_record or 'not declared'}",
            "- Add or update focused tests for changed behavior.",
            "- Run the focused tests and relevant repository gates before closeout.",
            "- Return evidence suitable for the existing relay, candidate gate, revision loop, "
            "and controlled publisher.",
            "",
            "Portfolio-selection evidence identity:",
            json.dumps(dict(evidence_identity), indent=2, sort_keys=True),
            *refill_lines,
            "",
            "Pipeline-native prerequisite evidence:",
            json.dumps(dict(prerequisite_evidence), indent=2, sort_keys=True),
            "",
            "Relevant canon/task packet:",
            json.dumps(
                {
                    "phase_plan": plan_rel,
                    "selected_slice": dict(native_slice.raw_slice),
                    "previous_slices": list(native_slice.previous_slices),
                    "following_slices": list(native_slice.following_slices),
                    "sprint_spec_path": native_slice.sprint_spec_path,
                    "selection_record": native_slice.selection_record,
                },
                indent=2,
                sort_keys=True,
            ),
            "",
            "Non-goals:",
            "- Do not charter new product scope.",
            "- Do not alter PPE registry or priority policy.",
            "- Do not run continuous refill or dispatch additional work.",
        ]
    )


def _job_id(
    pipeline_id: str,
    work_item_id: str,
    native_slice: NativeSlicePacket,
    source_commit: str,
    refill_attempt: Mapping[str, Any] | None = None,
) -> str:
    if refill_attempt is not None:
        digest = _sha256_text(json.dumps(dict(refill_attempt), sort_keys=True))[:10]
        return _safe_id(
            f"build-next-{pipeline_id}-{work_item_id}-{native_slice.slice_id}-"
            f"{source_commit[:12]}-g{str(refill_attempt.get('generation_id'))[:12]}-"
            f"a{refill_attempt.get('attempt_ordinal')}-r{refill_attempt.get('retry_ordinal')}-{digest}"
        )
    return _safe_id(
        f"build-next-{pipeline_id}-{work_item_id}-{native_slice.slice_id}-{source_commit[:12]}"
    )


def _build_job(
    *,
    job_id: str,
    pipeline_id: str,
    work: Mapping[str, Any],
    plan_rel: str,
    native_slice: NativeSlicePacket,
    forbidden_paths: Sequence[str],
    source_identity: SourceIdentity,
    evidence_identity: Mapping[str, Any],
    prerequisite_evidence: Mapping[str, Any],
    requested_by: str,
    dependency_source_sha256: str,
    refill_attempt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    lane_id = _safe_id(native_slice.slice_id, fallback="lane")
    candidate_validation = build_ppe_validation_contract(
        pipeline_id=pipeline_id,
        job_id=job_id,
        work_item_id=str(work.get("work_item_id") or ""),
        native_slice_id=native_slice.slice_id,
        source_commit=source_identity.commit,
        allowed_changed_paths=native_slice.touch_set,
        target_repository="DanielTabakman/Probability-prediction-engine",
        dependency_source_sha256=dependency_source_sha256,
    )
    return {
        "version": 1,
        "job_id": job_id,
        "approved": True,
        "publication_enabled": False,
        "requested_by": requested_by,
        "expected_source_head": source_identity.commit,
        "founder_build_next": {
            "version": 1,
            "pipeline_id": pipeline_id,
            "work_item_id": work.get("work_item_id"),
            "repository": "DanielTabakman/Probability-prediction-engine",
            "registered_adapter": "ppe_operator",
            "source": asdict(source_identity),
            "phase_plan": plan_rel,
            "native_slice": {
                "slice_id": native_slice.slice_id,
                "build_branch": native_slice.build_branch,
                "layer_preset": native_slice.layer_preset,
                "worker_mode": native_slice.worker_mode,
                "declared_plane": native_slice.declared_plane,
                "touch_set": list(native_slice.touch_set),
                "sequence_index": native_slice.sequence_index,
                "total_slices": native_slice.total_slices,
                "previous_slices": list(native_slice.previous_slices),
                "following_slices": list(native_slice.following_slices),
            },
            "prerequisites": dict(prerequisite_evidence),
            "portfolio_selection_evidence": dict(evidence_identity),
            **({"refill_attempt": dict(refill_attempt)} if refill_attempt is not None else {}),
            "authority": {
                "publication_enabled": False,
                "merge_enabled": False,
                "product_main_write_enabled": False,
            },
        },
        "candidate_validation": candidate_validation,
        "manifest": {
            "version": 1,
            "publication_enabled": False,
            "lanes": [
                {
                    "task_id": lane_id,
                    "lane_id": lane_id,
                    "chapter_id": _safe_id(str(work.get("work_item_id") or lane_id)).upper(),
                    "branch": native_slice.build_branch,
                    "layer": native_slice.layer_preset,
                    "worker_mode": native_slice.worker_mode,
                    "preferred_cost_class": "standard",
                    "allowed_paths": list(native_slice.touch_set),
                    "forbidden_paths": list(forbidden_paths),
                    "allow_changes": True,
                    "instruction": _instruction(
                        pipeline_id=pipeline_id,
                        work=work,
                        plan_rel=plan_rel,
                        native_slice=native_slice,
                        forbidden_paths=forbidden_paths,
                        source_identity=source_identity,
                        evidence_identity=evidence_identity,
                        prerequisite_evidence=prerequisite_evidence,
                        refill_attempt=refill_attempt,
                    ),
                }
            ],
        },
    }


def _job_state(config: BuildNextConfig, job_id: str) -> str | None:
    if config.host_root is None:
        return None
    paths = HostPaths.from_root(config.host_root)
    filename = f"{job_id}.yaml"
    if (paths.running / filename).exists():
        return "RUNNING"
    if (paths.pending / filename).exists():
        return "QUEUED"
    if (paths.completed / job_id).exists() or (paths.failed / job_id).exists():
        return "BLOCKED"
    if paths.status_file.exists():
        try:
            status = json.loads(paths.status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if status.get("active_job_id") == job_id and status.get("state") == "running":
            return "RUNNING"
    return None


def _prepare_feed_checkout(config: BuildNextConfig) -> Path:
    root = (
        config.checkout_root
        or Path(tempfile.gettempdir()) / "msos-autobuilder-build-next-feed"
    ).expanduser().resolve()
    if not (root / ".git").exists():
        if root.exists():
            shutil.rmtree(root)
        root.parent.mkdir(parents=True, exist_ok=True)
        _git(
            None,
            "-c",
            "core.autocrlf=false",
            "clone",
            "--no-tags",
            config.feed_repo_url,
            str(root),
        )
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "user.name", "MSOS Autobuilder Build Next")
    _git(root, "config", "user.email", "autobuilder-build-next@localhost")
    _git(root, "fetch", "--no-tags", "origin", config.jobs_branch, accepted=(0, 128))
    checkout = _run(
        [
            "git",
            "-C",
            str(root),
            "checkout",
            "-B",
            config.jobs_branch,
            f"origin/{config.jobs_branch}",
        ],
        accepted=(0, 128),
    )
    if checkout.returncode != 0:
        _git(root, "checkout", "--orphan", config.jobs_branch)
    else:
        _git(root, "reset", "--hard", f"origin/{config.jobs_branch}")
    _git(root, "clean", "-fd")
    return root


@dataclass(frozen=True)
class FeedSubmission:
    feed_commit: str | None
    feed_path: str
    created: bool


def _submit_feed_job(config: BuildNextConfig, job: Mapping[str, Any]) -> FeedSubmission:
    job_id = str(job["job_id"])
    text = yaml.safe_dump(dict(job), sort_keys=False, allow_unicode=True)
    parse_host_job(text)
    if not config.submit:
        return FeedSubmission(None, f"{config.jobs_path}/{job_id}.yaml", False)
    lock_root = (
        config.checkout_root
        or Path(tempfile.gettempdir()) / "msos-autobuilder-build-next-feed"
    ).expanduser().resolve()
    with FeedMutationLock(lock_root.with_suffix(".lock")):
        checkout = _prepare_feed_checkout(config)
        relative = Path(config.jobs_path) / f"{job_id}.yaml"
        destination = checkout / relative
        if destination.exists():
            existing = destination.read_text(encoding="utf-8")
            parse_host_job(existing)
            if existing != text:
                raise BuildNextError(
                    f"approved job {job_id!r} already exists with different content"
                )
            existing_commit = _git(
                checkout,
                "log",
                "-n",
                "1",
                "--format=%H",
                "--",
                relative.as_posix(),
            )
            return FeedSubmission(existing_commit, relative.as_posix(), False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8", newline="\n")
        _git(checkout, "add", "--", relative.as_posix())
        changed = _run(
            ["git", "-C", str(checkout), "diff", "--cached", "--quiet"],
            accepted=(0, 1),
        ).returncode
        if changed == 0:
            existing_commit = _git(
                checkout,
                "log",
                "-n",
                "1",
                "--format=%H",
                "--",
                relative.as_posix(),
            )
            return FeedSubmission(existing_commit, relative.as_posix(), False)
        _git(checkout, "commit", "-m", f"Queue founder build next job {job_id}")
        commit = _git(checkout, "rev-parse", "HEAD")
        _git(checkout, "push", "origin", f"HEAD:{config.jobs_branch}")
        return FeedSubmission(commit, relative.as_posix(), True)


def _blocked_receipt(message: str, evidence: Mapping[str, Any] | None = None) -> BuildNextReceipt:
    return BuildNextReceipt(
        status="BLOCKED",
        pipeline_id=None,
        work_item_id=None,
        job_id=None,
        repository=None,
        source_commit=None,
        feed_path=None,
        feed_commit=None,
        message=message,
        evidence=evidence or {},
    )


def build_next(config: BuildNextConfig) -> BuildNextReceipt:
    ppe_repo = config.ppe_repo.expanduser().resolve()
    try:
        snapshot = _collect_snapshot(ppe_repo, config.exclude_work_item_ids)
        _validate_selection_context(snapshot, config.exclude_work_item_ids)
        _validate_snapshot(snapshot, config.max_snapshot_age_seconds)
        rec = snapshot.get("recommended_next_action")
        if not isinstance(rec, dict) or rec.get("state") == "UNFILLED":
            return BuildNextReceipt(
                status="UNFILLED",
                pipeline_id=None,
                work_item_id=None,
                job_id=None,
                repository=None,
                source_commit=None,
                feed_path=None,
                feed_commit=None,
                message="No safe READY_TO_BUILD work item was selected by PPE.",
                evidence={"snapshot_as_of": snapshot.get("as_of")},
                submitted=False,
            )
        if rec.get("state") != "READY_TO_BUILD" or rec.get("action_type") != "build":
            return BuildNextReceipt(
                status="UNFILLED" if rec.get("state") == "UNFILLED" else "BLOCKED",
                pipeline_id=rec.get("pipeline_id"),
                work_item_id=rec.get("work_item_id"),
                job_id=None,
                repository=None,
                source_commit=None,
                feed_path=None,
                feed_commit=None,
                message=f"PPE selected non-dispatchable state {rec.get('state')!r}.",
                evidence={"recommended_next_action": rec},
                submitted=False,
            )

        pipeline_id = str(rec.get("pipeline_id") or "")
        work_item_id = str(rec.get("work_item_id") or "")
        if pipeline_id != "ppe":
            raise BuildNextError("v1 can dispatch only PPE/MSOS product jobs")
        refill_attempt = (
            config.refill_attempt.evidence(work_item_id)
            if config.refill_attempt is not None
            else None
        )
        if (
            refill_attempt is not None
            and refill_attempt["selected_work_item_id"] != _normalize_work_item_id(work_item_id)
        ):
            raise BuildNextError("refill attempt selected work-item identity does not match PPE")
        registry = _load_json(
            ppe_repo / "config" / "founder_pipeline_registry.json",
            "PPE registry",
        )
        registry_pipe = _pipeline(registry, pipeline_id)
        _validate_registry_adapter(registry_pipe)
        pipe = _snapshot_pipeline(snapshot, pipeline_id)
        _validate_pipeline_runtime(pipe)
        work = _ready_work(pipe, work_item_id)
        if work.get("evidence") not in {"manual", "canonical", "native_runtime"}:
            raise BuildNextError("selected work item lacks accepted evidence")
        trace = str(work.get("trace") or rec.get("trace") or "")
        plan, plan_rel, plan_raw = _plan_text(ppe_repo, trace)
        native_slice = _select_native_slice(plan)
        prerequisite_evidence = _validate_native_prerequisites(work, plan, native_slice)
        forbidden_paths = FORBIDDEN_AUTHORITY_PATHS
        source_identity = _source_identity(config, ppe_repo)
        evidence_identity = _evidence_identity(
            ppe_repo,
            source_identity=source_identity,
            snapshot=snapshot,
            selected={"pipeline_id": pipeline_id, "work_item_id": work_item_id, "trace": plan_rel},
            plan_rel=plan_rel,
            native_slice=native_slice,
            prerequisite_evidence=prerequisite_evidence,
            refill_attempt=refill_attempt,
            requested_exclusions=config.exclude_work_item_ids,
        )
        job_id = _job_id(
            pipeline_id,
            work_item_id,
            native_slice,
            source_identity.commit,
            refill_attempt=refill_attempt,
        )
        state = _job_state(config, job_id)
        if state in {"RUNNING", "QUEUED"}:
            return BuildNextReceipt(
                status=state,
                pipeline_id=pipeline_id,
                work_item_id=work_item_id,
                job_id=job_id,
                repository="DanielTabakman/Probability-prediction-engine",
                source_commit=source_identity.commit,
                feed_path=None,
                feed_commit=None,
                message=f"Job {job_id} is already {state.lower()}; no duplicate was submitted.",
                evidence=evidence_identity,
                submitted=False,
            )
        if state == "BLOCKED":
            raise BuildNextError(f"job {job_id} already completed or failed; refusing redispatch")
        requirements_path = ppe_repo / "requirements.txt"
        if not requirements_path.is_file():
            raise BuildNextError("PPE dependency source is missing: requirements.txt")
        job = _build_job(
            job_id=job_id,
            pipeline_id=pipeline_id,
            work=work,
            plan_rel=plan_rel,
            native_slice=native_slice,
            forbidden_paths=forbidden_paths,
            source_identity=source_identity,
            evidence_identity=evidence_identity,
            prerequisite_evidence=prerequisite_evidence,
            requested_by=config.requested_by,
            dependency_source_sha256=_sha256_file(requirements_path),
            refill_attempt=refill_attempt,
        )
        submission = _submit_feed_job(config, job)
        if not config.submit:
            return BuildNextReceipt(
                status="UNFILLED",
                pipeline_id=pipeline_id,
                work_item_id=work_item_id,
                job_id=job_id,
                repository="DanielTabakman/Probability-prediction-engine",
                source_commit=source_identity.commit,
                feed_path=submission.feed_path,
                feed_commit=None,
                message=(
                    "Dry run constructed one immutable approved build-next job; "
                    "no feed submission occurred."
                ),
                evidence=evidence_identity,
                submitted=False,
                projected_status="QUEUED",
            )
        return BuildNextReceipt(
            status="QUEUED",
            pipeline_id=pipeline_id,
            work_item_id=work_item_id,
            job_id=job_id,
            repository="DanielTabakman/Probability-prediction-engine",
            source_commit=source_identity.commit,
            feed_path=submission.feed_path,
            feed_commit=submission.feed_commit,
            message=(
                "Submitted one immutable approved build-next job."
                if submission.created
                else (
                    "Identical immutable approved build-next job already exists; "
                    "no duplicate was submitted."
                )
            ),
            evidence=evidence_identity,
            submitted=submission.created,
        )
    except BuildNextError as exc:
        return _blocked_receipt(str(exc))


def render_receipt_json(receipt: BuildNextReceipt) -> str:
    return json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n"
