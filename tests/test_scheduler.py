import threading
from pathlib import Path

import pytest

from msos_autobuilder.backends.base import ExecutionEvidence
from msos_autobuilder.lanes import LaneConflictError
from msos_autobuilder.leases import FileLeaseStore
from msos_autobuilder.models import BuildLane, BuildTask, CostClass, WorkerCapabilities
from msos_autobuilder.routing import BackendRouter
from msos_autobuilder.scheduler import ParallelScheduler
from msos_autobuilder.workspaces import WorkspaceIsolationError, WorkspacePolicy


class ConcurrentPlanBackend:
    def __init__(self, workspace_root: Path, parties: int = 2) -> None:
        self.workspace_root = workspace_root
        self.barrier = threading.Barrier(parties)
        self.execute_count = 0
        self._lock = threading.Lock()

    @property
    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            backend_id="concurrent-plan",
            capabilities=frozenset({"plan"}),
            max_concurrency=2,
            cost_class=CostClass.FREE,
            timeout_seconds=30,
        )

    def claim(self, task: BuildTask) -> bool:
        return task.lane.required_capabilities <= self.capabilities.capabilities

    def prepare_workspace(self, task: BuildTask) -> Path:
        return self.workspace_root / task.lane.lane_id

    def execute(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        with self._lock:
            self.execute_count += 1
        self.barrier.wait(timeout=3)
        return ExecutionEvidence(
            task_id=task.task_id,
            backend_id=self.capabilities.backend_id,
            status="planned",
            summary=f"Concurrent plan at {workspace}",
        )

    def collect_evidence(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        raise AssertionError("scheduler uses execute evidence")

    def cancel(self, task: BuildTask) -> None:
        return None


def lane(
    lane_id: str,
    branch: str,
    layer: str,
    *paths: str,
) -> BuildLane:
    return BuildLane(
        lane_id=lane_id,
        chapter_id=f"chapter-{lane_id}",
        branch=branch,
        layer=layer,
        allowed_paths=paths,
        required_capabilities=frozenset({"plan"}),
    )


def scheduler(tmp_path: Path, backend: ConcurrentPlanBackend) -> ParallelScheduler:
    product_root = tmp_path / "product"
    product_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    runtime_root = tmp_path / "runtime"
    policy = WorkspacePolicy(
        product_root=product_root,
        workspace_root=workspace_root,
        runtime_root=runtime_root,
    )
    return ParallelScheduler(
        router=BackendRouter([backend]),
        lease_store=FileLeaseStore(runtime_root),
        workspace_policy=policy,
    )


def test_two_disjoint_lanes_execute_concurrently(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    backend = ConcurrentPlanBackend(workspace_root)
    runtime = scheduler(tmp_path, backend)
    tasks = [
        BuildTask(
            task_id="web-task",
            lane=lane(
                "web",
                "chapter/web",
                "msos-shell",
                "apps/msos-web/**",
            ),
            instruction="Plan web work",
        ),
        BuildTask(
            task_id="core-task",
            lane=lane(
                "core",
                "chapter/core",
                "ppe-core",
                "src/engine/**",
            ),
            instruction="Plan engine work",
        ),
    ]

    evidence = runtime.run(tasks, owner_id="scheduler-1", lease_ttl_seconds=60)

    assert [item.task_id for item in evidence] == ["web-task", "core-task"]
    assert all(item.status == "planned" for item in evidence)
    assert backend.execute_count == 2
    assert list((tmp_path / "runtime").glob("*.json")) == []


def test_overlapping_lanes_fail_before_execution(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    backend = ConcurrentPlanBackend(workspace_root)
    runtime = scheduler(tmp_path, backend)
    tasks = [
        BuildTask(
            task_id="engine-task",
            lane=lane(
                "engine",
                "chapter/engine",
                "ppe-core",
                "src/engine/**",
            ),
            instruction="Plan engine work",
        ),
        BuildTask(
            task_id="engine-child-task",
            lane=lane(
                "engine-child",
                "chapter/engine-child",
                "ppe-core",
                "src/engine/distributions/**",
            ),
            instruction="Plan distribution work",
        ),
    ]

    with pytest.raises(LaneConflictError, match="overlapping ownership"):
        runtime.run(tasks, owner_id="scheduler-1")

    assert backend.execute_count == 0


def test_runtime_and_workspace_roots_must_be_outside_product(tmp_path: Path) -> None:
    product_root = tmp_path / "product"
    product_root.mkdir()

    with pytest.raises(WorkspaceIsolationError, match="workspace root"):
        WorkspacePolicy(
            product_root=product_root,
            workspace_root=product_root / "workspaces",
            runtime_root=tmp_path / "runtime",
        )
