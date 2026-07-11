"""Parallel task scheduler with leases, isolated workspaces, and backend limits."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .backends.base import ExecutionEvidence, WorkerBackend
from .lanes import assert_lanes_compatible
from .leases import FileLeaseStore
from .models import BuildTask
from .routing import BackendRouter
from .workspaces import WorkspacePolicy


class SchedulerError(RuntimeError):
    """Raised when a parallel run cannot be safely prepared or completed."""


@dataclass(frozen=True)
class ScheduledTask:
    task: BuildTask
    backend: WorkerBackend
    workspace: Path


class ParallelScheduler:
    def __init__(
        self,
        *,
        router: BackendRouter,
        lease_store: FileLeaseStore,
        workspace_policy: WorkspacePolicy,
    ) -> None:
        if lease_store.root != workspace_policy.runtime_root:
            raise SchedulerError("lease store root must equal the workspace policy runtime root")
        self.router = router
        self.lease_store = lease_store
        self.workspace_policy = workspace_policy

    def _prepare(self, tasks: tuple[BuildTask, ...]) -> tuple[ScheduledTask, ...]:
        if not tasks:
            raise SchedulerError("at least one task is required")
        task_ids = [task.task_id for task in tasks]
        if len(task_ids) != len(set(task_ids)):
            raise SchedulerError("task IDs must be unique")

        assert_lanes_compatible(tuple(task.lane for task in tasks))

        prepared: list[ScheduledTask] = []
        workspace_paths: list[Path] = []
        for task in tasks:
            backend = self.router.select(task)
            proposed = backend.prepare_workspace(task)
            workspace = self.workspace_policy.validate_backend_path(task.lane, proposed)
            workspace_paths.append(workspace)
            prepared.append(
                ScheduledTask(
                    task=task,
                    backend=backend,
                    workspace=workspace,
                )
            )
        self.workspace_policy.assert_unique(tuple(workspace_paths))
        return tuple(prepared)

    def run(
        self,
        tasks: list[BuildTask] | tuple[BuildTask, ...],
        *,
        owner_id: str,
        lease_ttl_seconds: int = 300,
    ) -> tuple[ExecutionEvidence, ...]:
        scheduled = self._prepare(tuple(tasks))
        acquired_lane_ids: list[str] = []

        try:
            for item in scheduled:
                self.lease_store.acquire(
                    item.task.lane.lane_id,
                    owner_id,
                    lease_ttl_seconds,
                )
                acquired_lane_ids.append(item.task.lane.lane_id)

            semaphores = {
                item.backend.capabilities.backend_id: threading.Semaphore(
                    item.backend.capabilities.max_concurrency
                )
                for item in scheduled
            }

            def execute(item: ScheduledTask) -> ExecutionEvidence:
                semaphore = semaphores[item.backend.capabilities.backend_id]
                with semaphore:
                    return item.backend.execute(item.task, item.workspace)

            with ThreadPoolExecutor(max_workers=len(scheduled)) as executor:
                futures = [executor.submit(execute, item) for item in scheduled]
                return tuple(future.result() for future in futures)
        finally:
            for lane_id in reversed(acquired_lane_ids):
                self.lease_store.release(lane_id, owner_id)
