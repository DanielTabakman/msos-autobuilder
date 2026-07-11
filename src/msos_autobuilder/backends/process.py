"""Provider-neutral local process worker with path-scoped Git evidence."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from ..lanes import assert_changed_paths_allowed
from ..models import BuildTask, CostClass, WorkerCapabilities
from .base import ExecutionEvidence
from .git_clone import LocalGitCloneBackend


class ProcessExecutionError(RuntimeError):
    """Raised when a worker process fails, times out, commits, or escapes its lane."""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git command failed").strip()
        raise ProcessExecutionError(detail)
    return proc.stdout.strip()


def _changed_paths(repo: Path) -> tuple[str, ...]:
    paths: set[str] = set()
    for args in (
        ("diff", "--name-only", "--relative"),
        ("diff", "--cached", "--name-only", "--relative"),
        ("ls-files", "--others", "--exclude-standard"),
    ):
        paths.update(line for line in _git(repo, *args).splitlines() if line)
    return tuple(sorted(paths))


def _bounded_output(stdout: str | None, stderr: str | None, limit: int = 1200) -> str:
    combined = "\n".join(part.strip() for part in (stderr, stdout) if part and part.strip())
    if len(combined) <= limit:
        return combined
    return combined[-limit:]


class LocalProcessBackend:
    """Clone a lane workspace and execute a fixed argv without shell interpolation."""

    def __init__(
        self,
        source_repo: str | Path,
        argv: tuple[str, ...],
        *,
        backend_id: str = "local-process",
        capabilities: frozenset[str] = frozenset({"process", "code", "git-clone"}),
        cost_class: CostClass = CostClass.LOW,
        max_concurrency: int = 1,
        timeout_seconds: int = 900,
        environment: Mapping[str, str] | None = None,
        env_allowlist: tuple[str, ...] = (),
    ) -> None:
        if not argv or not all(isinstance(value, str) and value for value in argv):
            raise ValueError("argv must contain non-empty strings")
        self.argv = argv
        self.clone_backend = LocalGitCloneBackend(source_repo, backend_id=f"{backend_id}-clone")
        self._capabilities = WorkerCapabilities(
            backend_id=backend_id,
            capabilities=capabilities,
            max_concurrency=max_concurrency,
            cost_class=cost_class,
            timeout_seconds=timeout_seconds,
        )
        baseline = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TMP", "TEMP")
        allowed = set(baseline) | set(env_allowlist)
        self.environment = {key: os.environ[key] for key in allowed if key in os.environ}
        if environment:
            self.environment.update(environment)

    @property
    def capabilities(self) -> WorkerCapabilities:
        return self._capabilities

    def claim(self, task: BuildTask) -> bool:
        return task.lane.required_capabilities <= self.capabilities.capabilities

    def prepare_workspace(self, task: BuildTask, workspace: Path) -> Path:
        return self.clone_backend.prepare_workspace(task, workspace)

    def execute(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        initial_head = _git(workspace, "rev-parse", "HEAD")
        try:
            proc = subprocess.run(
                list(self.argv),
                cwd=workspace,
                input=task.instruction,
                capture_output=True,
                text=True,
                shell=False,
                timeout=self.capabilities.timeout_seconds,
                env=self.environment,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProcessExecutionError(
                f"worker {self.capabilities.backend_id!r} timed out"
            ) from exc
        if proc.returncode != 0:
            detail = _bounded_output(proc.stdout, proc.stderr)
            raise ProcessExecutionError(
                f"worker {self.capabilities.backend_id!r} exited {proc.returncode}: {detail}"
            )
        if _git(workspace, "rev-parse", "HEAD") != initial_head:
            raise ProcessExecutionError("worker commits are forbidden before publication")

        changed_paths = _changed_paths(workspace)
        assert_changed_paths_allowed(task.lane, changed_paths)
        return ExecutionEvidence(
            task_id=task.task_id,
            backend_id=self.capabilities.backend_id,
            status="completed",
            summary=f"Worker completed with {len(changed_paths)} changed path(s)",
            changed_paths=changed_paths,
            metadata=(("returncode", str(proc.returncode)),),
        )

    def collect_evidence(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        changed_paths = _changed_paths(workspace)
        assert_changed_paths_allowed(task.lane, changed_paths)
        return ExecutionEvidence(
            task_id=task.task_id,
            backend_id=self.capabilities.backend_id,
            status="evidence",
            summary=f"Collected {len(changed_paths)} changed path(s)",
            changed_paths=changed_paths,
        )

    def cancel(self, task: BuildTask) -> None:
        del task
