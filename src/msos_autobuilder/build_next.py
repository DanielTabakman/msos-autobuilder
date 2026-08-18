"""One-shot founder ``build next`` dispatcher.

The dispatcher selects one Autobuilder-owned immutable approved job packet and
submits it to the existing Autobuilder feed. Target repositories are packet data,
fetched only after admission at the exact frozen commit.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .codex_shadow import load_codex_host_config
from .job_packet import (
    DEFAULT_CATALOG_RELPATH,
    AdmittedTargetIdentity,
    ApprovedJobPacket,
    JobPacketError,
    assert_identity_not_redirected,
    fetch_declared_target,
    freeze_admitted_identity,
    load_packet_dir,
    prove_declared_commit_fetchable,
    select_next_packet,
)
from .lifecycle_evidence import (
    SourceRef,
    attempt_identity,
    emit_lifecycle_evidence,
    record_producer_evidence_error,
)
from .managed_source import (
    SourceIdentity,
    normalize_github_repository,
)
from .persistent_host import HostPaths, load_persistent_host_config, parse_host_job
from .validation_contract import (
    build_ppe_validation_contract,
)
from .work_admission import (
    AdmissionRequest,
    AdmissionStatus,
    WorkCandidate,
    admit_work,
    candidate_from_pr,
    objective_identity_from_work,
    release_claim,
)


class BuildNextError(RuntimeError):
    """Raised when build-next validation or feed submission fails closed."""


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
    feed_repo_url: str
    ppe_repo: Path | None = None
    packet_root: Path | None = None
    catalog_path: str = DEFAULT_CATALOG_RELPATH
    jobs_branch: str = "jobs"
    jobs_path: str = "jobs/approved"
    checkout_root: Path | None = None
    host_root: Path | None = None
    max_snapshot_age_seconds: int = 600
    requested_by: str = "founder build next"
    submit: bool = True
    source_remote: str = "origin"
    source_ref: str = "HEAD"
    expected_source_repository: str = ""
    allow_test_local_source_remote: bool = False
    exclude_work_item_ids: tuple[str, ...] = ()
    refill_attempt: RefillAttemptContext | None = None
    expected_source_commit: str | None = None
    target_checkout_root: Path | None = None

    def __post_init__(self) -> None:
        if not self.feed_repo_url.strip():
            raise ValueError("feed_repo_url is required")
        if self.jobs_branch in {"main", "master"}:
            raise ValueError("jobs_branch must not be a product/default branch")
        rel = Path(self.jobs_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("jobs_path must be a safe relative path")
        catalog = Path(self.catalog_path)
        if catalog.is_absolute() or ".." in catalog.parts:
            raise ValueError("catalog_path must be a safe relative path")
        if self.max_snapshot_age_seconds < 1:
            raise ValueError("max_snapshot_age_seconds must be positive")
        if not self.source_remote.strip() or not self.source_ref.strip():
            raise ValueError("source remote/ref are required")

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
        packet_root: Path | None = None,
    ) -> BuildNextConfig:
        service = load_persistent_host_config(service_config)
        if service.feed is None:
            raise ValueError("persistent host service config does not enable a job feed")
        host_config = load_codex_host_config(service.codex_host_config)
        return cls(
            ppe_repo=host_config.source_repo,
            packet_root=packet_root,
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


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    accepted: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    if any("founder_portfolio.py" in str(part) for part in argv):
        raise BuildNextError("selector/refill path must not invoke founder_portfolio.py")
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


def _load_catalog(config: BuildNextConfig) -> tuple[ApprovedJobPacket, ...]:
    if config.packet_root is not None:
        return load_packet_dir(config.packet_root.expanduser().resolve())
    checkout = _prepare_feed_checkout(config)
    return load_packet_dir(checkout / config.catalog_path)


def _source_identity_from_packet(packet: ApprovedJobPacket) -> SourceIdentity:
    return SourceIdentity(
        remote="origin",
        remote_url=packet.target_remote_url,
        repository=packet.target_repository,
        ref="HEAD",
        remote_ref=packet.target_source_commit,
        commit=packet.target_source_commit,
    )


def _native_slice_from_packet(packet: ApprovedJobPacket) -> NativeSlicePacket:
    raw = dict(packet.native_slice)
    touch_set_raw = raw.get("touch_set") or raw.get("touchSet") or list(packet.allowed_paths)
    if not isinstance(touch_set_raw, list) or not touch_set_raw:
        raise BuildNextError("approved job packet lacks a writable touch set")
    touch_set = tuple(_validate_writable_path(str(item)) for item in touch_set_raw)
    if touch_set != packet.allowed_paths:
        raise BuildNextError(
            "approved job packet allowed_paths do not match native slice touch set"
        )
    if len(set(touch_set)) != len(touch_set):
        raise BuildNextError("approved job packet has duplicate touch paths")
    slice_id = str(raw.get("slice_id") or raw.get("sliceId") or "").strip()
    build_branch = str(raw.get("build_branch") or raw.get("buildBranch") or "").strip()
    layer_preset = str(raw.get("layer_preset") or raw.get("layerPreset") or "").strip()
    if not slice_id or not build_branch or not layer_preset:
        raise BuildNextError("approved job packet native slice is missing identity fields")
    previous = raw.get("previous_slices") or raw.get("previousSlices") or ()
    following = raw.get("following_slices") or raw.get("followingSlices") or ()
    return NativeSlicePacket(
        slice_id=slice_id,
        build_branch=build_branch,
        layer_preset=layer_preset,
        worker_mode=str(raw.get("worker_mode") or raw.get("workerMode") or "").strip() or None,
        declared_plane=str(raw.get("declared_plane") or raw.get("declaredPlane") or "").strip(),
        touch_set=touch_set,
        sequence_index=int(raw.get("sequence_index") or raw.get("sequenceIndex") or 0),
        total_slices=int(raw.get("total_slices") or raw.get("totalSlices") or 1),
        previous_slices=tuple(str(item) for item in previous),
        following_slices=tuple(str(item) for item in following),
        sprint_spec_path=(
            _safe_relative(
                raw.get("sprint_spec_path") or raw.get("sprintSpecPath"),
                "sprintSpecPath",
            )
            if (raw.get("sprint_spec_path") or raw.get("sprintSpecPath"))
            else None
        ),
        selection_record=(
            _safe_relative(
                raw.get("selection_record") or raw.get("selectionRecord"),
                "selectionRecord",
            )
            if (raw.get("selection_record") or raw.get("selectionRecord"))
            else None
        ),
        raw_slice=raw,
    )


def _target_checkout_root(config: BuildNextConfig, job_id: str) -> Path:
    root = config.target_checkout_root
    if root is None:
        default_feed = Path(tempfile.gettempdir()) / "msos-autobuilder-build-next-feed"
        base = config.checkout_root or default_feed
        root = Path(base) / "admitted-targets"
    return root.expanduser().resolve() / job_id


def _safe_id(value: str, *, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned[:96] or fallback


def _normalize_excluded_work_item_ids(values: Sequence[Any]) -> list[str]:
    return sorted({str(item).strip() for item in values if str(item).strip()})


def _safe_relative(value: Any, label: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise BuildNextError(f"{label} must be a safe relative path")
    return path.as_posix()


def _normalize_github_repository(url: str) -> str | None:
    return normalize_github_repository(url)


def _linked_issue_from_text(text: str) -> int | None:
    matches = re.findall(r"(?i)(?:fixes|closes|resolves|issue)\s+#(\d+)", text)
    if not matches:
        return None
    return int(matches[0])


class GitHubWorkDiscoveryClient:
    def __init__(self, repo_full_name: str, token: str) -> None:
        self.repo_full_name = repo_full_name
        self.token = token

    @classmethod
    def from_git_credential(cls, repo_full_name: str) -> GitHubWorkDiscoveryClient:
        credential = _run(
            ["git", "credential", "fill"],
            input_text="protocol=https\nhost=github.com\n\n",
        )
        values: dict[str, str] = {}
        for line in credential.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        token = values.get("password", "")
        if not token:
            raise BuildNextError("Git Credential Manager did not return a GitHub token")
        return cls(repo_full_name, token)

    def _request(self, path: str) -> Any:
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            method="GET",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "msos-autobuilder-build-next",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise BuildNextError(f"GitHub API GET {path} failed: {exc.code} {body}") from exc
        except urllib.error.URLError as exc:
            raise BuildNextError(f"GitHub API GET {path} failed: {exc}") from exc

    def _paged_list(self, path: str, label: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        separator = "&" if "?" in path else "?"
        for page in range(1, 11):
            result = self._request(f"{path}{separator}per_page=100&page={page}")
            if not isinstance(result, list):
                raise BuildNextError(f"GitHub {label} discovery returned invalid payload")
            items.extend(item for item in result if isinstance(item, dict))
            if len(result) < 100:
                return items
        raise BuildNextError(f"GitHub {label} discovery exceeded bounded page limit")

    def related_candidates(
        self,
        *,
        objective_sha256: str,
        acceptance_contract_sha256: str,
        changed_paths: Sequence[str],
    ) -> tuple[WorkCandidate, ...]:
        requested_paths = {path.replace("\\", "/").strip("/") for path in changed_paths}
        candidates: list[WorkCandidate] = []
        for issue in self._paged_list(f"/repos/{self.repo_full_name}/issues?state=all", "issue"):
            if "pull_request" in issue:
                continue
            body = str(issue.get("body") or "")
            title = str(issue.get("title") or "")
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            same_objective = objective_sha256 in body or objective_sha256 in title
            same_contract = (
                acceptance_contract_sha256 in body
                or acceptance_contract_sha256 in title
            )
            path_hits = tuple(sorted(path for path in requested_paths if path in body))
            if not (same_objective or same_contract or path_hits):
                continue
            candidates.append(
                WorkCandidate(
                    kind="issue",
                    number=number,
                    title=title,
                    state=str(issue.get("state") or ""),
                    branch=None,
                    linked_issue=number,
                    objective_sha256=objective_sha256 if same_objective else None,
                    acceptance_contract_sha256=(
                        acceptance_contract_sha256 if same_contract else None
                    ),
                    changed_paths=path_hits,
                    canonical=False,
                    merged=False,
                    url=str(issue.get("html_url") or ""),
                )
            )
        for pull in self._paged_list(f"/repos/{self.repo_full_name}/pulls?state=all", "PR"):
            number = pull.get("number")
            if not isinstance(number, int):
                continue
            file_items = self._paged_list(
                f"/repos/{self.repo_full_name}/pulls/{number}/files",
                "PR file",
            )
            pr_paths = tuple(
                sorted(
                    str(file.get("filename") or "").replace("\\", "/")
                    for file in file_items
                    if file.get("filename")
                )
            )
            body = str(pull.get("body") or "")
            same_objective = objective_sha256 in body
            same_contract = acceptance_contract_sha256 in body
            path_overlap = bool(requested_paths & set(pr_paths))
            if not (same_objective or same_contract or path_overlap):
                continue
            head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
            candidates.append(
                candidate_from_pr(
                    number=number,
                    title=str(pull.get("title") or ""),
                    state=str(pull.get("state") or ""),
                    branch=str(head.get("ref") or ""),
                    linked_issue=_linked_issue_from_text(body),
                    objective_sha256=objective_sha256 if same_objective else None,
                    acceptance_contract_sha256=(
                        acceptance_contract_sha256 if same_contract else None
                    ),
                    changed_paths=pr_paths,
                    canonical=bool(pull.get("merged_at")),
                    merged=bool(pull.get("merged_at")),
                    url=str(pull.get("html_url") or ""),
                )
            )
        return tuple(candidates)


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


def _evidence_identity(
    *,
    source_identity: SourceIdentity,
    selected: Mapping[str, Any],
    native_slice: NativeSlicePacket,
    prerequisite_evidence: Mapping[str, Any],
    packet: ApprovedJobPacket,
    admitted: AdmittedTargetIdentity,
    refill_attempt: Mapping[str, Any] | None = None,
    requested_exclusions: Sequence[str] = (),
) -> dict[str, Any]:
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
        "packet_sha256": packet.packet_sha256,
        "admitted_target": asdict(admitted),
        "requested_exclusions": _normalize_excluded_work_item_ids(requested_exclusions),
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
            "Implement only the bounded work item selected from Autobuilder-owned "
            "approved job packets.",
            f"Pipeline ID: {pipeline_id}",
            f"Work-item ID: {work.get('work_item_id')}",
            f"Source repository: {source_identity.repository}",
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
        return _refill_job_id(
            pipeline_id,
            work_item_id,
            native_slice,
            source_commit,
            refill_attempt,
        )
    return _safe_id(
        f"build-next-{pipeline_id}-{work_item_id}-{native_slice.slice_id}-{source_commit[:12]}"
    )


def _refill_job_id(
    pipeline_id: str,
    work_item_id: str,
    native_slice: NativeSlicePacket,
    source_commit: str,
    refill_attempt: Mapping[str, Any],
) -> str:
    digest_payload = {
        "pipeline_id": pipeline_id,
        "work_item_id": work_item_id,
        "selected_work_item_id": str(refill_attempt.get("selected_work_item_id") or ""),
        "slice_id": native_slice.slice_id,
        "source_commit": source_commit,
        "generation_id": str(refill_attempt.get("generation_id") or ""),
        "attempt_ordinal": refill_attempt.get("attempt_ordinal"),
        "retry_ordinal": refill_attempt.get("retry_ordinal"),
    }
    digest = _sha256_text(json.dumps(digest_payload, sort_keys=True, separators=(",", ":")))[:16]
    safe_prefix = _safe_id(
        f"build-next-{pipeline_id}-{work_item_id}-{native_slice.slice_id}-"
        f"{source_commit[:12]}-g{str(refill_attempt.get('generation_id'))[:12]}-"
        f"a{refill_attempt.get('attempt_ordinal')}-r{refill_attempt.get('retry_ordinal')}"
    )
    suffix = f"-{digest}"
    prefix_budget = 96 - len(suffix)
    return f"{safe_prefix[:prefix_budget].rstrip('-')}{suffix}"


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
    work_item_source_sha256: str,
    target_repository: str,
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
        target_repository=target_repository,
        dependency_source_sha256=dependency_source_sha256,
        adapter=str(work.get("adapter") or "ppe_operator"),
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
            "repository": target_repository,
            "registered_adapter": str(work.get("adapter") or "ppe_operator"),
            "source": asdict(source_identity),
            "work_item_source_sha256_v1": work_item_source_sha256,
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
    source_path: Path | None = None
    source_sha256: str | None = None
    recorded_at: str | None = None


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
            return FeedSubmission(
                existing_commit,
                relative.as_posix(),
                False,
                destination,
                _sha256_file(destination),
                _git(checkout, "log", "-n", "1", "--format=%cI", "--", relative.as_posix()),
            )
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
            return FeedSubmission(
                existing_commit,
                relative.as_posix(),
                False,
                destination,
                _sha256_file(destination),
                _git(checkout, "log", "-n", "1", "--format=%cI", "--", relative.as_posix()),
            )
        _git(checkout, "commit", "-m", f"Queue founder build next job {job_id}")
        commit = _git(checkout, "rev-parse", "HEAD")
        recorded_at = _git(checkout, "show", "-s", "--format=%cI", commit)
        _git(checkout, "push", "origin", f"HEAD:{config.jobs_branch}")
        return FeedSubmission(
            commit,
            relative.as_posix(),
            True,
            destination,
            _sha256_file(destination),
            recorded_at,
        )


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
    try:
        packets = _load_catalog(config)
        packet = select_next_packet(
            packets,
            exclude_work_item_ids=_normalize_excluded_work_item_ids(config.exclude_work_item_ids),
        )
        if packet is None:
            return BuildNextReceipt(
                status="UNFILLED",
                pipeline_id=None,
                work_item_id=None,
                job_id=None,
                repository=None,
                source_commit=None,
                feed_path=None,
                feed_commit=None,
                message="No safe Autobuilder-owned approved job packet was selected.",
                evidence={
                    "catalog_count": len(packets),
                    "requested_exclusions": _normalize_excluded_work_item_ids(
                        config.exclude_work_item_ids
                    ),
                },
                submitted=False,
            )
        admitted = freeze_admitted_identity(packet)
        assert_identity_not_redirected(admitted, packet)
        source_identity = _source_identity_from_packet(packet)
        pipeline_id = admitted.pipeline_id
        work_item_id = admitted.work_item_id
        refill_attempt = (
            config.refill_attempt.evidence(work_item_id)
            if config.refill_attempt is not None
            else None
        )
        if (
            refill_attempt is not None
            and refill_attempt["selected_work_item_id"] not in {"", work_item_id}
            and refill_attempt["selected_work_item_id"] != work_item_id
        ):
            raise BuildNextError(
                "refill attempt selected work-item identity does not match approved packet"
            )
        native_slice = _native_slice_from_packet(packet)
        work = {
            "work_item_id": work_item_id,
            "adapter": admitted.adapter,
            "native_prerequisites": dict(packet.prerequisites),
            "evidence": "canonical",
        }
        plan = {
            "slices": [
                *[{"sliceId": item} for item in native_slice.previous_slices],
                dict(native_slice.raw_slice),
                *[{"sliceId": item} for item in native_slice.following_slices],
            ]
        }
        prerequisite_evidence = _validate_native_prerequisites(work, plan, native_slice)
        forbidden_paths = FORBIDDEN_AUTHORITY_PATHS
        plan_rel = packet.phase_plan or "jobs/catalog"
        evidence_identity = _evidence_identity(
            source_identity=source_identity,
            selected={
                "pipeline_id": pipeline_id,
                "work_item_id": work_item_id,
                "trace": plan_rel,
                "packet_sha256": packet.packet_sha256,
            },
            native_slice=native_slice,
            prerequisite_evidence=prerequisite_evidence,
            packet=packet,
            admitted=admitted,
            refill_attempt=refill_attempt,
            requested_exclusions=config.exclude_work_item_ids,
        )
        receipt_evidence_identity = {
            **evidence_identity,
            "source": asdict(source_identity),
            "work_item_source_sha256_v1": packet.work_item_source_sha256_v1,
            "selection_explanation": {
                "rank_tuple": [packet.order, packet.pipeline_id, packet.work_item_id],
            },
            "requested_exclusions": _normalize_excluded_work_item_ids(
                config.exclude_work_item_ids
            ),
        }
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
                repository=admitted.target_repository,
                source_commit=source_identity.commit,
                feed_path=None,
                feed_commit=None,
                message=f"Job {job_id} is already {state.lower()}; no duplicate was submitted.",
                evidence=receipt_evidence_identity,
                submitted=False,
            )
        if state == "BLOCKED":
            raise BuildNextError(f"job {job_id} already completed or failed; refusing redispatch")
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
            dependency_source_sha256=admitted.dependency_source_sha256,
            work_item_source_sha256=packet.work_item_source_sha256_v1,
            target_repository=admitted.target_repository,
            refill_attempt=refill_attempt,
        )
        if (
            job["founder_build_next"]["repository"] != admitted.target_repository
            or job["expected_source_head"] != admitted.target_source_commit
            or job["candidate_validation"]["target_repository"] != admitted.target_repository
            or job["candidate_validation"]["source_commit"] != admitted.target_source_commit
        ):
            raise BuildNextError(
                "target identity cannot redirect after immutable admission identity is established"
            )
        admission_capability_contract = {
            "version": 1,
            "pipeline_id": pipeline_id,
            "work_item_id": work_item_id,
            "adapter": admitted.adapter,
            "target_repository": admitted.target_repository,
            "native_slice_id": native_slice.slice_id,
            "authorized_paths": list(native_slice.touch_set),
            "forbidden_paths": list(forbidden_paths),
            "publication_enabled": False,
            "merge_enabled": False,
            "product_main_write_enabled": False,
            "target_source_commit": admitted.target_source_commit,
            "packet_sha256": admitted.packet_sha256,
            "validation_identity": admitted.validation_identity,
        }
        admission_acceptance_sha256 = _sha256_text(
            json.dumps(
                admission_capability_contract,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        admission_objective = objective_identity_from_work(
            repository=admitted.target_repository,
            linked_issue=None,
            work_item_id=work_item_id,
            stable_parts={
                "pipeline_id": pipeline_id,
                "work_item_id": work_item_id,
                "acceptance_contract_sha256": admission_acceptance_sha256,
                "canonical_capability_path_contract": admission_capability_contract,
            },
            acceptance_contract_sha256=admission_acceptance_sha256,
        )
        related_candidates: tuple[WorkCandidate, ...] = ()
        if config.submit and not config.allow_test_local_source_remote:
            related_candidates = GitHubWorkDiscoveryClient.from_git_credential(
                admitted.target_repository
            ).related_candidates(
                objective_sha256=admission_objective.objective_sha256,
                acceptance_contract_sha256=admission_acceptance_sha256,
                changed_paths=native_slice.touch_set,
            )
        admission = admit_work(
            AdmissionRequest(
                objective=admission_objective,
                writer_id=f"build-next:{job_id}",
                branch=native_slice.build_branch,
                authorized_paths=native_slice.touch_set,
                claim_root=(
                    config.host_root / "state"
                    if config.host_root is not None and config.submit
                    else None
                ),
                candidates=related_candidates,
                evidence={
                    "admission_phase": "build_next.pre_feed_submission",
                    "job_id": job_id,
                    "pipeline_id": pipeline_id,
                    "work_item_id": work_item_id,
                    "source_commit": source_identity.commit,
                    "target_repository": admitted.target_repository,
                    "github_duplicate_discovery": {
                        "repository": admitted.target_repository,
                        "searched_issues": bool(related_candidates)
                        or (
                            config.submit
                            and not config.allow_test_local_source_remote
                        ),
                        "searched_pull_requests": bool(related_candidates)
                        or (
                            config.submit
                            and not config.allow_test_local_source_remote
                        ),
                        "candidate_count": len(related_candidates),
                    },
                    "claim_lifecycle": (
                        "active until verified merge, explicit supersession, "
                        "accepted abandonment, or bounded failure disposition"
                    ),
                },
            )
        )
        receipt_evidence_identity = {
            **receipt_evidence_identity,
            "work_admission": asdict(admission),
        }
        if admission.status != AdmissionStatus.NEW_WORK_ADMITTED:
            return BuildNextReceipt(
                status=admission.status.value,
                pipeline_id=pipeline_id,
                work_item_id=work_item_id,
                job_id=job_id,
                repository=admitted.target_repository,
                source_commit=source_identity.commit,
                feed_path=None,
                feed_commit=None,
                message=admission.message,
                evidence=receipt_evidence_identity,
                submitted=False,
            )
        fetched_commit = prove_declared_commit_fetchable(
            target_repository=admitted.target_repository,
            target_source_commit=admitted.target_source_commit,
            remote_url=packet.target_remote_url,
        )
        if config.target_checkout_root is not None:
            fetched_commit = fetch_declared_target(
                target_repository=admitted.target_repository,
                target_source_commit=admitted.target_source_commit,
                destination=_target_checkout_root(config, job_id),
                remote_url=packet.target_remote_url,
            )
        if fetched_commit != admitted.target_source_commit:
            raise BuildNextError(
                "target identity cannot redirect after immutable admission identity is established"
            )
        job["founder_build_next"]["work_admission"] = {
            "status": admission.status.value,
            "objective_sha256": admission.objective_sha256,
            "claim_generation": (
                admission.claim.generation if admission.claim is not None else None
            ),
            "claim_writer_id": (
                admission.claim.writer_id if admission.claim is not None else None
            ),
            "authorized_paths": list(native_slice.touch_set),
            "objective_identity": asdict(admission_objective),
            "acceptance_contract": admission_capability_contract,
            "execution_validation_contract_sha256": job["candidate_validation"][
                "contract_sha256"
            ],
            "admitted_target": asdict(admitted),
        }
        try:
            submission = _submit_feed_job(config, job)
        except Exception as exc:
            if config.host_root is not None and admission.claim is not None:
                release_claim(
                    config.host_root / "state",
                    admission.objective_sha256,
                    writer_id=admission.claim.writer_id,
                    terminal_state="failed",
                    expected_generation=admission.claim.generation,
                    evidence={
                        "bounded_failure_disposition": "feed_submission_failed",
                        "job_id": job_id,
                        "error": str(exc),
                    },
                )
            raise
        if not config.submit:
            return BuildNextReceipt(
                status="UNFILLED",
                pipeline_id=pipeline_id,
                work_item_id=work_item_id,
                job_id=job_id,
                repository=admitted.target_repository,
                source_commit=source_identity.commit,
                feed_path=submission.feed_path,
                feed_commit=None,
                message=(
                    "Dry run constructed one immutable approved build-next job; "
                    "no feed submission occurred."
                ),
                evidence=receipt_evidence_identity,
                submitted=False,
                projected_status="QUEUED",
            )
        if (
            config.host_root is not None
            and refill_attempt is not None
            and submission.source_sha256 is not None
            and submission.recorded_at is not None
        ):
            identity = None
            try:
                identity = attempt_identity(
                    pipeline_id=pipeline_id,
                    work_item_id=work_item_id,
                    work_item_digest=packet.work_item_source_sha256_v1,
                    generation_id=str(refill_attempt["generation_id"]),
                    job_id=job_id,
                    attempt_ordinal=int(refill_attempt["attempt_ordinal"]),
                    retry_ordinal=int(refill_attempt.get("retry_ordinal") or 0),
                )
                emit_lifecycle_evidence(
                    config.host_root,
                    evidence_kind="dispatch.submitted",
                    identity=identity,
                    source_ref=SourceRef(
                        repository=config.feed_repo_url,
                        ref=config.jobs_branch,
                        commit=str(submission.feed_commit or ""),
                        path=submission.feed_path,
                        sha256=submission.source_sha256,
                    ),
                    payload={
                        "feed_commit": submission.feed_commit,
                        "feed_path": submission.feed_path,
                        "submitted_job_sha256": submission.source_sha256,
                    },
                    final=True,
                    closed_status="final",
                    observed_at=submission.recorded_at,
                )
            except Exception as exc:
                record_producer_evidence_error(
                    config.host_root,
                    producer="build_next",
                    evidence_kind="dispatch.submitted",
                    error=exc,
                    identity=identity,
                    primary_outcome={
                        "status": "QUEUED",
                        "feed_commit": submission.feed_commit,
                        "feed_path": submission.feed_path,
                    },
                )
        return BuildNextReceipt(
            status="QUEUED",
            pipeline_id=pipeline_id,
            work_item_id=work_item_id,
            job_id=job_id,
            repository=admitted.target_repository,
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
            evidence=receipt_evidence_identity,
            submitted=submission.created,
        )
    except (BuildNextError, JobPacketError) as exc:
        return _blocked_receipt(str(exc))


def render_receipt_json(receipt: BuildNextReceipt) -> str:
    return json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n"
