"""Evidence for two concurrent path-scoped worker processes in isolated clones."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .backends.process import LocalProcessBackend
from .leases import FileLeaseStore
from .models import BuildLane, BuildTask, CostClass
from .routing import BackendRouter
from .scheduler import ParallelScheduler
from .workspaces import WorkspacePolicy


@dataclass(frozen=True)
class ProcessLaneEvidence:
    lane_id: str
    workspace: str
    changed_paths: tuple[str, ...]
    clean_source: bool


@dataclass(frozen=True)
class ProcessWitnessReport:
    source: str
    source_head: str
    source_clean_before: bool
    source_clean_after: bool
    lanes: tuple[ProcessLaneEvidence, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_head": self.source_head,
            "source_clean_before": self.source_clean_before,
            "source_clean_after": self.source_clean_after,
            "lanes": [asdict(lane) for lane in self.lanes],
        }


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _lane(lane_id: str, layer: str, allowed_path: str) -> BuildLane:
    return BuildLane(
        lane_id=lane_id,
        chapter_id=f"process-witness-{lane_id}",
        branch=f"shadow/process-witness-{lane_id}",
        layer=layer,
        allowed_paths=(allowed_path,),
        required_capabilities=frozenset({"process", "code", "git-clone"}),
        preferred_cost_class=CostClass.FREE,
    )


def run_process_witness(
    source_repo: str | Path,
    workspace_root: str | Path,
    runtime_root: str | Path,
) -> ProcessWitnessReport:
    source = Path(source_repo).resolve()
    workspaces = Path(workspace_root).resolve()
    runtime = Path(runtime_root).resolve()
    source_head = _git(source, "rev-parse", "HEAD")
    source_clean_before = _git(source, "status", "--porcelain") == ""
    if not source_clean_before:
        raise ValueError("source checkout must be clean before the process witness")

    backend = LocalProcessBackend(
        source,
        (sys.executable, "-m", "msos_autobuilder.witness_worker"),
        backend_id="local-process-witness",
        capabilities=frozenset({"process", "code", "git-clone"}),
        cost_class=CostClass.FREE,
        max_concurrency=2,
        timeout_seconds=30,
        env_allowlist=("PYTHONPATH",),
    )
    scheduler = ParallelScheduler(
        router=BackendRouter([backend]),
        lease_store=FileLeaseStore(runtime),
        workspace_policy=WorkspacePolicy(
            product_root=source,
            workspace_root=workspaces,
            runtime_root=runtime,
        ),
    )
    barrier = runtime / "process-witness-barrier"
    definitions = (
        (
            "msos-web",
            "msos-shell",
            "apps/msos-web/**",
            "apps/msos-web/.autobuilder-process-witness",
        ),
        (
            "ppe-core",
            "ppe-core",
            "src/engine/**",
            "src/engine/.autobuilder-process-witness",
        ),
    )
    tasks: list[BuildTask] = []
    for lane_id, layer, allowed_path, target in definitions:
        instruction = json.dumps(
            {
                "lane_id": lane_id,
                "target": target,
                "content": f"witness:{lane_id}\n",
                "barrier_dir": str(barrier),
                "parties": len(definitions),
            }
        )
        tasks.append(
            BuildTask(
                task_id=f"{lane_id}-process",
                lane=_lane(lane_id, layer, allowed_path),
                instruction=instruction,
            )
        )

    evidence = scheduler.run(tasks, owner_id="msos-process-witness")
    lanes = tuple(
        ProcessLaneEvidence(
            lane_id=task.lane.lane_id,
            workspace=str(workspaces / task.lane.lane_id),
            changed_paths=result.changed_paths,
            clean_source=_git(source, "status", "--porcelain") == "",
        )
        for task, result in zip(tasks, evidence, strict=True)
    )
    return ProcessWitnessReport(
        source=str(source),
        source_head=source_head,
        source_clean_before=source_clean_before,
        source_clean_after=_git(source, "status", "--porcelain") == "",
        lanes=lanes,
    )


def render_process_witness_json(report: ProcessWitnessReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
