"""Read-only local Git clone backend for isolated lane workspace witnesses."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..models import BuildTask, CostClass, WorkerCapabilities
from .base import ExecutionEvidence


class GitWorkspaceError(RuntimeError):
    """Raised when a clean isolated Git workspace cannot be prepared or verified."""


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


class LocalGitCloneBackend:
    """Clone a local source checkout without modifying it, then emit clean evidence."""

    def __init__(self, source_repo: str | Path, *, backend_id: str = "local-git-clone") -> None:
        self.source_repo = Path(source_repo).resolve()
        self.backend_id = backend_id
        if not (self.source_repo / ".git").exists():
            raise GitWorkspaceError(f"source is not a Git checkout: {self.source_repo}")

    @property
    def capabilities(self) -> WorkerCapabilities:
        return WorkerCapabilities(
            backend_id=self.backend_id,
            capabilities=frozenset({"plan", "git-clone", "read-only"}),
            max_concurrency=4,
            cost_class=CostClass.FREE,
            timeout_seconds=300,
        )

    def claim(self, task: BuildTask) -> bool:
        return task.lane.required_capabilities <= self.capabilities.capabilities

    def prepare_workspace(self, task: BuildTask, workspace: Path) -> Path:
        del task
        workspace = workspace.resolve()
        if workspace.exists():
            if any(workspace.iterdir()):
                raise GitWorkspaceError(f"workspace is not empty: {workspace}")
            workspace.rmdir()
        workspace.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [
                "git",
                "clone",
                "--local",
                "--no-hardlinks",
                "--no-tags",
                str(self.source_repo),
                str(workspace),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            shutil.rmtree(workspace, ignore_errors=True)
            detail = (proc.stderr or proc.stdout or "git clone failed").strip()
            raise GitWorkspaceError(detail)
        return workspace

    def execute(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        source_head = _git(self.source_repo, "rev-parse", "HEAD")
        workspace_head = _git(workspace, "rev-parse", "HEAD")
        source_status = _git(self.source_repo, "status", "--porcelain")
        workspace_status = _git(workspace, "status", "--porcelain")
        if source_head != workspace_head:
            raise GitWorkspaceError("workspace HEAD does not match source HEAD")
        if source_status or workspace_status:
            raise GitWorkspaceError("source and workspace must remain clean")
        return ExecutionEvidence(
            task_id=task.task_id,
            backend_id=self.capabilities.backend_id,
            status="workspace-ready",
            summary=f"Clean isolated clone for {task.lane.chapter_id}",
            metadata=(("head", workspace_head), ("workspace", str(workspace))),
        )

    def collect_evidence(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        return self.execute(task, workspace)

    def cancel(self, task: BuildTask) -> None:
        del task
