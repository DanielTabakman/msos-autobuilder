"""Just-in-time approved-backlog → jobs/catalog packet materializer.

Stage 5: after a durable predecessor terminal signal, author the next eligible
Autobuilder catalog packet from PPE ``PHASE_CHAPTER_BACKLOG.json`` and publish
exactly one catalog file to the jobs branch when configured.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .build_next import FeedMutationLock, _prepare_feed_checkout
from .job_packet import (
    DEFAULT_CATALOG_RELPATH,
    load_packet_dir,
    parse_approved_job_packet,
    prove_declared_commit_fetchable,
    select_next_packet,
)
from .managed_source import normalize_github_repository
from .validation_contract import canonical_dependency_source_sha256


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

DEFAULT_TARGET_REPOSITORY = "DanielTabakman/Probability-prediction-engine"
DEFAULT_TARGET_REMOTE_URL = (
    "https://github.com/DanielTabakman/Probability-prediction-engine.git"
)
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
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_CATALOG_WRITE_LOCK = threading.Lock()


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
    for entry in items_raw:
        if not isinstance(entry, dict):
            continue
        order = entry.get("autobuilderCatalogOrder")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            continue
        chapter_id = str(entry.get("chapterId") or "").strip()
        if not chapter_id:
            continue
        items.append(
            BacklogItem(
                chapter_id=chapter_id,
                order=order,
                depends_on=tuple(
                    str(item).strip()
                    for item in (entry.get("dependsOn") or [])
                    if str(item).strip()
                ),
                packetization=str(entry.get("packetization") or "").strip(),
                eligibility=str(entry.get("eligibility") or "").strip(),
                status=str(entry.get("status") or "").strip(),
                related_pull_requests=tuple(
                    str(item).strip()
                    for item in (entry.get("relatedPullRequests") or [])
                    if str(item).strip()
                ),
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
        if (
            proof.get("item_terminal") is True
            and disposition in MERGED_LIFECYCLE_DISPOSITIONS
        ):
            return True
        # Explicit non-merged statuses (queued/drafted/open) fail closed.
        if status and status not in MERGED_COMPLETION_STATUSES:
            return False
    return False


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
    return not all(resolved_map.get(ref) is True for ref in item.related_pull_requests)


def evaluate_jit_eligibility(
    backlog: Sequence[BacklogItem] | Mapping[str, Any] | Path | str,
    *,
    predecessor_terminal: Any,
    related_pr_resolved: Mapping[str, bool] | None = None,
) -> JitEligibility:
    if isinstance(backlog, (str, Path, Mapping)):
        items = list(load_phase_chapter_backlog(backlog))
    else:
        items = list(backlog)
    blocked_orders: dict[str, Any] = {}
    for item in items:
        if item.order in {MARKET_COMPARE_ORDER, RISK_EXPRESSION_ORDER}:
            blocked_orders[str(item.order)] = {
                "chapter_id": item.chapter_id,
                "eligibility": item.eligibility,
                "related_pull_requests": list(item.related_pull_requests),
                "resolved": not order_is_draft_gated_blocked(
                    items, item.order, related_pr_resolved=related_pr_resolved
                ),
            }
    guided = next(
        (item for item in items if item.chapter_id == GUIDED_SHELL_WORK_ITEM_ID),
        None,
    )
    if guided is None:
        return JitEligibility(
            status="skipped",
            reason="guided_shell_missing_from_backlog",
            evidence={"blocked_orders": blocked_orders},
        )
    if guided.packetization != "just_in_time":
        return JitEligibility(
            status="skipped",
            reason="guided_shell_not_just_in_time",
            evidence={"blocked_orders": blocked_orders},
        )
    if not is_predecessor_terminal_proof(predecessor_terminal):
        return JitEligibility(
            status="skipped",
            reason="predecessor_not_terminal",
            work_item_id=guided.chapter_id,
            order=guided.order,
            evidence={"blocked_orders": blocked_orders},
        )
    for dep in guided.depends_on:
        if dep != PREDECESSOR_WORK_ITEM_ID:
            return JitEligibility(
                status="skipped",
                reason=f"unsupported_dependency:{dep}",
                evidence={"blocked_orders": blocked_orders},
            )
    return JitEligibility(
        status="eligible",
        reason="predecessor_terminal",
        work_item_id=guided.chapter_id,
        order=guided.order,
        evidence={"blocked_orders": blocked_orders},
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
    if frozen_commit:
        commit = frozen_commit.lower()
        if not _COMMIT_RE.fullmatch(commit):
            raise CatalogMaterializerError("frozen_commit must be a 40-character SHA")
        remote = target_remote_url
        if ppe_repo is not None:
            origin = _git(
                ppe_repo.expanduser().resolve(),
                "remote",
                "get-url",
                "origin",
                accepted=(0, 2),
            )
            if origin:
                remote = origin
        if fetch_remote or allow_test_local_source_remote:
            prove_declared_commit_fetchable(
                target_repository=target_repository,
                target_source_commit=commit,
                remote_url=remote,
                allow_test_local_source_remote=allow_test_local_source_remote,
            )
        return commit, remote

    if ppe_repo is None:
        raise CatalogMaterializerError("ppe_repo is required to freeze current main SHA")
    repo = ppe_repo.expanduser().resolve()
    if fetch_remote:
        _git(repo, "fetch", "--no-tags", "origin", "main", accepted=(0, 128))
        commit = _git(repo, "rev-parse", "origin/main", accepted=(0, 128)).lower()
        if not _COMMIT_RE.fullmatch(commit):
            commit = _git(repo, "rev-parse", "HEAD").lower()
    else:
        commit = _git(repo, "rev-parse", "HEAD").lower()
    if not _COMMIT_RE.fullmatch(commit):
        raise CatalogMaterializerError("could not freeze a 40-character PPE main SHA")
    remote = target_remote_url
    origin = _git(repo, "remote", "get-url", "origin", accepted=(0, 2))
    if origin:
        remote = origin
    prove_declared_commit_fetchable(
        target_repository=target_repository
        if normalize_github_repository(remote) is None
        else (normalize_github_repository(remote) or target_repository),
        target_source_commit=commit,
        remote_url=remote,
        allow_test_local_source_remote=allow_test_local_source_remote,
    )
    return commit, remote


def author_guided_shell_packet(
    *,
    frozen_commit: str,
    target_remote_url: str,
    dependency_source_sha256: str,
    target_repository: str = DEFAULT_TARGET_REPOSITORY,
    allow_test_local_source_remote: bool = False,
    allowed_paths: Sequence[str] | None = None,
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
    # Order-07 backlog does not authorize AUTO_MERGE_WHEN_GREEN; omit merge_authority.
    parse_approved_job_packet(
        packet,
        source_path=GUIDED_SHELL_CATALOG_FILENAME,
        allow_test_local_source_remote=allow_test_local_source_remote,
    )
    return packet


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
            raise CatalogMaterializerError(
                f"conflicting_catalog_packet already exists: {filename}"
            )
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
            if (
                other.get("work_item_id") == parsed.work_item_id
                and path.name != filename
            ):
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


def _dependency_digest(ppe_repo: Path | None) -> str:
    if ppe_repo is None:
        return "0" * 64
    requirements = ppe_repo.expanduser().resolve() / "requirements.txt"
    if not requirements.is_file():
        return "0" * 64
    return canonical_dependency_source_sha256(requirements.read_bytes())


def _publish_packet_to_jobs(
    *,
    packet: Mapping[str, Any],
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
        checkout_root
        or Path(tempfile.gettempdir()) / "msos-autobuilder-build-next-feed"
    ).expanduser().resolve()
    with FeedMutationLock(lock_root.with_suffix(".catalog.lock")):
        checkout = _prepare_feed_checkout(bn)
        catalog = checkout / catalog_path
        result = write_packet_to_catalog_dir(
            catalog,
            packet,
            allow_test_local_source_remote=allow_test_local_source_remote,
        )
        relative = Path(catalog_path) / GUIDED_SHELL_CATALOG_FILENAME
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
                    "Add 07-region_bet_guided_shell_v1 catalog packet via JIT materializer.",
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
        feed_commit = _git(
            checkout,
            "log",
            "-n",
            "1",
            "--format=%H",
            "--",
            relative.as_posix(),
            accepted=(0, 1),
        ) or None
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
    eligibility = evaluate_jit_eligibility(
        backlog,
        predecessor_terminal=predecessor_terminal,
        related_pr_resolved=related_pr_resolved,
    )
    if eligibility.status != "eligible":
        return MaterializeResult(
            status="skipped",
            reason=eligibility.reason,
            work_item_id=eligibility.work_item_id,
            order=eligibility.order,
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

    commit, remote = freeze_ppe_main_sha(
        ppe_repo=ppe_repo,
        target_repository=target_repository,
        target_remote_url=target_remote_url,
        allow_test_local_source_remote=allow_test_local_source_remote,
        fetch_remote=fetch_remote,
        frozen_commit=frozen_commit,
    )
    repo_name = normalize_github_repository(remote) or target_repository
    packet = author_guided_shell_packet(
        frozen_commit=commit,
        target_remote_url=remote,
        dependency_source_sha256=_dependency_digest(ppe_repo),
        target_repository=repo_name,
        allow_test_local_source_remote=allow_test_local_source_remote,
    )
    if publish_to_jobs:
        return _publish_packet_to_jobs(
            packet=packet,
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
        evidence={"blocked_orders": (eligibility.evidence or {}).get("blocked_orders")},
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
        report_work = str(
            payload.get("work_item_id") or payload.get("workItemId") or ""
        ).strip()
        job_id = str(payload.get("job_id") or path.parent.name).strip()
        if report_work != work_item_id and work_item_id not in job_id:
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


def resolve_predecessor_terminal_proof(
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
                if identity_work in {"", work_item_id} and reason in MERGED_LIFECYCLE_DISPOSITIONS:
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
    from .job_packet import JobPacketError

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
    if not is_predecessor_terminal_proof(proof):
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
            raise CatalogMaterializerError(
                f"JIT eligible but backlog missing at {backlog}"
            )
    else:
        raise CatalogMaterializerError(
            "JIT eligible but ppe_repo/backlog_path missing for catalog materialization"
        )

    return materialize_guided_shell_packet(
        backlog=backlog,
        ppe_repo=ppe_repo,
        predecessor_terminal=proof,
        catalog_dir=packet_root,
        publish_to_jobs=publish_to_jobs and packet_root is None,
        feed_repo_url=feed_repo_url,
        jobs_branch=jobs_branch,
        catalog_path=catalog_path,
        checkout_root=checkout_root,
        allow_test_local_source_remote=allow_test_local_source_remote,
        fetch_remote=fetch_remote,
    )
