"""Reference backend that proves routing without executing or writing."""

from __future__ import annotations

from pathlib import Path

from ..models import BuildTask, CostClass, WorkerCapabilities
from .base import ExecutionEvidence


class PlanOnlyBackend:
    """A safe bootstrap backend with no command execution or publication authority."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root

    @property
    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            backend_id="plan-only",
            capabilities=frozenset({"plan", "contract-validation"}),
            max_concurrency=32,
            cost_class=CostClass.FREE,
            timeout_seconds=60,
        )

    def claim(self, task: BuildTask) -> bool:
        return task.lane.required_capabilities <= self.capabilities.capabilities

    def prepare_workspace(self, task: BuildTask) -> Path:
        return self.workspace_root / task.lane.lane_id

    def execute(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        return ExecutionEvidence(
            task_id=task.task_id,
            backend_id=self.capabilities.backend_id,
            status="planned",
            summary=f"Read-only plan for {task.lane.chapter_id} at {workspace}",
        )

    def collect_evidence(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        return self.execute(task, workspace)

    def cancel(self, task: BuildTask) -> None:
        return None
