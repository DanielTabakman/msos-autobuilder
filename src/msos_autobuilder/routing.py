"""Capability and cost-aware worker backend routing."""

from __future__ import annotations

from collections.abc import Iterable

from .backends.base import WorkerBackend
from .models import BuildTask, CostClass


class BackendRoutingError(RuntimeError):
    """Raised when tasks cannot be safely assigned to a worker backend."""


_COST_RANK = {
    CostClass.FREE: 0,
    CostClass.LOW: 1,
    CostClass.STANDARD: 2,
    CostClass.PREMIUM: 3,
}


class BackendRouter:
    def __init__(self, backends: Iterable[WorkerBackend]) -> None:
        self.backends = tuple(backends)
        if not self.backends:
            raise BackendRoutingError("at least one backend is required")
        backend_ids = [backend.capabilities.backend_id for backend in self.backends]
        if len(backend_ids) != len(set(backend_ids)):
            raise BackendRoutingError("backend IDs must be unique")

    def select(self, task: BuildTask) -> WorkerBackend:
        compatible = [
            backend
            for backend in self.backends
            if task.lane.required_capabilities <= backend.capabilities.capabilities
            and backend.claim(task)
        ]
        if not compatible:
            required = sorted(task.lane.required_capabilities)
            raise BackendRoutingError(
                f"no backend supports task {task.task_id!r} capabilities {required}"
            )

        preferred_rank = _COST_RANK[task.lane.preferred_cost_class]

        def sort_key(backend: WorkerBackend) -> tuple[bool, int, str]:
            cost_rank = _COST_RANK[backend.capabilities.cost_class]
            exceeds_preference = cost_rank > preferred_rank
            return exceeds_preference, cost_rank, backend.capabilities.backend_id

        return min(compatible, key=sort_key)
