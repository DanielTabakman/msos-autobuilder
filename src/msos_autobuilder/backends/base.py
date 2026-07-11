"""Provider-neutral worker backend protocol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..models import BuildTask, WorkerCapabilities


@dataclass(frozen=True)
class ExecutionEvidence:
    task_id: str
    backend_id: str
    status: str
    summary: str
    changed_paths: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()


class WorkerBackend(Protocol):
    @property
    def capabilities(self) -> WorkerCapabilities: ...

    def claim(self, task: BuildTask) -> bool: ...

    def prepare_workspace(self, task: BuildTask, workspace: Path) -> Path: ...

    def execute(self, task: BuildTask, workspace: Path) -> ExecutionEvidence: ...

    def collect_evidence(self, task: BuildTask, workspace: Path) -> ExecutionEvidence: ...

    def cancel(self, task: BuildTask) -> None: ...
