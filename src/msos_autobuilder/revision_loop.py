"""Generate bounded Codex revision jobs from failed candidate-gate evidence.

This service is factory-infrastructure only. It reads immutable review evidence from the
``results`` branch and may write approved, publication-disabled manifests only to the
``jobs`` branch. It never accesses or writes a product repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .service_error_lifecycle import record_service_cycle_success, write_service_error_marker


class RevisionLoopError(RuntimeError):
    """Raised when gate evidence or revision output violates the contract."""


@dataclass(frozen=True)
class RevisionPlan:
    revision_job_prefix: str
    target_task_ids: tuple[str, ...]
    instruction_prefix: str = ""


@dataclass(frozen=True)
class RevisionLoopConfig:
    host_root: Path
    repo_url: str
    results_branch: str = "results"
    jobs_branch: str = "jobs"
    jobs_path: str = "jobs/approved"
    machine_id: str = ""
    poll_seconds: float = 30.0
    max_revision_depth: int = 3
    plans: Mapping[str, RevisionPlan] | None = None

    def __post_init__(self) -> None:
        if not self.repo_url.strip():
            raise ValueError("repo_url is required")
        if self.results_branch in {"main", "master", self.jobs_branch}:
            raise ValueError("results_branch must be a dedicated review branch")
        if self.jobs_branch in {"main", "master", self.results_branch}:
            raise ValueError("jobs_branch must be a dedicated job-feed branch")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.max_revision_depth <= 0:
            raise ValueError("max_revision_depth must be positive")
        path = Path(self.jobs_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("jobs_path must be a safe relative path")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_segment(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip(".-")
    return cleaned[:96] or fallback


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RevisionLoopError(f"{label} must be a mapping")
    return value


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
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


def _run_git(
    repo: Path | None,
    *args: str,
    accepted: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    command = ["git"]
    if repo is not None:
        command.extend(["-C", str(repo)])
    command.extend(args)
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        check=False,
    )
    if proc.returncode not in accepted:
        detail = (proc.stderr or proc.stdout or "git command failed").strip()
        raise RevisionLoopError(detail)
    return proc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounded(value: Any, limit: int = 12_000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[-limit:]


def _safe_relative(value: Any, label: str) -> str:
    text = str(value or "").strip()
    path = Path(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise RevisionLoopError(f"{label} must be a safe relative path")
    return path.as_posix()


def load_revision_loop_config(path: str | Path) -> RevisionLoopConfig:
    config_path = Path(path).expanduser().resolve()
    root = _mapping(yaml.safe_load(config_path.read_text(encoding="utf-8")), "revision config")
    if root.get("version") != 1:
        raise RevisionLoopError("only revision-loop config version 1 is supported")
    if root.get("publication_enabled", False) is not False:
        raise RevisionLoopError("revision-loop publication must remain disabled")

    plans_raw = _mapping(root.get("plans"), "plans")
    plans: dict[str, RevisionPlan] = {}
    for raw_root_id, raw_plan in plans_raw.items():
        root_id = _safe_segment(str(raw_root_id), fallback="job")
        if root_id != str(raw_root_id):
            raise RevisionLoopError(f"unsafe plan job ID: {raw_root_id!r}")
        plan = _mapping(raw_plan, f"plan {root_id}")
        prefix = _safe_segment(str(plan.get("revision_job_prefix") or ""), fallback="revision")
        task_ids_raw = plan.get("target_task_ids")
        if not isinstance(task_ids_raw, list) or not task_ids_raw:
            raise RevisionLoopError(f"plan {root_id} requires target_task_ids")
        task_ids = tuple(_safe_segment(str(item), fallback="task") for item in task_ids_raw)
        plans[root_id] = RevisionPlan(
            revision_job_prefix=prefix,
            target_task_ids=task_ids,
            instruction_prefix=str(plan.get("instruction_prefix") or "").strip(),
        )

    host_root = Path(str(root.get("host_root") or "")).expanduser()
    if not host_root.is_absolute():
        host_root = (config_path.parent / host_root).resolve()
    else:
        host_root = host_root.resolve()

    return RevisionLoopConfig(
        host_root=host_root,
        repo_url=str(root.get("repo_url") or "").strip(),
        results_branch=str(root.get("results_branch") or "results").strip(),
        jobs_branch=str(root.get("jobs_branch") or "jobs").strip(),
        jobs_path=_safe_relative(root.get("jobs_path", "jobs/approved"), "jobs_path"),
        machine_id=_safe_segment(
            str(root.get("machine_id") or socket.gethostname()),
            fallback="windows-host",
        ),
        poll_seconds=float(root.get("poll_seconds", 30.0)),
        max_revision_depth=int(root.get("max_revision_depth", 3)),
        plans=plans,
    )


def _revision_depth(job_id: str, plan: RevisionPlan) -> int:
    if job_id == plan.revision_job_prefix:
        return 0
    match = re.fullmatch(re.escape(plan.revision_job_prefix) + r"-revision-(\d+)", job_id)
    if match:
        return int(match.group(1))
    return 0


def _find_plan(
    job_id: str,
    plans: Mapping[str, RevisionPlan],
) -> tuple[str, RevisionPlan] | None:
    if job_id in plans:
        return job_id, plans[job_id]
    for root_id, plan in plans.items():
        if re.fullmatch(re.escape(plan.revision_job_prefix) + r"-revision-\d+", job_id):
            return root_id, plan
    return None


def _failed_evidence(gate_report: Mapping[str, Any]) -> str:
    checks = gate_report.get("checks")
    if not isinstance(checks, list):
        raise RevisionLoopError("gate report checks must be a list")
    failed_checks = [item for item in checks if isinstance(item, dict) and item.get("passed") is not True]
    policy_blocks = gate_report.get("policy_blocks", [])
    errors = gate_report.get("errors", [])
    if not isinstance(policy_blocks, list) or not isinstance(errors, list):
        raise RevisionLoopError("gate report blockers/errors must be lists")

    sections: list[str] = []
    for check in failed_checks:
        sections.append(
            "\n".join(
                [
                    f"Failed check: {check.get('name')}",
                    f"Command: {json.dumps(check.get('argv', []))}",
                    f"Return code: {check.get('returncode')}",
                    f"Timed out: {bool(check.get('timed_out'))}",
                    f"stdout:\n{_bounded(check.get('stdout'))}",
                    f"stderr:\n{_bounded(check.get('stderr'))}",
                ]
            )
        )
    if policy_blocks:
        sections.append("Policy blockers:\n" + "\n".join(f"- {item}" for item in policy_blocks))
    if errors:
        sections.append("Gate errors:\n" + json.dumps(errors, indent=2, sort_keys=True))
    if not sections:
        raise RevisionLoopError("failed gate report contains no actionable failure evidence")
    return "\n\n".join(sections)


def _select_lanes(job_yaml: Mapping[str, Any], plan: RevisionPlan) -> tuple[dict[str, Any], ...]:
    manifest = _mapping(job_yaml.get("manifest"), "job manifest")
    lanes = manifest.get("lanes")
    if not isinstance(lanes, list):
        raise RevisionLoopError("job manifest lanes must be a list")
    by_task: dict[str, dict[str, Any]] = {}
    for raw_lane in lanes:
        lane = _mapping(raw_lane, "job lane")
        task_id = str(lane.get("task_id") or "").strip()
        if task_id:
            by_task[task_id] = lane
    missing = [task_id for task_id in plan.target_task_ids if task_id not in by_task]
    if missing:
        raise RevisionLoopError(f"revision target task(s) missing from source job: {missing}")
    return tuple(by_task[task_id] for task_id in plan.target_task_ids)


def build_revision_manifest(
    gate_report: Mapping[str, Any],
    job_yaml: Mapping[str, Any],
    plan: RevisionPlan,
    *,
    max_revision_depth: int,
) -> dict[str, Any]:
    """Build one deterministic, publication-disabled correction manifest."""

    if gate_report.get("status") != "failed":
        raise RevisionLoopError("only failed gate reports may create revision jobs")
    if gate_report.get("publication_enabled", False) is not False:
        raise RevisionLoopError("gate report publication must remain disabled")
    if gate_report.get("product_write_performed", False) is not False:
        raise RevisionLoopError("refusing revision after a reported product write")

    source_job_id = _safe_segment(str(gate_report.get("job_id") or ""), fallback="job")
    source_head = str(gate_report.get("source_head") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", source_head):
        raise RevisionLoopError("gate report is missing a full source_head SHA")
    source_report_sha = str(gate_report.get("source_report_sha256") or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{64}", source_report_sha):
        raise RevisionLoopError("gate report is missing source_report_sha256")

    depth = _revision_depth(source_job_id, plan) + 1
    if depth > max_revision_depth:
        raise RevisionLoopError(
            f"maximum revision depth exceeded for {source_job_id}: {depth}>{max_revision_depth}"
        )
    revision_job_id = f"{plan.revision_job_prefix}-revision-{depth}"
    timestamp = str(gate_report.get("finished_at") or gate_report.get("started_at") or "").strip()
    if not timestamp:
        raise RevisionLoopError("gate report is missing a deterministic timestamp")

    evidence = _failed_evidence(gate_report)
    patches = gate_report.get("patches", [])
    patch_hashes = []
    if isinstance(patches, list):
        for item in patches:
            if isinstance(item, dict):
                patch_hashes.append(
                    {
                        "task_id": item.get("task_id"),
                        "patch_sha256": item.get("patch_sha256"),
                        "changed_paths": item.get("changed_paths", []),
                    }
                )

    revision_lanes: list[dict[str, Any]] = []
    for source_lane in _select_lanes(job_yaml, plan):
        source_task_id = _safe_segment(str(source_lane.get("task_id") or ""), fallback="task")
        original_instruction = str(source_lane.get("instruction") or "").strip()
        instruction_parts = [
            plan.instruction_prefix,
            "Correct the failed candidate described below. Work from the clean source checkout; "
            "do not assume any prior patch has been applied.",
            f"Source gate job: {source_job_id}",
            f"Source report SHA-256: {source_report_sha}",
            "Patch evidence:\n" + json.dumps(patch_hashes, indent=2, sort_keys=True),
            "Exact gate failure evidence:\n" + evidence,
            "Original task instruction:\n" + original_instruction,
            "Preserve the original allowed/forbidden path boundaries. Add or update focused tests. "
            "Do not commit, push, publish, or open a pull request.",
        ]
        revision_lanes.append(
            {
                "task_id": f"{source_task_id}-revision-{depth}",
                "lane_id": f"{_safe_segment(str(source_lane.get('lane_id') or source_task_id), fallback='lane')}-revision-{depth}",
                "chapter_id": f"{_safe_segment(str(source_lane.get('chapter_id') or source_task_id), fallback='chapter')}-REVISION-{depth}",
                "branch": f"shadow/{revision_job_id}-{source_task_id}",
                "layer": source_lane.get("layer"),
                "preferred_cost_class": source_lane.get("preferred_cost_class", "standard"),
                "allowed_paths": list(source_lane.get("allowed_paths", [])),
                "forbidden_paths": list(source_lane.get("forbidden_paths", [])),
                "allow_changes": True,
                "instruction": "\n\n".join(part for part in instruction_parts if part),
            }
        )

    return {
        "version": 1,
        "job_id": revision_job_id,
        "approved": True,
        "publication_enabled": False,
        "requested_by": "MSOS Autobuilder automatic revision loop",
        "submitted_at": timestamp,
        "approved_at": timestamp,
        "expected_source_head": source_head,
        "revision": {
            "version": 1,
            "depth": depth,
            "source_job_id": source_job_id,
            "source_report_sha256": source_report_sha,
        },
        "manifest": {
            "version": 1,
            "publication_enabled": False,
            "lanes": revision_lanes,
        },
    }


class BranchCheckout:
    def __init__(self, root: Path, repo_url: str, branch: str, *, writable: bool) -> None:
        self.root = root
        self.repo_url = repo_url
        self.branch = branch
        self.writable = writable

    def prepare(self) -> None:
        if not (self.root / ".git").exists():
            if self.root.exists():
                shutil.rmtree(self.root)
            self.root.parent.mkdir(parents=True, exist_ok=True)
            _run_git(
                None,
                "-c",
                "core.autocrlf=false",
                "clone",
                "--single-branch",
                "--branch",
                self.branch,
                "--no-tags",
                self.repo_url,
                str(self.root),
            )
        else:
            _run_git(self.root, "config", "core.autocrlf", "false")
            _run_git(self.root, "fetch", "--no-tags", "origin", self.branch)
            _run_git(self.root, "checkout", "-B", self.branch, f"origin/{self.branch}")
            _run_git(self.root, "reset", "--hard", f"origin/{self.branch}")
            _run_git(self.root, "clean", "-fd")
        _run_git(self.root, "config", "core.autocrlf", "false")
        if self.writable:
            _run_git(self.root, "config", "user.name", "MSOS Autobuilder Revision Loop")
            _run_git(self.root, "config", "user.email", "autobuilder-revision@localhost")


class RevisionLoop:
    def __init__(self, config: RevisionLoopConfig) -> None:
        self.config = config
        self.host_root = config.host_root.expanduser().resolve()
        self.state = self.host_root / "state"
        self.ledger_path = self.state / "revision-loop-seen.json"
        self.results = BranchCheckout(
            self.state / "revision-loop-results-repo",
            config.repo_url,
            config.results_branch,
            writable=False,
        )
        self.jobs = BranchCheckout(
            self.state / "revision-loop-jobs-repo",
            config.repo_url,
            config.jobs_branch,
            writable=True,
        )
        self._last_error_marker_written = False

    def _load_ledger(self) -> dict[str, dict[str, str]]:
        if not self.ledger_path.exists():
            return {}
        raw = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise RevisionLoopError("revision-loop ledger must be a mapping")
        result: dict[str, dict[str, str]] = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise RevisionLoopError("revision-loop ledger entries are invalid")
            gate_sha = value.get("gate_report_sha256")
            revision_job_id = value.get("revision_job_id")
            jobs_commit = value.get("jobs_commit")
            if not all(isinstance(item, str) for item in (gate_sha, revision_job_id, jobs_commit)):
                raise RevisionLoopError("revision-loop ledger entry is incomplete")
            result[key] = dict(value)
        return result

    def _save_ledger(self, ledger: Mapping[str, Any]) -> None:
        _atomic_write_json(self.ledger_path, ledger)

    def _publish(self, manifest: Mapping[str, Any]) -> str:
        job_id = _safe_segment(str(manifest.get("job_id") or ""), fallback="job")
        relative = Path(self.config.jobs_path) / f"{job_id}.yaml"
        destination = self.jobs.root / relative
        text = yaml.safe_dump(dict(manifest), sort_keys=False, allow_unicode=True)
        if destination.exists():
            existing = destination.read_text(encoding="utf-8")
            if existing != text:
                raise RevisionLoopError(f"revision job already exists with different content: {job_id}")
            return _run_git(self.jobs.root, "rev-parse", "HEAD").stdout.strip()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8", newline="\n")
        _run_git(self.jobs.root, "add", "--", relative.as_posix())
        _run_git(self.jobs.root, "commit", "-m", f"Queue automatic revision job {job_id}")
        commit = _run_git(self.jobs.root, "rev-parse", "HEAD").stdout.strip()
        _run_git(self.jobs.root, "push", "origin", f"HEAD:{self.config.jobs_branch}")
        return commit

    def run_once(self) -> tuple[str, ...]:
        self._last_error_marker_written = False
        self.state.mkdir(parents=True, exist_ok=True)
        cycle_started_at = _utc_now()
        self.results.prepare()
        self.jobs.prepare()
        ledger = self._load_ledger()
        processed: list[str] = []
        root = self.results.root / "results" / self.config.machine_id
        plans = self.config.plans or {}
        job_dirs = sorted(path for path in root.iterdir() if path.is_dir()) if root.exists() else []
        for job_dir in job_dirs:
            gate_path = job_dir / "gate-report.json"
            job_path = job_dir / "job.yaml"
            if not gate_path.exists() or not job_path.exists():
                continue
            gate_report = json.loads(gate_path.read_text(encoding="utf-8"))
            if not isinstance(gate_report, dict) or gate_report.get("status") != "failed":
                continue
            job_id = _safe_segment(str(gate_report.get("job_id") or job_dir.name), fallback="job")
            matched = _find_plan(job_id, plans)
            if matched is None:
                continue
            _, plan = matched
            gate_sha = _sha256_file(gate_path)
            ledger_key = f"{self.config.machine_id}/{job_id}"
            associated = {
                "job_id": job_id,
                "source_job_id": job_id,
                "machine_id": self.config.machine_id,
                "repository": self.config.repo_url,
            }
            previous = ledger.get(ledger_key)
            if previous:
                if previous["gate_report_sha256"] != gate_sha:
                    exc = RevisionLoopError(
                        f"gate report changed after revision processing: {job_id}"
                    )
                    self._write_error_marker(exc, associated=associated)
                    raise exc
                continue
            try:
                job_yaml = yaml.safe_load(job_path.read_text(encoding="utf-8"))
                manifest = build_revision_manifest(
                    gate_report,
                    _mapping(job_yaml, "job.yaml"),
                    plan,
                    max_revision_depth=self.config.max_revision_depth,
                )
                associated = {
                    **associated,
                    "revision_job_id": str(manifest["job_id"]),
                }
                commit = self._publish(manifest)
                ledger[ledger_key] = {
                    "gate_report_sha256": gate_sha,
                    "revision_job_id": str(manifest["job_id"]),
                    "jobs_commit": commit,
                    "queued_at": _utc_now(),
                    "source_job_id": job_id,
                }
                self._save_ledger(ledger)
                processed.append(str(manifest["job_id"]))
            except (RevisionLoopError, OSError, ValueError, yaml.YAMLError) as exc:
                self._write_error_marker(exc, associated=associated)
                raise
        record_service_cycle_success(
            state_root=self.state,
            host_root=self.host_root,
            service="revision",
            cycle_started_at=cycle_started_at,
            associated_jobs=processed,
            terminal_evidence={"revision_jobs": processed},
        )
        return tuple(processed)

    def _write_error_marker(
        self,
        exc: BaseException,
        *,
        associated: Mapping[str, Any] | None = None,
    ) -> None:
        write_service_error_marker(
            state_root=self.state,
            host_root=self.host_root,
            service="revision",
            marker_name="revision-loop-error.json",
            error_type=type(exc).__name__,
            message=str(exc),
            associated=associated,
            extra={"publication_enabled": False},
            exception=exc,
        )
        self._last_error_marker_written = True

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except (RevisionLoopError, json.JSONDecodeError, OSError, ValueError) as exc:
                if not self._last_error_marker_written:
                    self._write_error_marker(exc, associated={"scope": "global"})
            time.sleep(self.config.poll_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="msos-autobuilder-revision-loop")
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    loop = RevisionLoop(load_revision_loop_config(args.config))
    if args.once:
        processed = loop.run_once()
        print(
            json.dumps(
                {
                    "status": "completed",
                    "revision_jobs": list(processed),
                    "publication_enabled": False,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    loop.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
