"""Just-in-time approved-backlog → jobs/catalog packet materializer.

Stage 5: after a durable predecessor terminal signal, author the next eligible
Autobuilder catalog packet from PPE ``PHASE_CHAPTER_BACKLOG.json`` and publish
exactly one catalog file to the jobs branch when configured.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .build_next import (
    BuildNextError,
    FeedMutationLock,
    _prepare_feed_checkout,
    _select_native_slice,
)
from .job_packet import (
    DEFAULT_CATALOG_RELPATH,
    JobPacketError,
    load_packet_dir,
    parse_approved_job_packet,
    prove_declared_commit_fetchable,
    select_next_packet,
)
from .managed_source import normalize_github_repository
from .validation_contract import canonical_dependency_source_sha256
from .windows_git_checkout import git_environment


class CatalogMaterializerError(RuntimeError):
    """Raised when JIT catalog materialization must fail closed."""


BACKLOG_RELPATH = "docs/SOP/PHASE_CHAPTER_BACKLOG.json"
PREDECESSOR_WORK_ITEM_ID = "region_bet_contract_v1"
GUIDED_SHELL_WORK_ITEM_ID = "region_bet_guided_shell_v1"
GUIDED_SHELL_ORDER = 7
GUIDED_SHELL_CATALOG_FILENAME = "07-region_bet_guided_shell_v1.json"
MARKET_COMPARE_WORK_ITEM_ID = "region_bet_market_compare_bridge_v1"
MARKET_COMPARE_ORDER = 8
MARKET_COMPARE_RELATED_PR = "DanielTabakman/Probability-prediction-engine#5427"
RISK_EXPRESSION_ORDER = 9
PACKET_SPEC_KEY = "autobuilderPacket"

DEFAULT_TARGET_REPOSITORY = "DanielTabakman/Probability-prediction-engine"
DEFAULT_TARGET_REMOTE_URL = "https://github.com/DanielTabakman/Probability-prediction-engine.git"
DEFAULT_ADAPTER = "ppe_operator"
DEFAULT_VALIDATION = {"profile_id": "ppe-ci-pytest-v1"}
DEFAULT_AUTHORITY = {
    "publication_enabled": False,
    "merge_enabled": False,
    "product_main_write_enabled": False,
}

# Guided shell reuses Region Bet / workflow / horizon primitives only.
# Do not pull market-compare (#5427) or risk-expression (#5428) into 07.
GUIDED_SHELL_ALLOWED_PATHS: tuple[str, ...] = (
    "apps/msos-web/src/components/WorkflowStepper.tsx",
    "apps/msos-web/src/lib/regionBet.ts",
    "apps/msos-web/src/lib/msosWorkflowStore.ts",
    "apps/msos-web/src/lib/horizonRegion.ts",
    "tests/test_msos_web_region_bet_guided_shell.py",
)

MERGED_COMPLETION_STATUSES = frozenset({"merged"})
MERGED_LIFECYCLE_DISPOSITIONS = frozenset({"item_terminal_success_merged"})
RESOLVED_RELATED_PR_DISPOSITIONS = frozenset(
    {"accepted", "merged", "superseded", "superseded_by_backlog_item"}
)
DEFERRED_ITEM_STATUSES = frozenset({"deferred", "skipped"})
SUPPORTED_JIT_ELIGIBILITY = frozenset(
    {
        "blocked_until_predecessor_terminal",
        "blocked_until_predecessor_and_draft_decision",
        "buildable_via_autobuilder_catalog",
        "ready",
    }
)
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_FILENAME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
_CATALOG_WRITE_LOCK = threading.Lock()
_GIT_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class BacklogItem:
    chapter_id: str
    order: int
    depends_on: tuple[str, ...]
    packetization: str
    eligibility: str
    status: str
    related_pull_requests: tuple[str, ...]
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class JitEligibility:
    status: str
    reason: str
    work_item_id: str | None = None
    order: int | None = None
    evidence: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class MaterializeResult:
    status: str
    reason: str = ""
    work_item_id: str | None = None
    order: int | None = None
    packet_path: str | None = None
    packet_sha256: str | None = None
    frozen_commit: str | None = None
    feed_commit: str | None = None
    evidence: Mapping[str, Any] | None = None


def _git(repo: Path | None, *args: str, accepted: tuple[int, ...] = (0,)) -> str:
    argv = ["git"]
    if repo is not None:
        argv.extend(["-C", str(repo)])
    argv.extend(args)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
            env=git_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CatalogMaterializerError(f"Git command did not complete: {argv}") from exc
    if proc.returncode not in accepted:
        detail = (proc.stderr or proc.stdout or "command failed").strip()
        raise CatalogMaterializerError(f"{' '.join(argv)}: {detail}")
    return proc.stdout.strip()


def _read_json_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogMaterializerError(f"invalid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise CatalogMaterializerError(f"JSON object required: {path}")
    return raw


def _packet_bytes(raw: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(raw), indent=2, sort_keys=False) + "\n").encode("utf-8")


def _packets_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, sort_keys=True, separators=(",", ":")
    )


def load_phase_chapter_backlog(path: Path | str | Mapping[str, Any]) -> tuple[BacklogItem, ...]:
    if isinstance(path, Mapping):
        payload = dict(path)
    else:
        payload = _read_json_mapping(Path(path))
    items_raw = payload.get("items")
    if not isinstance(items_raw, list):
        raise CatalogMaterializerError("PHASE_CHAPTER_BACKLOG.json items must be a list")
    items: list[BacklogItem] = []
    seen_orders: set[int] = set()
    seen_chapters: set[str] = set()
    for entry in items_raw:
        if not isinstance(entry, dict):
            continue
        if "autobuilderCatalogOrder" not in entry:
            continue
        order = entry.get("autobuilderCatalogOrder")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise CatalogMaterializerError("autobuilderCatalogOrder must be a positive integer")
        chapter_id = str(entry.get("chapterId") or "").strip()
        if not chapter_id:
            raise CatalogMaterializerError(
                f"Autobuilder catalog order {order} is missing chapterId"
            )
        if order in seen_orders:
            raise CatalogMaterializerError(f"duplicate Autobuilder catalog order: {order}")
        if chapter_id in seen_chapters:
            raise CatalogMaterializerError(f"duplicate Autobuilder chapterId: {chapter_id}")
        depends_on = entry.get("dependsOn") or []
        related_pull_requests = entry.get("relatedPullRequests") or []
        if not isinstance(depends_on, list) or not all(
            isinstance(value, str) and value.strip() for value in depends_on
        ):
            raise CatalogMaterializerError(
                f"dependsOn for {chapter_id} must be a list of non-empty strings"
            )
        if len(set(depends_on)) != len(depends_on):
            raise CatalogMaterializerError(f"dependsOn for {chapter_id} contains duplicates")
        if not isinstance(related_pull_requests, list) or not all(
            isinstance(value, str) and value.strip() for value in related_pull_requests
        ):
            raise CatalogMaterializerError(
                f"relatedPullRequests for {chapter_id} must be a list of non-empty strings"
            )
        if len(set(related_pull_requests)) != len(related_pull_requests):
            raise CatalogMaterializerError(
                f"relatedPullRequests for {chapter_id} contains duplicates"
            )
        seen_orders.add(order)
        seen_chapters.add(chapter_id)
        items.append(
            BacklogItem(
                chapter_id=chapter_id,
                order=order,
                depends_on=tuple(value.strip() for value in depends_on),
                packetization=str(entry.get("packetization") or "").strip(),
                eligibility=str(entry.get("eligibility") or "").strip(),
                status=str(entry.get("status") or "").strip(),
                related_pull_requests=tuple(value.strip() for value in related_pull_requests),
                raw=dict(entry),
            )
        )
    items.sort(key=lambda item: (item.order, item.chapter_id))
    return tuple(items)


def _proof_is_merged_terminal(proof: Any) -> bool:
    if proof is True:
        return True
    if proof is False or proof in (None, ""):
        return False
    if isinstance(proof, Mapping):
        status = str(proof.get("status") or "").strip().lower()
        if status in MERGED_COMPLETION_STATUSES:
            return True
        disposition = str(proof.get("item_disposition") or "").strip()
        if proof.get("item_terminal") is True and disposition in MERGED_LIFECYCLE_DISPOSITIONS:
            return True
        # Explicit non-merged statuses (queued/drafted/open) fail closed.
        if status and status not in MERGED_COMPLETION_STATUSES:
            return False
    return False


def _proof_is_merged_for_work_item(proof: Any, work_item_id: str) -> bool:
    """Require any declared proof identity to agree with the lookup key."""
    if isinstance(proof, Mapping):
        declared: set[str] = set()
        for key in ("work_item_id", "workItemId"):
            value = str(proof.get(key) or "").strip()
            if value:
                declared.add(value)
        identity = proof.get("attempt_identity")
        if isinstance(identity, Mapping):
            value = str(identity.get("work_item_id") or "").strip()
            if value:
                declared.add(value)
        if declared and declared != {work_item_id}:
            return False
    return _proof_is_merged_terminal(proof)


def is_predecessor_terminal_proof(proof: Any) -> bool:
    """Durable terminal only: completion merged / lifecycle merged-terminal.

    Queue emptiness and non-merged statuses never count.
    """
    return _proof_is_merged_terminal(proof)


def order_is_draft_gated_blocked(
    backlog: Sequence[BacklogItem] | Mapping[str, Any] | Path | str,
    order: int,
    *,
    related_pr_resolved: Mapping[str, bool] | None = None,
) -> bool:
    if isinstance(backlog, (str, Path, Mapping)):
        items = load_phase_chapter_backlog(backlog)
    else:
        items = tuple(backlog)
    item = next((entry for entry in items if entry.order == order), None)
    if item is None:
        return True
    if item.eligibility != "blocked_until_predecessor_and_draft_decision":
        return False
    if not item.related_pull_requests:
        return True
    resolved_map = related_pr_resolved or {}
    declared: dict[str, str] = {}
    raw_resolutions = item.raw.get("relatedPullRequestResolutions")
    if isinstance(raw_resolutions, Mapping):
        for ref, value in raw_resolutions.items():
            if isinstance(value, Mapping):
                declared[str(ref).strip()] = str(
                    value.get("disposition") or value.get("status") or ""
                ).strip()
            else:
                declared[str(ref).strip()] = str(value or "").strip()
    elif isinstance(raw_resolutions, list):
        for value in raw_resolutions:
            if not isinstance(value, Mapping):
                continue
            ref = str(value.get("ref") or value.get("pullRequest") or "").strip()
            if ref:
                declared[ref] = str(value.get("disposition") or value.get("status") or "").strip()
    return not all(
        resolved_map.get(ref) is True
        or declared.get(ref, "").lower() in RESOLVED_RELATED_PR_DISPOSITIONS
        for ref in item.related_pull_requests
    )


def _has_packet_spec(item: BacklogItem) -> bool:
    """Return whether PPE supplied a bounded executable packet contract.

    Order 07 retains its original narrow adapter as a compatibility bridge. All
    later chapters must declare their own paths and acceptance contract in PPE.
    """
    if str(item.raw.get("planPath") or "").strip():
        return True
    if isinstance(item.raw.get(PACKET_SPEC_KEY), Mapping):
        return True
    return item.chapter_id == GUIDED_SHELL_WORK_ITEM_ID


def _terminal_proofs(
    predecessor_terminal: Any,
    terminal_proofs: Mapping[str, Any] | None,
) -> dict[str, Any]:
    proofs = dict(terminal_proofs or {})
    if predecessor_terminal not in (None, False, ""):
        proofs.setdefault(PREDECESSOR_WORK_ITEM_ID, predecessor_terminal)
    return proofs


def evaluate_jit_eligibility(
    backlog: Sequence[BacklogItem] | Mapping[str, Any] | Path | str,
    *,
    predecessor_terminal: Any = False,
    terminal_proofs: Mapping[str, Any] | None = None,
    exclude_work_item_ids: Sequence[str] = (),
    related_pr_resolved: Mapping[str, bool] | None = None,
) -> JitEligibility:
    if isinstance(backlog, (str, Path, Mapping)):
        items = list(load_phase_chapter_backlog(backlog))
    else:
        items = list(backlog)
    proofs = _terminal_proofs(predecessor_terminal, terminal_proofs)
    excluded = {str(item).strip() for item in exclude_work_item_ids if str(item).strip()}
    decisions: dict[str, Any] = {}
    blocked_orders: dict[str, Any] = {
        str(item.order): {
            "chapter_id": item.chapter_id,
            "eligibility": item.eligibility,
            "related_pull_requests": list(item.related_pull_requests),
            "resolved": not order_is_draft_gated_blocked(
                items,
                item.order,
                related_pr_resolved=related_pr_resolved,
            ),
        }
        for item in items
        if item.order in {MARKET_COMPARE_ORDER, RISK_EXPRESSION_ORDER}
    }
    first_pending: BacklogItem | None = None

    for item in items:
        if item.packetization != "just_in_time":
            continue
        if first_pending is None:
            first_pending = item
        related_resolved = not order_is_draft_gated_blocked(
            items,
            item.order,
            related_pr_resolved=related_pr_resolved,
        )
        decision: dict[str, Any] = {
            "chapter_id": item.chapter_id,
            "order": item.order,
            "status": item.status,
            "eligibility": item.eligibility,
            "depends_on": list(item.depends_on),
            "related_pull_requests": list(item.related_pull_requests),
            "related_pull_requests_resolved": related_resolved,
            "packet_spec_present": _has_packet_spec(item),
        }
        proof = proofs.get(item.chapter_id)
        if _proof_is_merged_for_work_item(proof, item.chapter_id):
            decision["decision"] = "completed"
            decisions[item.chapter_id] = decision
            continue
        if item.chapter_id in excluded:
            # An exclusion prevents duplicate work. It does not prove success for
            # dependants; only an exact merged completion may do that.
            decision["decision"] = "excluded_without_merged_proof"
            decisions[item.chapter_id] = decision
            continue
        if item.status.strip().lower() in DEFERRED_ITEM_STATUSES or item.eligibility.startswith(
            "deferred_"
        ):
            decision["decision"] = "deferred"
            decisions[item.chapter_id] = decision
            continue
        if item.eligibility not in SUPPORTED_JIT_ELIGIBILITY:
            decision["decision"] = "blocked_unsupported_eligibility"
            decisions[item.chapter_id] = decision
            continue
        unmet = [
            dep
            for dep in item.depends_on
            if not _proof_is_merged_for_work_item(proofs.get(dep), dep)
        ]
        if unmet:
            decision["decision"] = "blocked_dependencies"
            decision["unmet_dependencies"] = unmet
            decisions[item.chapter_id] = decision
            continue
        if not related_resolved:
            decision["decision"] = "blocked_related_pull_request_decision"
            decisions[item.chapter_id] = decision
            continue
        if not _has_packet_spec(item):
            decision["decision"] = "blocked_packet_spec_missing"
            decisions[item.chapter_id] = decision
            continue

        decision["decision"] = "eligible"
        decisions[item.chapter_id] = decision
        reason = "dependencies_terminal"
        if item.chapter_id == GUIDED_SHELL_WORK_ITEM_ID:
            reason = "predecessor_terminal"
        return JitEligibility(
            status="eligible",
            reason=reason,
            work_item_id=item.chapter_id,
            order=item.order,
            evidence={"items": decisions, "blocked_orders": blocked_orders},
        )

    guided = next(
        (item for item in items if item.chapter_id == GUIDED_SHELL_WORK_ITEM_ID),
        None,
    )
    reason = "no_eligible_jit_item"
    work_item_id = first_pending.chapter_id if first_pending else None
    order = first_pending.order if first_pending else None
    guided_complete = bool(
        guided
        and _proof_is_merged_for_work_item(
            proofs.get(guided.chapter_id), guided.chapter_id
        )
    )
    if guided is None:
        reason = "guided_shell_missing_from_backlog"
    elif guided.packetization != "just_in_time":
        reason = "guided_shell_not_just_in_time"
        work_item_id = guided.chapter_id
        order = guided.order
    elif not guided_complete and guided.chapter_id in excluded:
        reason = "work_item_excluded"
        work_item_id = guided.chapter_id
        order = guided.order
    elif not guided_complete and not _proof_is_merged_for_work_item(
        proofs.get(PREDECESSOR_WORK_ITEM_ID), PREDECESSOR_WORK_ITEM_ID
    ):
        reason = "predecessor_not_terminal"
        work_item_id = guided.chapter_id
        order = guided.order
    return JitEligibility(
        status="skipped",
        reason=reason,
        work_item_id=work_item_id,
        order=order,
        evidence={"items": decisions, "blocked_orders": blocked_orders},
    )


def freeze_ppe_main_sha(
    *,
    ppe_repo: Path | None,
    target_repository: str = DEFAULT_TARGET_REPOSITORY,
    target_remote_url: str = DEFAULT_TARGET_REMOTE_URL,
    allow_test_local_source_remote: bool = False,
    fetch_remote: bool = True,
    frozen_commit: str | None = None,
) -> tuple[str, str]:
    """Return (commit, remote_url) frozen at materialization time."""
    repo = ppe_repo.expanduser().resolve() if ppe_repo is not None else None
    remote = target_remote_url
    if repo is not None:
        remote = _git(repo, "remote", "get-url", "origin", accepted=(0, 2)) or remote
    remote_repository = normalize_github_repository(remote)
    if remote_repository != target_repository and not (
        remote_repository is None and allow_test_local_source_remote
    ):
        raise CatalogMaterializerError("target remote does not match target_repository")

    if frozen_commit:
        commit = frozen_commit.lower()
        if not _COMMIT_RE.fullmatch(commit):
            raise CatalogMaterializerError("frozen_commit must be a 40-character SHA")
    elif repo is None:
        raise CatalogMaterializerError("ppe_repo is required to freeze current main SHA")
    elif fetch_remote:
        # A failed fetch is not permission to reuse an old tracking ref or HEAD.
        _git(repo, "fetch", "--no-tags", "origin", "main")
        commit = _git(repo, "rev-parse", "FETCH_HEAD^{commit}").lower()
    else:
        commit = _git(repo, "rev-parse", "HEAD^{commit}").lower()
    if not _COMMIT_RE.fullmatch(commit):
        raise CatalogMaterializerError("could not freeze a 40-character PPE main SHA")
    if frozen_commit or not fetch_remote:
        try:
            prove_declared_commit_fetchable(
                target_repository=target_repository,
                target_source_commit=commit,
                remote_url=remote,
                allow_test_local_source_remote=allow_test_local_source_remote,
            )
        except JobPacketError as exc:
            raise CatalogMaterializerError(str(exc)) from exc
    return commit, remote


def author_guided_shell_packet(
    *,
    frozen_commit: str,
    target_remote_url: str,
    dependency_source_sha256: str,
    target_repository: str = DEFAULT_TARGET_REPOSITORY,
    allow_test_local_source_remote: bool = False,
    allowed_paths: Sequence[str] | None = None,
    objective: str | None = None,
) -> dict[str, Any]:
    paths = tuple(allowed_paths) if allowed_paths is not None else GUIDED_SHELL_ALLOWED_PATHS
    commit = frozen_commit.lower()
    if not _COMMIT_RE.fullmatch(commit):
        raise CatalogMaterializerError("frozen_commit must be a 40-character SHA")
    slice_id = "RegionBet-GuidedShell-Product-Slice002"
    packet: dict[str, Any] = {
        "version": 1,
        "pipeline_id": "ppe",
        "work_item_id": GUIDED_SHELL_WORK_ITEM_ID,
        "order": GUIDED_SHELL_ORDER,
        "eligible": True,
        "target_repository": target_repository,
        "target_source_commit": commit,
        "target_remote_url": target_remote_url,
        "adapter": DEFAULT_ADAPTER,
        "allowed_paths": list(paths),
        "native_slice": {
            "slice_id": slice_id,
            "build_branch": f"build/auto/{slice_id}",
            "layer_preset": "MSOS_UI",
            "worker_mode": "local-agent",
            "declared_plane": "PRODUCT-PLANE",
            "touch_set": list(paths),
            "sequence_index": 1,
            "total_slices": 1,
            "previous_slices": [],
            "following_slices": [],
            "raw_slice": {
                "sliceId": slice_id,
                "layerPreset": "MSOS_UI",
                "buildBranch": f"build/auto/{slice_id}",
                "declaredPlane": "PRODUCT-PLANE",
                "implementationStatus": "PENDING",
                "workerMode": "local-agent",
                "touchSet": list(paths),
            },
        },
        "prerequisites": {
            "version": 1,
            "read_only": True,
            "source": "ppe_approved_backlog_jit",
            "evidence": {
                "frozen_target_commit": commit,
                "target_remote_url": target_remote_url,
                "backlog_chapter_id": GUIDED_SHELL_WORK_ITEM_ID,
                "depends_on": [PREDECESSOR_WORK_ITEM_ID],
                "packetization": "just_in_time",
            },
        },
        "dependency_source_sha256": dependency_source_sha256,
        "authority": dict(DEFAULT_AUTHORITY),
        "validation": dict(DEFAULT_VALIDATION),
    }
    if objective:
        packet["native_slice"]["raw_slice"]["objective"] = objective
        packet["native_slice"]["raw_slice"]["backlogSource"] = (
            f"{BACKLOG_RELPATH}#{GUIDED_SHELL_WORK_ITEM_ID}"
        )
    # Order-07 backlog does not authorize AUTO_MERGE_WHEN_GREEN; omit merge_authority.
    parse_approved_job_packet(
        packet,
        source_path=GUIDED_SHELL_CATALOG_FILENAME,
        allow_test_local_source_remote=allow_test_local_source_remote,
    )
    return packet


def catalog_filename_for_item(item: BacklogItem) -> str:
    if not _SAFE_FILENAME_ID_RE.fullmatch(item.chapter_id):
        raise CatalogMaterializerError(
            f"backlog chapter id is unsafe for a catalog filename: {item.chapter_id!r}"
        )
    return f"{item.order:02d}-{item.chapter_id}.json"


def _required_packet_text(spec: Mapping[str, Any], key: str) -> str:
    value = spec.get(key)
    text = str(value or "").strip()
    if not isinstance(value, str) or not text:
        raise CatalogMaterializerError(f"{PACKET_SPEC_KEY}.{key} is required")
    return text


def _required_packet_string_list(spec: Mapping[str, Any], key: str) -> list[str]:
    value = spec.get(key)
    if not isinstance(value, list) or not value:
        raise CatalogMaterializerError(f"{PACKET_SPEC_KEY}.{key} must be a non-empty list")
    normalized = [str(item).strip() for item in value]
    if any(not item for item in normalized) or len(set(normalized)) != len(normalized):
        raise CatalogMaterializerError(
            f"{PACKET_SPEC_KEY}.{key} contains empty or duplicate values"
        )
    return normalized


def _attach_merge_authority(
    packet: dict[str, Any],
    item: BacklogItem,
    *,
    packet_spec: Mapping[str, Any] | None = None,
) -> None:
    raw = item.raw.get("autobuilderMergeAuthority")
    if raw in (None, "") and packet_spec is not None:
        raw = packet_spec.get("mergeAuthority")
    if raw in (None, ""):
        return
    if not isinstance(raw, Mapping):
        raise CatalogMaterializerError("autobuilder merge authority must be an object")
    packet["merge_authority"] = {
        "class": str(raw.get("class") or raw.get("authorityClass") or "").strip(),
        "declared_at": str(raw.get("declaredAt") or raw.get("declared_at") or "").strip(),
    }


def _validate_authored_packet(
    packet: dict[str, Any],
    item: BacklogItem,
    *,
    allow_test_local_source_remote: bool,
) -> dict[str, Any]:
    try:
        parse_approved_job_packet(
            packet,
            source_path=catalog_filename_for_item(item),
            allow_test_local_source_remote=allow_test_local_source_remote,
        )
    except JobPacketError as exc:
        raise CatalogMaterializerError(
            f"invalid JIT packet contract for {item.chapter_id}: {exc}"
        ) from exc
    return packet


def author_backlog_packet(
    *,
    item: BacklogItem,
    frozen_commit: str,
    target_remote_url: str,
    dependency_source_sha256: str,
    target_repository: str = DEFAULT_TARGET_REPOSITORY,
    allow_test_local_source_remote: bool = False,
    phase_plan: Mapping[str, Any] | None = None,
    phase_plan_path: str | None = None,
    prerequisite_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Author one executable packet from the frozen PPE backlog contract."""
    objective = str(item.raw.get("reason") or "").strip()
    if not objective:
        raise CatalogMaterializerError(f"eligible backlog item {item.chapter_id} has no objective")
    commit = frozen_commit.lower()
    if not _COMMIT_RE.fullmatch(commit):
        raise CatalogMaterializerError("frozen_commit must be a 40-character SHA")

    if phase_plan is not None:
        declared_path = str(item.raw.get("planPath") or "").strip()
        plan_path = str(phase_plan_path or declared_path).strip()
        if not plan_path or (declared_path and plan_path != declared_path):
            raise CatalogMaterializerError(f"phase plan identity conflicts for {item.chapter_id}")
        try:
            native = _select_native_slice(dict(phase_plan))
        except BuildNextError as exc:
            raise CatalogMaterializerError(
                f"invalid frozen phase plan for {item.chapter_id}: {exc}"
            ) from exc
        max_attempts = native.raw_slice.get("maxAttempts", 2)
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
            or max_attempts > 3
        ):
            raise CatalogMaterializerError(
                f"phase plan maxAttempts for {item.chapter_id} must be an integer from 1 to 3"
            )
        acceptance = phase_plan.get("acceptanceCriteria")
        if item.chapter_id != GUIDED_SHELL_WORK_ITEM_ID and (
            not isinstance(acceptance, list)
            or not acceptance
            or not all(isinstance(value, str) and value.strip() for value in acceptance)
        ):
            raise CatalogMaterializerError(
                f"phase plan for {item.chapter_id} requires acceptanceCriteria"
            )
        raw_slice = dict(native.raw_slice)
        raw_slice["objective"] = objective
        raw_slice["backlogSource"] = f"{BACKLOG_RELPATH}#{item.chapter_id}"
        if isinstance(acceptance, list) and acceptance:
            raw_slice["acceptanceCriteria"] = [value.strip() for value in acceptance]
        packet: dict[str, Any] = {
            "version": 1,
            "pipeline_id": "ppe",
            "work_item_id": item.chapter_id,
            "order": item.order,
            "eligible": True,
            "target_repository": target_repository,
            "target_source_commit": commit,
            "target_remote_url": target_remote_url,
            "adapter": DEFAULT_ADAPTER,
            "allowed_paths": list(native.touch_set),
            "native_slice": {
                "slice_id": native.slice_id,
                "build_branch": native.build_branch,
                "layer_preset": native.layer_preset,
                "worker_mode": native.worker_mode,
                "declared_plane": native.declared_plane,
                "touch_set": list(native.touch_set),
                "sequence_index": native.sequence_index,
                "total_slices": native.total_slices,
                "previous_slices": list(native.previous_slices),
                "following_slices": list(native.following_slices),
                "raw_slice": raw_slice,
            },
            "phase_plan": plan_path,
            "prerequisites": dict(prerequisite_evidence or {}),
            "dependency_source_sha256": dependency_source_sha256,
            "authority": dict(DEFAULT_AUTHORITY),
            "validation": dict(DEFAULT_VALIDATION),
        }
        if native.sprint_spec_path:
            packet["native_slice"]["sprint_spec_path"] = native.sprint_spec_path
        if native.selection_record:
            packet["native_slice"]["selection_record"] = native.selection_record
        _attach_merge_authority(packet, item)
        return _validate_authored_packet(
            packet,
            item,
            allow_test_local_source_remote=allow_test_local_source_remote,
        )

    raw_spec = item.raw.get(PACKET_SPEC_KEY)
    if not isinstance(raw_spec, Mapping):
        if item.chapter_id != GUIDED_SHELL_WORK_ITEM_ID:
            raise CatalogMaterializerError(
                f"eligible backlog item {item.chapter_id} lacks {PACKET_SPEC_KEY}"
            )
        objective = str(item.raw.get("reason") or "").strip()
        packet = author_guided_shell_packet(
            frozen_commit=frozen_commit,
            target_remote_url=target_remote_url,
            dependency_source_sha256=dependency_source_sha256,
            target_repository=target_repository,
            allow_test_local_source_remote=allow_test_local_source_remote,
            objective=objective or None,
        )
        _attach_merge_authority(packet, item)
        return _validate_authored_packet(
            packet,
            item,
            allow_test_local_source_remote=allow_test_local_source_remote,
        )

    spec = dict(raw_spec)
    if spec.get("version") != 1:
        raise CatalogMaterializerError(f"{PACKET_SPEC_KEY}.version must be 1")
    paths = _required_packet_string_list(spec, "allowedPaths")
    acceptance = _required_packet_string_list(spec, "acceptanceCriteria")
    slice_id = _required_packet_text(spec, "sliceId")
    layer_preset = _required_packet_text(spec, "layerPreset")
    build_branch = str(spec.get("buildBranch") or f"build/auto/{slice_id}").strip()
    if not build_branch:
        raise CatalogMaterializerError(f"{PACKET_SPEC_KEY}.buildBranch is empty")
    declared_plane = str(spec.get("declaredPlane") or "PRODUCT-PLANE").strip()
    if declared_plane != "PRODUCT-PLANE":
        raise CatalogMaterializerError(f"{PACKET_SPEC_KEY}.declaredPlane must be PRODUCT-PLANE")
    max_attempts = spec.get("maxAttempts", 2)
    if (
        not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
        or max_attempts > 3
    ):
        raise CatalogMaterializerError(
            f"{PACKET_SPEC_KEY}.maxAttempts must be an integer from 1 to 3"
        )
    raw_slice: dict[str, Any] = {
        "sliceId": slice_id,
        "layerPreset": layer_preset,
        "buildBranch": build_branch,
        "declaredPlane": declared_plane,
        "implementationStatus": "PENDING",
        "workerMode": str(spec.get("workerMode") or "local-agent").strip(),
        "susMinutes": int(spec.get("susMinutes", 90)),
        "hardMinutes": int(spec.get("hardMinutes", 240)),
        "maxAttempts": max_attempts,
        "touchSet": list(paths),
        "objective": objective,
        "acceptanceCriteria": acceptance,
        "backlogSource": f"{BACKLOG_RELPATH}#{item.chapter_id}",
    }
    canon_ref = str(item.raw.get("canonRef") or "").strip()
    if canon_ref:
        raw_slice["canonRef"] = canon_ref

    packet: dict[str, Any] = {
        "version": 1,
        "pipeline_id": "ppe",
        "work_item_id": item.chapter_id,
        "order": item.order,
        "eligible": True,
        "target_repository": target_repository,
        "target_source_commit": commit,
        "target_remote_url": target_remote_url,
        "adapter": str(spec.get("adapter") or DEFAULT_ADAPTER).strip(),
        "allowed_paths": list(paths),
        "native_slice": {
            "slice_id": slice_id,
            "build_branch": build_branch,
            "layer_preset": layer_preset,
            "worker_mode": raw_slice["workerMode"],
            "declared_plane": declared_plane,
            "touch_set": list(paths),
            "sequence_index": 0,
            "total_slices": 1,
            "previous_slices": [],
            "following_slices": [],
            "raw_slice": raw_slice,
        },
        "prerequisites": {
            "version": 1,
            "read_only": True,
            "source": "ppe_approved_backlog_jit",
            "evidence": {
                "frozen_target_commit": commit,
                "target_remote_url": target_remote_url,
                "backlog_chapter_id": item.chapter_id,
                "depends_on": list(item.depends_on),
                "related_pull_requests": list(item.related_pull_requests),
                "related_pull_request_resolutions": item.raw.get(
                    "relatedPullRequestResolutions", []
                ),
                "packetization": item.packetization,
            },
        },
        "dependency_source_sha256": dependency_source_sha256,
        "authority": dict(DEFAULT_AUTHORITY),
        "validation": dict(spec.get("validation") or DEFAULT_VALIDATION),
    }
    phase_plan = str(spec.get("phasePlan") or item.raw.get("planPath") or "").strip()
    if phase_plan:
        packet["phase_plan"] = phase_plan
    sprint_spec = str(spec.get("sprintSpecPath") or "").strip()
    if sprint_spec:
        packet["native_slice"]["sprint_spec_path"] = sprint_spec
        raw_slice["sprintSpecPath"] = sprint_spec
    selection_record = str(
        spec.get("selectionRecord") or item.raw.get("selectionRecord") or ""
    ).strip()
    if selection_record:
        packet["native_slice"]["selection_record"] = selection_record
        raw_slice["selectionRecord"] = selection_record

    _attach_merge_authority(packet, item, packet_spec=spec)
    return _validate_authored_packet(
        packet,
        item,
        allow_test_local_source_remote=allow_test_local_source_remote,
    )


