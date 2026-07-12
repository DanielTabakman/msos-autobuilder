"""Authenticated Codex CLI backend for isolated Autobuilder lane workspaces."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from enum import StrEnum
from pathlib import Path

from ..lanes import assert_changed_paths_allowed
from ..models import BuildTask, CostClass, WorkerCapabilities
from .base import ExecutionEvidence
from .git_clone import LocalGitCloneBackend
from .process import ProcessExecutionError, _bounded_output, _changed_paths, _git


class CodexHostError(ProcessExecutionError):
    """Raised when Codex is missing, unauthenticated, or fails safely."""


class CodexSandboxMode(StrEnum):
    WORKSPACE_WRITE = "workspace-write"
    DANGEROUS_BYPASS = "dangerous-bypass"


def _existing_file(value: str | Path | None) -> str | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_file():
        return None
    return str(path.resolve())


def iter_codex_cli_candidates(
    explicit: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, ...]:
    """Return deterministic existing Codex CLI candidates for Windows and Unix hosts."""
    env = os.environ if environ is None else environ
    candidates: list[str] = []
    seen: set[str] = set()

    def add(value: str | Path | None) -> None:
        resolved = _existing_file(value)
        if resolved and resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)

    add(explicit)
    add(env.get("MSOS_AUTOBUILDER_CODEX_EXE"))

    local_app = env.get("LOCALAPPDATA", "").strip()
    if local_app:
        add(Path(local_app) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe")

    add(which("codex"))

    app_data = env.get("APPDATA", "").strip()
    if app_data:
        add(Path(app_data) / "npm" / "codex.cmd")

    home = env.get("USERPROFILE") or env.get("HOME", "")
    if home:
        add(Path(home) / ".local" / "bin" / "codex.exe")
        add(Path(home) / ".local" / "bin" / "codex")

    return tuple(candidates)


def resolve_codex_cli(
    explicit: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    candidates = iter_codex_cli_candidates(explicit, environ=environ, which=which)
    return candidates[0] if candidates else None


def _safe_environment(
    *,
    environ: Mapping[str, str] | None = None,
    environment: Mapping[str, str] | None = None,
    env_allowlist: tuple[str, ...] = (),
) -> dict[str, str]:
    source = os.environ if environ is None else environ
    baseline = (
        "PATH",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "LOCALAPPDATA",
        "APPDATA",
        "TMP",
        "TEMP",
        "CODEX_HOME",
    )
    allowed = set(baseline) | set(env_allowlist)
    result = {key: source[key] for key in allowed if key in source}
    if environment:
        result.update(environment)
    return result


def codex_authenticated(
    executable: str | Path,
    *,
    timeout_seconds: int = 30,
    environment: Mapping[str, str] | None = None,
) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [str(executable), "login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
            env=dict(environment) if environment is not None else None,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    detail = _bounded_output(proc.stdout, proc.stderr, limit=600)
    return proc.returncode == 0, detail


class CodexCliBackend:
    """Clone a lane, invoke `codex exec`, then enforce Git path ownership."""

    def __init__(
        self,
        source_repo: str | Path,
        *,
        executable: str | Path | None = None,
        sandbox_mode: CodexSandboxMode | str = CodexSandboxMode.WORKSPACE_WRITE,
        backend_id: str = "codex-cli",
        cost_class: CostClass = CostClass.STANDARD,
        max_concurrency: int = 1,
        timeout_seconds: int = 7200,
        environment: Mapping[str, str] | None = None,
        env_allowlist: tuple[str, ...] = (),
    ) -> None:
        resolved = resolve_codex_cli(executable)
        if not resolved:
            raise CodexHostError(
                "Codex CLI was not found. Install it or set MSOS_AUTOBUILDER_CODEX_EXE."
            )
        self.executable = resolved
        self.sandbox_mode = CodexSandboxMode(sandbox_mode)
        self.environment = _safe_environment(
            environment=environment,
            env_allowlist=env_allowlist,
        )
        self.clone_backend = LocalGitCloneBackend(source_repo, backend_id=f"{backend_id}-clone")
        self._capabilities = WorkerCapabilities(
            backend_id=backend_id,
            capabilities=frozenset({"codex", "code", "process", "git-clone"}),
            max_concurrency=max_concurrency,
            cost_class=cost_class,
            timeout_seconds=timeout_seconds,
        )

    @property
    def capabilities(self) -> WorkerCapabilities:
        return self._capabilities

    def claim(self, task: BuildTask) -> bool:
        return task.lane.required_capabilities <= self.capabilities.capabilities

    def prepare_workspace(self, task: BuildTask, workspace: Path) -> Path:
        return self.clone_backend.prepare_workspace(task, workspace)

    def command_for(self, task: BuildTask, workspace: Path) -> tuple[str, ...]:
        command = [
            self.executable,
            "exec",
            "-C",
            str(workspace),
            "-c",
            'approval_policy="never"',
        ]
        if self.sandbox_mode is CodexSandboxMode.DANGEROUS_BYPASS:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.extend(["-s", "workspace-write"])
        command.append(task.instruction)
        return tuple(command)

    def execute(self, task: BuildTask, workspace: Path) -> ExecutionEvidence:
        authenticated, detail = codex_authenticated(
            self.executable,
            environment=self.environment,
        )
        if not authenticated:
            suffix = f": {detail}" if detail else ""
            raise CodexHostError(f"Codex is not authenticated; run `codex login`{suffix}")

        initial_head = _git(workspace, "rev-parse", "HEAD")
        command = self.command_for(task, workspace)
        try:
            proc = subprocess.run(
                list(command),
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                timeout=self.capabilities.timeout_seconds,
                env=self.environment,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CodexHostError(
                f"Codex lane {task.lane.lane_id!r} timed out after "
                f"{self.capabilities.timeout_seconds}s"
            ) from exc
        except OSError as exc:
            raise CodexHostError(f"Codex could not start: {exc}") from exc

        output = _bounded_output(proc.stdout, proc.stderr, limit=2000)
        if proc.returncode != 0:
            raise CodexHostError(f"Codex exited {proc.returncode}: {output}")
        if _git(workspace, "rev-parse", "HEAD") != initial_head:
            raise CodexHostError("Codex commits are forbidden before publication")

        changed_paths = _changed_paths(workspace)
        assert_changed_paths_allowed(task.lane, changed_paths)
        metadata = (
            ("returncode", str(proc.returncode)),
            ("sandbox_mode", self.sandbox_mode.value),
            ("workspace", str(workspace)),
            ("output_tail", output),
        )
        return ExecutionEvidence(
            task_id=task.task_id,
            backend_id=self.capabilities.backend_id,
            status="completed",
            summary=f"Codex completed with {len(changed_paths)} changed path(s)",
            changed_paths=changed_paths,
            metadata=metadata,
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
