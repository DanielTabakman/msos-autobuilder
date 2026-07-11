"""Parallel task scheduler with leases, heartbeats, and isolated workspaces."""

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

    def _plan(self, tasks: tuple[BuildTask, ...]) -> tuple[ScheduledTask, ...]:
        if not tasks:
            raise SchedulerError("at least one task is required")
        task_ids = [task.task_id for task in tasks]
        if len(task_ids) != len(set(task_ids)):
            raise SchedulerError("task IDs must be unique")

        assert_lanes_compatible(tuple(task.lane for task in tasks))

        planned = tuple(
            ScheduledTask(
                task=task,
                backend=self.router.select(task),
                workspace=self.workspace_policy.expected_path(task.lane),
            )
            for task in tasks
        )
        self.workspace_policy.assert_unique(tuple(item.workspace for item in planned))
        return planned

    def run(
        self,
        tasks: list[BuildTask] | tuple[BuildTask, ...],
        *,
        owner_id: str,
        lease_ttl_seconds: int = 300,
        heartbeat_interval_seconds: float | None = None,
    ) -> tuple[ExecutionEvidence, ...]:
        scheduled = self._plan(tuple(tasks))
        acquired_lane_ids: list[str] = []
        heartbeat_stop = threading.Event()
        heartbeat_errors: list[BaseException] = []
        heartbeat_thread: threading.Thread | None = None

        try:
            for item in scheduled:
                self.lease_store.acquire(
                    item.task.lane.lane_id,
                    owner_id,
                    lease_ttl_seconds,
                )
                acquired_lane_ids.append(item.task.lane.lane_id)

            interval = heartbeat_interval_seconds
            if interval is None:
                interval = max(0.1, min(30.0, lease_ttl_seconds / 3))
            if interval <= 0:
                raise SchedulerError("heartbeat interval must be positive")

            def heartbeat() -> None:
                while not heartbeat_stop.wait(interval):
                    try:
                        for lane_id in acquired_lane_ids:
                            self.lease_store.renew(
                                lane_id,
                                owner_id,
                                lease_ttl_seconds,
                            )
                    except BaseException as exc:
                        heartbeat_errors.append(exc)
                        heartbeat_stop.set()

            heartbeat_thread = threading.Thread(
                target=heartbeat,
                name="autobuilder-lease-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()

            semaphores: dict[str, threading.Semaphore] = {}
            for item in scheduled:
                backend_id = item.backend.capabilities.backend_id
                semaphores.setdefault(
                    backend_id,
                    threading.Semaphore(item.backend.capabilities.max_concurrency),
                )

            def execute(item: ScheduledTask) -> ExecutionEvidence:
                semaphore = semaphores[item.backend.capabilities.backend_id]
                with semaphore:
                    prepared = item.backend.prepare_workspace(item.task, item.workspace)
                    workspace = self.workspace_policy.validate_backend_path(
                        item.task.lane,
                        prepared,
                    )
                    return item.backend.execute(item.task, workspace)

            with ThreadPoolExecutor(max_workers=len(scheduled)) as executor:
                futures = [executor.submit(execute, item) for item in scheduled]
                evidence = tuple(future.result() for future in futures)

            if heartbeat_errors:
                raise SchedulerError("lane lease heartbeat failed") from heartbeat_errors[0]
            return evidence
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=5)
            for lane_id in reversed(acquired_lane_ids):
                self.lease_store.release(lane_id, owner_id)