def write_packet_to_catalog_dir(
    catalog_dir: Path,
    packet: Mapping[str, Any],
    *,
    filename: str = GUIDED_SHELL_CATALOG_FILENAME,
    allow_test_local_source_remote: bool = False,
) -> MaterializeResult:
    catalog_dir = catalog_dir.expanduser().resolve()
    catalog_dir.mkdir(parents=True, exist_ok=True)
    destination = catalog_dir / filename
    parsed = parse_approved_job_packet(
        packet,
        source_path=destination.as_posix(),
        allow_test_local_source_remote=allow_test_local_source_remote,
    )
    with _CATALOG_WRITE_LOCK:
        if destination.exists():
            try:
                existing = json.loads(destination.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CatalogMaterializerError(
                    f"conflicting_catalog_packet unreadable: {destination}"
                ) from exc
            if not isinstance(existing, dict):
                raise CatalogMaterializerError(
                    f"conflicting_catalog_packet not an object: {destination}"
                )
            if _packets_equal(existing, packet):
                return MaterializeResult(
                    status="unchanged",
                    reason="identical_catalog_packet",
                    work_item_id=parsed.work_item_id,
                    order=parsed.order,
                    packet_path=destination.as_posix(),
                    packet_sha256=parsed.packet_sha256,
                    frozen_commit=parsed.target_source_commit,
                )
            raise CatalogMaterializerError(f"conflicting_catalog_packet already exists: {filename}")
        for path in sorted(catalog_dir.glob("*.json")):
            try:
                other = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(other, dict):
                continue
            if other.get("order") == parsed.order and path.name != filename:
                raise CatalogMaterializerError(
                    f"conflicting_catalog_packet order {parsed.order} at {path.name}"
                )
            if other.get("work_item_id") == parsed.work_item_id and path.name != filename:
                raise CatalogMaterializerError(
                    f"conflicting_catalog_packet work_item_id at {path.name}"
                )
        destination.write_bytes(_packet_bytes(packet))
    return MaterializeResult(
        status="created",
        reason="catalog_packet_created",
        work_item_id=parsed.work_item_id,
        order=parsed.order,
        packet_path=destination.as_posix(),
        packet_sha256=parsed.packet_sha256,
        frozen_commit=parsed.target_source_commit,
    )


def _frozen_blob(ppe_repo: Path | None, commit: str, path: str) -> bytes:
    if ppe_repo is None:
        raise CatalogMaterializerError(f"ppe_repo is required to read frozen {path}")
    try:
        return subprocess.check_output(
            ["git", "-C", str(ppe_repo), "cat-file", "blob", f"{commit}:{path}"],
            stderr=subprocess.PIPE,
            env=git_environment(),
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CatalogMaterializerError(f"cannot read frozen {path} at {commit}") from exc


def _dependency_digest(ppe_repo: Path | None, commit: str) -> str:
    return canonical_dependency_source_sha256(_frozen_blob(ppe_repo, commit, "requirements.txt"))


def _safe_frozen_path(value: Any, label: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise CatalogMaterializerError(f"{label} must be a safe relative path")
    return path.as_posix()


def _frozen_phase_plan_inputs(
    ppe_repo: Path | None,
    commit: str,
    item: BacklogItem,
) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
    raw_path = item.raw.get("planPath")
    if raw_path in (None, ""):
        return None
    plan_path = _safe_frozen_path(raw_path, "backlog planPath")
    try:
        plan = json.loads(_frozen_blob(ppe_repo, commit, plan_path))
    except (ValueError, UnicodeError) as exc:
        raise CatalogMaterializerError(
            f"invalid frozen phase plan {plan_path} at {commit}"
        ) from exc
    if not isinstance(plan, dict):
        raise CatalogMaterializerError(f"frozen phase plan must be an object: {plan_path}")
    plan_name = str(plan.get("name") or "").strip()
    product_scope = plan.get("productScope")
    stable_id = ""
    if isinstance(product_scope, Mapping):
        stable_id = str(product_scope.get("stableId") or "").strip()
    if plan_name != item.chapter_id and stable_id != item.chapter_id:
        raise CatalogMaterializerError(
            f"frozen phase plan identity does not match {item.chapter_id}"
        )

    try:
        native = _select_native_slice(plan)
    except BuildNextError as exc:
        raise CatalogMaterializerError(
            f"invalid frozen phase plan for {item.chapter_id}: {exc}"
        ) from exc
    slices = [value for value in plan.get("slices") or [] if isinstance(value, dict)]
    statuses: list[dict[str, Any]] = []
    for value in slices[: native.sequence_index]:
        slice_id = str(value.get("sliceId") or "").strip()
        if not slice_id:
            continue
        statuses.append(
            {
                "slice_id": slice_id,
                "status": str(value.get("implementationStatus") or "").strip().lower(),
                "non_blocking": bool(value.get("nonBlocking") or value.get("non_blocking")),
            }
        )
    for status in statuses:
        if status["status"] not in {"complete", "completed"} and not status["non_blocking"]:
            raise CatalogMaterializerError(
                f"frozen phase plan has an unmet blocking prerequisite slice: {status['slice_id']}"
            )

    source_paths = [plan_path]
    for value in (
        plan.get("sprintSpecPath"),
        plan.get("selectionRecord"),
        plan.get("evidenceStatusPath"),
    ):
        if value not in (None, ""):
            normalized = _safe_frozen_path(value, "phase-plan source path")
            if normalized not in source_paths:
                source_paths.append(normalized)
    source_files: list[dict[str, Any]] = []
    for source_path in source_paths:
        blob = _frozen_blob(ppe_repo, commit, source_path)
        blob_sha = _git(ppe_repo, "rev-parse", f"{commit}:{source_path}")
        source_files.append(
            {
                "path": source_path,
                "exists": True,
                "git_commit": commit,
                "blob_sha": blob_sha,
                "sha256": hashlib.sha256(blob).hexdigest(),
            }
        )

    evidence: dict[str, Any] = {
        "frozen_target_commit": commit,
        "phase_plan": plan_path,
        "phase_plan_blob_sha": source_files[0]["blob_sha"],
        "backlog_chapter_id": item.chapter_id,
        "depends_on": list(item.depends_on),
        "related_pull_requests": list(item.related_pull_requests),
        "related_pull_request_resolutions": item.raw.get("relatedPullRequestResolutions", []),
        "packetization": item.packetization,
        "source_files": source_files,
    }
    if statuses:
        evidence["control_slice"] = {
            "slice_id": statuses[0]["slice_id"],
            "implementationStatus": str(statuses[0]["status"]).upper(),
            "nonBlocking": statuses[0]["non_blocking"],
        }
    prerequisites = {
        "version": 1,
        "read_only": True,
        "source": "ppe_native_read_only",
        "evidence": evidence,
        "statuses": statuses,
    }
    return plan, plan_path, prerequisites


def _publish_packet_to_jobs(
    *,
    packet: Mapping[str, Any],
    filename: str,
    feed_repo_url: str,
    jobs_branch: str,
    catalog_path: str,
    checkout_root: Path | None,
    allow_test_local_source_remote: bool,
) -> MaterializeResult:
    from .build_next import BuildNextConfig

    if not feed_repo_url.strip():
        raise CatalogMaterializerError(
            "JIT catalog publish requires feed_repo_url; refusing silent skip"
        )
    bn = BuildNextConfig(
        feed_repo_url=feed_repo_url,
        jobs_branch=jobs_branch,
        catalog_path=catalog_path,
        checkout_root=checkout_root,
        allow_test_local_source_remote=allow_test_local_source_remote,
        submit=False,
    )
    lock_root = (
        (checkout_root or Path(tempfile.gettempdir()) / "msos-autobuilder-build-next-feed")
        .expanduser()
        .resolve()
    )
    with FeedMutationLock(lock_root.with_suffix(".catalog.lock")):
        checkout = _prepare_feed_checkout(bn)
        catalog = checkout / catalog_path
        result = write_packet_to_catalog_dir(
            catalog,
            packet,
            filename=filename,
            allow_test_local_source_remote=allow_test_local_source_remote,
        )
        relative = Path(catalog_path) / filename
        if result.status == "created":
            _git(checkout, "add", "--", relative.as_posix())
            changed = subprocess.run(
                ["git", "-C", str(checkout), "diff", "--cached", "--quiet"],
                capture_output=True,
                check=False,
            ).returncode
            if changed != 0:
                _git(
                    checkout,
                    "commit",
                    "-m",
                    (
                        f"Add {result.order:02d}-{result.work_item_id} catalog "
                        "packet via JIT materializer."
                    ),
                )
                commit = _git(checkout, "rev-parse", "HEAD")
                _git(checkout, "push", "origin", f"HEAD:{jobs_branch}")
                return MaterializeResult(
                    status="created",
                    reason=result.reason,
                    work_item_id=result.work_item_id,
                    order=result.order,
                    packet_path=relative.as_posix(),
                    packet_sha256=result.packet_sha256,
                    frozen_commit=result.frozen_commit,
                    feed_commit=commit,
                )
        feed_commit = (
            _git(
                checkout,
                "log",
                "-n",
                "1",
                "--format=%H",
                "--",
                relative.as_posix(),
                accepted=(0, 1),
            )
            or None
        )
        return MaterializeResult(
            status=result.status,
            reason=result.reason,
            work_item_id=result.work_item_id,
            order=result.order,
            packet_path=relative.as_posix(),
            packet_sha256=result.packet_sha256,
            frozen_commit=result.frozen_commit,
            feed_commit=feed_commit,
        )


def materialize_next_backlog_packet(
    *,
    backlog: Sequence[BacklogItem] | Mapping[str, Any] | Path | str,
    ppe_repo: Path | None = None,
    predecessor_terminal: Any = False,
    terminal_proofs: Mapping[str, Any] | None = None,
    exclude_work_item_ids: Sequence[str] = (),
    host_root: Path | None = None,
    results_root: Path | None = None,
    generation: Mapping[str, Any] | None = None,
    catalog_dir: Path | None = None,
    publish_to_jobs: bool = False,
    feed_repo_url: str = "",
    jobs_branch: str = "jobs",
    catalog_path: str = DEFAULT_CATALOG_RELPATH,
    checkout_root: Path | None = None,
    target_repository: str = DEFAULT_TARGET_REPOSITORY,
    target_remote_url: str = DEFAULT_TARGET_REMOTE_URL,
    allow_test_local_source_remote: bool = False,
    fetch_remote: bool = True,
    frozen_commit: str | None = None,
    related_pr_resolved: Mapping[str, bool] | None = None,
) -> MaterializeResult:
    commit, remote = freeze_ppe_main_sha(
        ppe_repo=ppe_repo,
        target_repository=target_repository,
        target_remote_url=target_remote_url,
        allow_test_local_source_remote=allow_test_local_source_remote,
        fetch_remote=fetch_remote,
        frozen_commit=frozen_commit,
    )
    if (
        ppe_repo is not None
        and isinstance(backlog, (str, Path))
        and Path(backlog).resolve() == (ppe_repo / BACKLOG_RELPATH).resolve()
    ):
        # Eligibility and dependencies must describe the same commit as the job.
        try:
            frozen_backlog = json.loads(_frozen_blob(ppe_repo, commit, BACKLOG_RELPATH))
        except (ValueError, UnicodeError) as exc:
            raise CatalogMaterializerError("invalid frozen chapter backlog") from exc
        if not isinstance(frozen_backlog, Mapping):
            raise CatalogMaterializerError("frozen chapter backlog must be an object")
        backlog = frozen_backlog
    items = (
        load_phase_chapter_backlog(backlog)
        if isinstance(backlog, (str, Path, Mapping))
        else tuple(backlog)
    )
    proofs = dict(terminal_proofs or {})
    if predecessor_terminal not in (None, False, ""):
        proofs.setdefault(PREDECESSOR_WORK_ITEM_ID, predecessor_terminal)
    relevant_ids = {
        value for item in items for value in (item.chapter_id, *item.depends_on) if value
    }
    for work_item_id in sorted(relevant_ids):
        if work_item_id in proofs:
            continue
        proof = resolve_work_item_terminal_proof(
            work_item_id=work_item_id,
            host_root=host_root,
            results_root=results_root,
            generation=generation,
        )
        if proof is not False:
            proofs[work_item_id] = proof

    eligibility = evaluate_jit_eligibility(
        items,
        predecessor_terminal=predecessor_terminal,
        terminal_proofs=proofs,
        exclude_work_item_ids=exclude_work_item_ids,
        related_pr_resolved=related_pr_resolved,
    )
    if eligibility.status != "eligible":
        return MaterializeResult(
            status="skipped",
            reason=eligibility.reason,
            work_item_id=eligibility.work_item_id,
            order=eligibility.order,
            frozen_commit=commit,
            evidence=eligibility.evidence,
        )
    if publish_to_jobs and not str(feed_repo_url or "").strip():
        raise CatalogMaterializerError(
            "JIT catalog materialization requires feed_repo_url when publish_to_jobs=True"
        )
    if not publish_to_jobs and catalog_dir is None:
        raise CatalogMaterializerError(
            "JIT catalog materialization requires catalog_dir or publish_to_jobs config"
        )

    selected_item = next(item for item in items if item.chapter_id == eligibility.work_item_id)
    plan_inputs = _frozen_phase_plan_inputs(ppe_repo, commit, selected_item)
    packet = author_backlog_packet(
        item=selected_item,
        frozen_commit=commit,
        target_remote_url=remote,
        dependency_source_sha256=_dependency_digest(ppe_repo, commit),
        target_repository=target_repository,
        allow_test_local_source_remote=allow_test_local_source_remote,
        phase_plan=plan_inputs[0] if plan_inputs else None,
        phase_plan_path=plan_inputs[1] if plan_inputs else None,
        prerequisite_evidence=plan_inputs[2] if plan_inputs else None,
    )
    filename = catalog_filename_for_item(selected_item)
    if publish_to_jobs:
        return _publish_packet_to_jobs(
            packet=packet,
            filename=filename,
            feed_repo_url=feed_repo_url,
            jobs_branch=jobs_branch,
            catalog_path=catalog_path,
            checkout_root=checkout_root,
            allow_test_local_source_remote=allow_test_local_source_remote,
        )
    assert catalog_dir is not None
    result = write_packet_to_catalog_dir(
        catalog_dir,
        packet,
        filename=filename,
        allow_test_local_source_remote=allow_test_local_source_remote,
    )
    return MaterializeResult(
        status=result.status,
        reason=result.reason,
        work_item_id=result.work_item_id,
        order=result.order,
        packet_path=result.packet_path,
        packet_sha256=result.packet_sha256,
        frozen_commit=commit,
        evidence=eligibility.evidence,
    )


def materialize_guided_shell_packet(
    *,
    backlog: Sequence[BacklogItem] | Mapping[str, Any] | Path | str,
    ppe_repo: Path | None = None,
    predecessor_terminal: Any = False,
    catalog_dir: Path | None = None,
    publish_to_jobs: bool = False,
    feed_repo_url: str = "",
    jobs_branch: str = "jobs",
    catalog_path: str = DEFAULT_CATALOG_RELPATH,
    checkout_root: Path | None = None,
    target_repository: str = DEFAULT_TARGET_REPOSITORY,
    target_remote_url: str = DEFAULT_TARGET_REMOTE_URL,
    allow_test_local_source_remote: bool = False,
    fetch_remote: bool = True,
    frozen_commit: str | None = None,
    related_pr_resolved: Mapping[str, bool] | None = None,
) -> MaterializeResult:
    """Compatibility wrapper for the original order-07 materializer API."""
    return materialize_next_backlog_packet(
        backlog=backlog,
        ppe_repo=ppe_repo,
        predecessor_terminal=predecessor_terminal,
        catalog_dir=catalog_dir,
        publish_to_jobs=publish_to_jobs,
        feed_repo_url=feed_repo_url,
        jobs_branch=jobs_branch,
        catalog_path=catalog_path,
        checkout_root=checkout_root,
        target_repository=target_repository,
        target_remote_url=target_remote_url,
        allow_test_local_source_remote=allow_test_local_source_remote,
        fetch_remote=fetch_remote,
        frozen_commit=frozen_commit,
        related_pr_resolved=related_pr_resolved,
    )


def _scan_completion_reports(results_root: Path, work_item_id: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if not results_root.exists():
        return matches
    for path in sorted(results_root.rglob("completion-report.json")):
        try:
            payload = _read_json_mapping(path)
        except CatalogMaterializerError:
            continue
        report_work = str(payload.get("work_item_id") or payload.get("workItemId") or "").strip()
        if report_work != work_item_id:
            continue
        matches.append({"status": str(payload.get("status") or "").strip().lower(), **payload})
    return matches


def _scan_lifecycle_snapshots(host_root: Path, work_item_id: str) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for root in (
        host_root / "state" / "lifecycle" / "work-items",
        host_root / "state" / "lifecycle" / "snapshots",
        host_root / "state" / "attempt-lifecycle" / "work-items",
    ):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            try:
                payload = _read_json_mapping(path)
            except CatalogMaterializerError:
                continue
            identity = payload.get("attempt_identity")
            identity_work = ""
            if isinstance(identity, Mapping):
                identity_work = str(identity.get("work_item_id") or "").strip()
            snapshot_work = str(payload.get("work_item_id") or "").strip()
            if work_item_id not in {identity_work, snapshot_work}:
                continue
            matches.append(payload)
    return matches


def resolve_work_item_terminal_proof(
    *,
    work_item_id: str = PREDECESSOR_WORK_ITEM_ID,
    predecessor_proof: Any = None,
    host_root: Path | None = None,
    results_root: Path | None = None,
    generation: Mapping[str, Any] | None = None,
) -> Any:
    if predecessor_proof is not None:
        return predecessor_proof
    if generation is not None:
        classification = generation.get("last_attempt_classification")
        if isinstance(classification, Mapping):
            evidence = classification.get("evidence")
            if isinstance(evidence, Mapping):
                identity = evidence.get("attempt_identity")
                identity_work = ""
                if isinstance(identity, Mapping):
                    identity_work = str(identity.get("work_item_id") or "").strip()
                reason = str(evidence.get("reason") or "").strip()
                # Require an explicit predecessor work-item id. Empty identity must
                # not false-qualify JIT eligibility (fixtures / unrelated terminals).
                if identity_work == work_item_id and reason in MERGED_LIFECYCLE_DISPOSITIONS:
                    return {
                        "item_terminal": True,
                        "item_disposition": reason,
                        "work_item_id": work_item_id,
                        "status": "merged",
                    }
    if results_root is not None:
        for report in _scan_completion_reports(results_root, work_item_id):
            if str(report.get("status") or "").lower() in MERGED_COMPLETION_STATUSES:
                return report
    if host_root is not None:
        for snapshot in _scan_lifecycle_snapshots(host_root, work_item_id):
            disposition = str(snapshot.get("item_disposition") or "").strip()
            if (
                snapshot.get("item_terminal") is True
                and disposition in MERGED_LIFECYCLE_DISPOSITIONS
            ):
                return snapshot
    return False


def resolve_predecessor_terminal_proof(
    *,
    work_item_id: str = PREDECESSOR_WORK_ITEM_ID,
    predecessor_proof: Any = None,
    host_root: Path | None = None,
    results_root: Path | None = None,
    generation: Mapping[str, Any] | None = None,
) -> Any:
    """Compatibility name for exact work-item terminal proof resolution."""
    return resolve_work_item_terminal_proof(
        work_item_id=work_item_id,
        predecessor_proof=predecessor_proof,
        host_root=host_root,
        results_root=results_root,
        generation=generation,
    )


def ensure_jit_catalog_for_refill(
    *,
    ppe_repo: Path | None = None,
    feed_repo_url: str = "",
    packet_root: Path | None = None,
    catalog_path: str = DEFAULT_CATALOG_RELPATH,
    jobs_branch: str = "jobs",
    checkout_root: Path | None = None,
    exclude_work_item_ids: Sequence[str] = (),
    predecessor_proof: Any = None,
    host_root: Path | None = None,
    results_root: Path | None = None,
    generation: Mapping[str, Any] | None = None,
    allow_test_local_source_remote: bool = False,
    publish_to_jobs: bool = False,
    fetch_remote: bool = True,
    backlog_path: Path | None = None,
) -> MaterializeResult:
    """Refill hook entry: materialize only when next select would be empty.

    Skip (do not fail closed) when catalog already has a next packet, predecessor
    is not durably terminal, or backlog/probe is unavailable. Fail closed only
    after eligibility is proven and authoring/publish cannot proceed safely.
    """
    try:
        if packet_root is not None:
            catalog = load_packet_dir(
                packet_root.expanduser().resolve(),
                allow_test_local_source_remote=allow_test_local_source_remote,
            )
        else:
            from .build_next import BuildNextConfig, _load_catalog

            if not feed_repo_url.strip():
                return MaterializeResult(
                    status="skipped",
                    reason="catalog_probe_unavailable",
                )
            catalog = _load_catalog(
                BuildNextConfig(
                    feed_repo_url=feed_repo_url,
                    catalog_path=catalog_path,
                    jobs_branch=jobs_branch,
                    checkout_root=checkout_root,
                    allow_test_local_source_remote=allow_test_local_source_remote,
                    submit=False,
                )
            )
    except JobPacketError as exc:
        # Malformed catalog ownership stays with build_next/refill dispatch.
        return MaterializeResult(
            status="skipped",
            reason="catalog_probe_unreadable",
            evidence={"message": str(exc)},
        )

    selected = select_next_packet(catalog, exclude_work_item_ids=exclude_work_item_ids)
    if selected is not None:
        return MaterializeResult(
            status="skipped",
            reason="catalog_next_not_empty",
            work_item_id=selected.work_item_id,
            order=selected.order,
            evidence={"selected_work_item_id": selected.work_item_id},
        )

    proof = resolve_predecessor_terminal_proof(
        predecessor_proof=predecessor_proof,
        host_root=host_root,
        results_root=results_root,
        generation=generation,
    )
    if not is_predecessor_terminal_proof(proof) and backlog_path is None and ppe_repo is None:
        return MaterializeResult(
            status="skipped",
            reason="predecessor_not_terminal",
            work_item_id=GUIDED_SHELL_WORK_ITEM_ID,
            order=GUIDED_SHELL_ORDER,
        )

    if backlog_path is not None:
        backlog: Any = backlog_path
    elif ppe_repo is not None:
        backlog = ppe_repo.expanduser().resolve() / BACKLOG_RELPATH
        if not Path(backlog).is_file():
            # A source checkout without the optional backlog contract cannot
            # establish JIT eligibility. Preserve normal UNFILLED handling;
            # fail closed only after an item has actually been proven eligible.
            return MaterializeResult(
                status="skipped",
                reason="backlog_unavailable",
                evidence={"path": str(backlog)},
            )
    else:
        raise CatalogMaterializerError(
            "JIT eligible but ppe_repo/backlog_path missing for catalog materialization"
        )

    return materialize_next_backlog_packet(
        backlog=backlog,
        ppe_repo=ppe_repo,
        predecessor_terminal=proof,
        exclude_work_item_ids=exclude_work_item_ids,
        host_root=host_root,
        results_root=results_root,
        generation=generation,
        catalog_dir=packet_root,
        publish_to_jobs=publish_to_jobs and packet_root is None,
        feed_repo_url=feed_repo_url,
        jobs_branch=jobs_branch,
        catalog_path=catalog_path,
        checkout_root=checkout_root,
        allow_test_local_source_remote=allow_test_local_source_remote,
        fetch_remote=fetch_remote,
    )
