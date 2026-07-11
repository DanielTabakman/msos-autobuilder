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

    def prepare_workspace(self, task: BuildTask, workspace: Path) -> Path:
        return workspace

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


def lane(lane_id: str, branch: str, layer: str, *paths: str) -> BuildLane:
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
    return ParallelScheduler(
        router=BackendRouter([backend]),
        lease_store=FileLeaseStore(runtime_root),
        workspace_policy=WorkspacePolicy(
            product_root=product_root,
            workspace_root=workspace_root,
            runtime_root=runtime_root,
        ),
    )


def test_two_disjoint_lanes_execute_concurrently(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    backend = ConcurrentPlanBackend(workspace_root)
    runtime = scheduler(tmp_path, backend)
    tasks = [
        BuildTask(
            task_id="web-task",
            lane=lane("web", "chapter/web", "msos-shell", "apps/msos-web/**"),
            instruction="Plan web work",
        ),
        BuildTask(
            task_id="core-task",
            lane=lane("core", "chapter/core", "ppe-core", "src/engine/**"),
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
            lane=lane("engine", "chapter/engine", "ppe-core", "src/engine/**"),
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


def test_scheduler_renews_leases_during_execution(tmp_path: Path) -> None:
    class RecordingLeaseStore(FileLeaseStore):
        def __init__(self, runtime_root: Path) -> None:
            super().__init__(runtime_root)
            self.renew_count = 0

        def renew(self, lane_id, owner_id, ttl_seconds, *, now=None):
            self.renew_count += 1
            return super().renew(lane_id, owner_id, ttl_seconds, now=now)

    class SlowBackend(ConcurrentPlanBackend):
        def __init__(self, workspace_root: Path) -> None:
            super().__init__(workspace_root, parties=1)
            self.entered = threading.Event()
            self.release = threading.Event()

        def execute(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
            self.entered.set()
            assert self.release.wait(timeout=3)
            return ExecutionEvidence(
                task_id=task.task_id,
                backend_id=self.capabilities.backend_id,
                status="planned",
                summary="slow plan",
            )

    product_root = tmp_path / "product"
    product_root.mkdir()
    workspace_root = tmp_path / "workspaces"
    runtime_root = tmp_path / "runtime"
    backend = SlowBackend(workspace_root)
    store = RecordingLeaseStore(runtime_root)
    runtime = ParallelScheduler(
        router=BackendRouter([backend]),
        lease_store=store,
        workspace_policy=WorkspacePolicy(
            product_root=product_root,
            workspace_root=workspace_root,
            runtime_root=runtime_root,
        ),
    )
    build_task = BuildTask(
        task_id="slow-task",
        lane=lane("slow", "chapter/slow", "msos-shell", "apps/msos-web/**"),
        instruction="Plan slowly",
    )
    result: list[tuple[ExecutionEvidence, ...]] = []

    def run() -> None:
        result.append(
            runtime.run(
                [build_task],
                owner_id="scheduler-1",
                lease_ttl_seconds=1,
                heartbeat_interval_seconds=0.05,
            )
        )

    thread = threading.Thread(target=run)
    thread.start()
    assert backend.entered.wait(timeout=3)
    assert threading.Event().wait(0.15) is False
    backend.release.set()
    thread.join(timeout=3)

    assert result[0][0].task_id == "slow-task"
    assert store.renew_count >= 1
