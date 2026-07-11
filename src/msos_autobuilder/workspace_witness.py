"""Read-only evidence for two isolated workspaces from one product checkout."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .backends.git_clone import GitWorkspaceError, LocalGitCloneBackend
from .leases import FileLeaseStore
from .models import BuildLane, BuildTask
from .routing import BackendRouter
from .scheduler import ParallelScheduler
from .workspaces import WorkspacePolicy


@dataclass(frozen=True)
class LaneWorkspaceEvidence:
    lane_id: str
    task_id: str
    backend_id: str
    workspace: str
    head: str
    clean: bool


@dataclass(frozen=True)
class WorkspaceWitnessReport:
    source: str
    source_head: str
    source_clean_before: bool
    source_clean_after: bool
    lanes: tuple[LaneWorkspaceEvidence, ...]

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
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git command failed").strip()
        raise GitWorkspaceError(detail)
    return proc.stdout.strip()


def _lane(lane_id: str, layer: str, path: str) -> BuildLane:
    return BuildLane(
        lane_id=lane_id,
        chapter_id=f"workspace-witness-{lane_id}",
        branch=f"shadow/workspace-witness-{lane_id}",
        layer=layer,
        allowed_paths=(path,),
        required_capabilities=frozenset({"git-clone", "read-only"}),
    )


def run_workspace_witness(
    source_repo: str | Path,
    workspace_root: str | Path,
    runtime_root: str | Path,
) -> WorkspaceWitnessReport:
    source = Path(source_repo).resolve()
    source_head = _git(source, "rev-parse", "HEAD")
    source_clean_before = _git(source, "status", "--porcelain") == ""
    if not source_clean_before:
        raise GitWorkspaceError("source checkout must be clean before the witness")

    backend = LocalGitCloneBackend(source)
    scheduler = ParallelScheduler(
        router=BackendRouter([backend]),
        lease_store=FileLeaseStore(runtime_root),
        workspace_policy=WorkspacePolicy(
            product_root=source,
            workspace_root=Path(workspace_root),
            runtime_root=Path(runtime_root),
        ),
    )
    tasks = [
        BuildTask(
            task_id="msos-web-workspace",
            lane=_lane("msos-web", "msos-shell", "apps/msos-web/**"),
            instruction="Materialize a read-only MSOS web workspace",
        ),
        BuildTask(
            task_id="ppe-core-workspace",
            lane=_lane("ppe-core", "ppe-core", "src/engine/**"),
            instruction="Materialize a read-only PPE core workspace",
        ),
    ]
    execution = scheduler.run(tasks, owner_id="msos-workspace-witness")

    lanes: list[LaneWorkspaceEvidence] = []
    for task, evidence in zip(tasks, execution, strict=True):
        metadata = dict(evidence.metadata)
        workspace = Path(metadata["workspace"])
        lanes.append(
            LaneWorkspaceEvidence(
                lane_id=task.lane.lane_id,
                task_id=task.task_id,
                backend_id=evidence.backend_id,
                workspace=str(workspace),
                head=metadata["head"],
                clean=_git(workspace, "status", "--porcelain") == "",
            )
        )

    source_clean_after = _git(source, "status", "--porcelain") == ""
    return WorkspaceWitnessReport(
        source=str(source),
        source_head=source_head,
        source_clean_before=source_clean_before,
        source_clean_after=source_clean_after,
        lanes=tuple(lanes),
    )


def render_workspace_witness_json(report: WorkspaceWitnessReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"
