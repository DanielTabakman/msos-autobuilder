"""One-shot founder ``build next`` dispatcher.

The dispatcher consumes PPE's read-only founder portfolio output and submits one
already-approved immutable job to the existing Autobuilder feed. It does not own
portfolio readiness or priority policy.
"""

from __future__ import annotations

import hashlib
import json
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

from .persistent_host import HostPaths, parse_host_job


class BuildNextError(RuntimeError):
    """Raised when build-next validation or feed submission fails closed."""


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
    return cleaned[:96] or fallback


def _safe_relative(value: Any, label: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise BuildNextError(f"{label} must be a safe relative path")
    return path.as_posix()


def _collect_snapshot(ppe_repo: Path) -> dict[str, Any]:
    script = ppe_repo / "scripts" / "founder_portfolio.py"
    if not script.is_file():
        raise BuildNextError(f"PPE founder portfolio script is missing: {script}")
    proc = _run(
        [
            sys.executable,
            str(script),
            "what's next",
            "--repo-root",
            str(ppe_repo),
            "--json",
        ],
        cwd=ppe_repo,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise BuildNextError("PPE founder portfolio output was not JSON") from exc
    if not isinstance(payload, dict):
        raise BuildNextError("PPE founder portfolio output must be an object")
    return payload


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
    if registry_pipe.get("canonical_repo") != "DanielTabakman/Probability-prediction-engine":
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


def _phase_plan_paths(plan: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    allowed: set[str] = set()
    forbidden = {
        ".git/**",
        "artifacts/**",
        ".github/workflows/**",
        "docs/SOP/CHATGPT_GITHUB_CODEX_CONTROL_PLANE_V1.md",
        "docs/SOP/FOUNDER_PIPELINE_COMMANDS_V1.md",
        "docs/SOP/PIPELINE_CREATION_SOP_V1.md",
        "docs/SOP/SCHEDULED_AUTOBUILDER_LANE_POLICY_V1.md",
    }
    for raw_slice in plan.get("slices") or []:
        if not isinstance(raw_slice, dict):
            continue
        for raw_path in raw_slice.get("touchSet") or []:
            rel = _safe_relative(raw_path, "touchSet entry")
            allowed.add(rel if rel.endswith("/") else rel)
        closeout = raw_slice.get("closeout") if isinstance(raw_slice.get("closeout"), dict) else {}
        for key in ("evidenceDoc", "sprintSpec", "selectionOutcomeDoc", "nextSelectionDoc"):
            if closeout.get(key):
                allowed.add(_safe_relative(closeout[key], f"closeout.{key}"))
        for raw_path in closeout.get("carryDocs") or []:
            allowed.add(_safe_relative(raw_path, "closeout.carryDocs entry"))
    for key in ("sprintSpecPath", "selectionRecord"):
        if plan.get(key):
            allowed.add(_safe_relative(plan[key], key))
    if not allowed:
        raise BuildNextError("selected phase plan does not declare path ownership")
    if any(path in forbidden or path.startswith(".git/") for path in allowed):
        raise BuildNextError("selected phase plan overlaps forbidden authority paths")
    return tuple(sorted(allowed)), tuple(sorted(forbidden))


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


def _source_commit(ppe_repo: Path) -> str:
    commit = _git(ppe_repo, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BuildNextError("PPE source commit is not a full SHA")
    dirty = _git(ppe_repo, "status", "--porcelain")
    if dirty:
        raise BuildNextError("PPE source checkout is dirty; cannot pin an exact source identity")
    return commit


def _evidence_identity(
    ppe_repo: Path,
    *,
    source_commit: str,
    snapshot: Mapping[str, Any],
    selected: Mapping[str, Any],
    plan_rel: str,
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
        "ppe_source_commit": source_commit,
        "selected": selected,
        "file_hashes": file_hashes,
        "selection_explanation": snapshot.get("recommended_next_action", {}).get(
            "selection_explanation"
        ),
    }
    return {
        **stable,
        "identity_sha256": _sha256_text(json.dumps(stable, sort_keys=True, separators=(",", ":"))),
    }


def _instruction(
    *,
    pipeline_id: str,
    work: Mapping[str, Any],
    plan_rel: str,
    plan_text: str,
    allowed_paths: Sequence[str],
    forbidden_paths: Sequence[str],
    source_commit: str,
    evidence_identity: Mapping[str, Any],
) -> str:
    return "\n".join(
        [
            "Implementation thread. THREAD_ROLE: codex_build.",
            "",
            "Implement only the bounded PPE/MSOS work item selected by the accepted "
            "founder portfolio registry.",
            f"Pipeline ID: {pipeline_id}",
            f"Work-item ID: {work.get('work_item_id')}",
            "Source repository: DanielTabakman/Probability-prediction-engine",
            f"Exact source commit: {source_commit}",
            f"Registered phase plan: {plan_rel}",
            "",
            "Authority and publication boundary:",
            "- Do not write product main or merge.",
            "- Do not force-push, enable automerge, mark a PR ready, or publish directly.",
            "- Produce only workspace changes for the Autobuilder relay/gate/publisher path.",
            "- Preserve the controlled draft publisher as the only product publisher.",
            "",
            "Allowed paths:",
            *[f"- {path}" for path in allowed_paths],
            "",
            "Forbidden paths:",
            *[f"- {path}" for path in forbidden_paths],
            "",
            "Acceptance criteria and validation requirements:",
            "- Satisfy the registered phase plan and referenced sprint/selection documents.",
            "- Add or update focused tests for changed behavior.",
            "- Run the focused tests and relevant repository gates before closeout.",
            "- Return evidence suitable for the existing relay, candidate gate, revision loop, "
            "and controlled publisher.",
            "",
            "Portfolio-selection evidence identity:",
            json.dumps(dict(evidence_identity), indent=2, sort_keys=True),
            "",
            "Relevant canon/task packet:",
            plan_text.strip(),
            "",
            "Non-goals:",
            "- Do not charter new product scope.",
            "- Do not alter PPE registry or priority policy.",
            "- Do not run continuous refill or dispatch additional work.",
        ]
    )


def _job_id(pipeline_id: str, work_item_id: str, source_commit: str) -> str:
    return _safe_id(f"build-next-{pipeline_id}-{work_item_id}-{source_commit[:12]}")


def _build_job(
    *,
    job_id: str,
    pipeline_id: str,
    work: Mapping[str, Any],
    plan_rel: str,
    plan_text: str,
    allowed_paths: Sequence[str],
    forbidden_paths: Sequence[str],
    source_commit: str,
    evidence_identity: Mapping[str, Any],
    requested_by: str,
) -> dict[str, Any]:
    lane_id = _safe_id(f"{pipeline_id}-{work.get('work_item_id')}", fallback="lane")
    return {
        "version": 1,
        "job_id": job_id,
        "approved": True,
        "publication_enabled": False,
        "requested_by": requested_by,
        "expected_source_head": source_commit,
        "founder_build_next": {
            "version": 1,
            "pipeline_id": pipeline_id,
            "work_item_id": work.get("work_item_id"),
            "repository": "DanielTabakman/Probability-prediction-engine",
            "source_commit": source_commit,
            "phase_plan": plan_rel,
            "portfolio_selection_evidence": dict(evidence_identity),
            "authority": {
                "publication_enabled": False,
                "merge_enabled": False,
                "product_main_write_enabled": False,
            },
        },
        "manifest": {
            "version": 1,
            "publication_enabled": False,
            "lanes": [
                {
                    "task_id": lane_id,
                    "lane_id": lane_id,
                    "chapter_id": _safe_id(str(work.get("work_item_id") or lane_id)).upper(),
                    "branch": f"autobuilder/{job_id}",
                    "layer": "ppe-product",
                    "preferred_cost_class": "standard",
                    "allowed_paths": list(allowed_paths),
                    "forbidden_paths": list(forbidden_paths),
                    "allow_changes": True,
                    "instruction": _instruction(
                        pipeline_id=pipeline_id,
                        work=work,
                        plan_rel=plan_rel,
                        plan_text=plan_text,
                        allowed_paths=allowed_paths,
                        forbidden_paths=forbidden_paths,
                        source_commit=source_commit,
                        evidence_identity=evidence_identity,
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


def _submit_feed_job(config: BuildNextConfig, job: Mapping[str, Any]) -> tuple[str | None, str]:
    job_id = str(job["job_id"])
    text = yaml.safe_dump(dict(job), sort_keys=False, allow_unicode=True)
    parse_host_job(text)
    if not config.submit:
        return None, f"{config.jobs_path}/{job_id}.yaml"
    checkout = _prepare_feed_checkout(config)
    relative = Path(config.jobs_path) / f"{job_id}.yaml"
    destination = checkout / relative
    if destination.exists():
        existing = destination.read_text(encoding="utf-8")
        parse_host_job(existing)
        if existing != text:
            raise BuildNextError(f"approved job {job_id!r} already exists with different content")
        return _git(checkout, "rev-parse", "HEAD"), relative.as_posix()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8", newline="\n")
    _git(checkout, "add", "--", relative.as_posix())
    changed = _run(
        ["git", "-C", str(checkout), "diff", "--cached", "--quiet"],
        accepted=(0, 1),
    ).returncode
    if changed == 0:
        return _git(checkout, "rev-parse", "HEAD"), relative.as_posix()
    _git(checkout, "commit", "-m", f"Queue founder build next job {job_id}")
    commit = _git(checkout, "rev-parse", "HEAD")
    _git(checkout, "push", "origin", f"HEAD:{config.jobs_branch}")
    return commit, relative.as_posix()


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
        snapshot = _collect_snapshot(ppe_repo)
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
            )

        pipeline_id = str(rec.get("pipeline_id") or "")
        work_item_id = str(rec.get("work_item_id") or "")
        if pipeline_id != "ppe":
            raise BuildNextError("v1 can dispatch only PPE/MSOS product jobs")
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
        allowed_paths, forbidden_paths = _phase_plan_paths(plan)
        source_commit = _source_commit(ppe_repo)
        evidence_identity = _evidence_identity(
            ppe_repo,
            source_commit=source_commit,
            snapshot=snapshot,
            selected={"pipeline_id": pipeline_id, "work_item_id": work_item_id, "trace": plan_rel},
            plan_rel=plan_rel,
        )
        job_id = _job_id(pipeline_id, work_item_id, source_commit)
        state = _job_state(config, job_id)
        if state in {"RUNNING", "QUEUED"}:
            return BuildNextReceipt(
                status=state,
                pipeline_id=pipeline_id,
                work_item_id=work_item_id,
                job_id=job_id,
                repository="DanielTabakman/Probability-prediction-engine",
                source_commit=source_commit,
                feed_path=None,
                feed_commit=None,
                message=f"Job {job_id} is already {state.lower()}; no duplicate was submitted.",
                evidence=evidence_identity,
            )
        if state == "BLOCKED":
            raise BuildNextError(f"job {job_id} already completed or failed; refusing redispatch")
        job = _build_job(
            job_id=job_id,
            pipeline_id=pipeline_id,
            work=work,
            plan_rel=plan_rel,
            plan_text=plan_raw,
            allowed_paths=allowed_paths,
            forbidden_paths=forbidden_paths,
            source_commit=source_commit,
            evidence_identity=evidence_identity,
            requested_by=config.requested_by,
        )
        feed_commit, feed_path = _submit_feed_job(config, job)
        return BuildNextReceipt(
            status="QUEUED",
            pipeline_id=pipeline_id,
            work_item_id=work_item_id,
            job_id=job_id,
            repository="DanielTabakman/Probability-prediction-engine",
            source_commit=source_commit,
            feed_path=feed_path,
            feed_commit=feed_commit,
            message=(
                "Submitted one immutable approved build-next job."
                if config.submit
                else "Dry run constructed one immutable approved build-next job."
            ),
            evidence=evidence_identity,
        )
    except BuildNextError as exc:
        return _blocked_receipt(str(exc))


def render_receipt_json(receipt: BuildNextReceipt) -> str:
    return json.dumps(asdict(receipt), indent=2, sort_keys=True) + "\n"
