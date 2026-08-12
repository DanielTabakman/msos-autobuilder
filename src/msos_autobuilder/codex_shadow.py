"""Local Codex host configuration, preflight, and shadow-lane execution."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .backends.base import ExecutionEvidence
from .backends.codex_cli import (
    CodexCliBackend,
    CodexSandboxMode,
    codex_authenticated,
    resolve_codex_cli,
)
from .leases import FileLeaseStore
from .models import BuildLane, BuildTask, CostClass
from .routing import BackendRouter
from .scheduler import ParallelScheduler
from .workspaces import WorkspacePolicy

# Bounded Windows cleanup window for owned worker-workspace entries that briefly deny deletion.
WORKSPACE_DELETE_RETRY_SECONDS = 1.0
WORKSPACE_DELETE_SLEEP_SECONDS = 0.05


class CodexConfigError(ValueError):
    """Raised when host or shadow configuration is unsafe or incomplete."""


class CodexShadowError(RuntimeError):
    """Raised when a shadow run cannot complete safely."""


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _make_owned_workspace_path_writable(path: Path) -> None:
    """Clear the writable bit on one validated owned entry without following a link."""
    if _is_link_like(path):
        raise CodexShadowError(
            f"refusing to normalize permissions through worker workspace link: {path}"
        )
    mode = path.lstat().st_mode
    writable_mode = stat.S_IMODE(mode) | stat.S_IWUSR
    try:
        os.chmod(os.fspath(path), writable_mode, follow_symlinks=False)
    except (NotImplementedError, LookupError, ValueError, TypeError) as exc:
        # Some platforms reject follow_symlinks=False for chmod. The entry was already
        # verified non-link-like, so a same-path chmod stays inside the owned workspace.
        if _is_link_like(path):
            raise CodexShadowError(
                f"refusing to normalize permissions through worker workspace link: {path}"
            ) from exc
        os.chmod(os.fspath(path), writable_mode)


def _delete_owned_workspace_path_with_retry(path: Path, delete: Callable[[], None]) -> None:
    """Delete one owned workspace entry with writable normalize + bounded PermissionError retry."""
    deadline = time.monotonic() + WORKSPACE_DELETE_RETRY_SECONDS
    last_error: PermissionError | None = None
    while True:
        if _is_link_like(path):
            # Symlink/junction entries are unlinked only; never chmod their targets.
            try:
                path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError as exc:
                last_error = exc
        else:
            try:
                delete()
                return
            except FileNotFoundError:
                return
            except PermissionError as exc:
                last_error = exc
                try:
                    if _path_lexists(path) and not _is_link_like(path):
                        _make_owned_workspace_path_writable(path)
                        delete()
                        return
                except FileNotFoundError:
                    return
                except PermissionError as retry_exc:
                    last_error = retry_exc
                except CodexShadowError:
                    raise
                except OSError:
                    # Writable-bit normalization can race with transient locks; keep retrying.
                    pass

        if time.monotonic() >= deadline:
            if last_error is not None:
                raise last_error
            raise PermissionError(f"could not delete worker workspace path: {path}")
        time.sleep(WORKSPACE_DELETE_SLEEP_SECONDS)


def _remove_workspace_tree_no_symlinks(path: Path) -> None:
    if _is_link_like(path):
        raise CodexShadowError(f"refusing to traverse worker workspace link: {path}")
    if not path.exists():
        return
    if not path.is_dir():
        _delete_owned_workspace_path_with_retry(path, path.unlink)
        return
    with os.scandir(path) as entries:
        for entry in entries:
            child = Path(entry.path)
            if entry.is_symlink() or _is_link_like(child):
                _delete_owned_workspace_path_with_retry(child, child.unlink)
            elif entry.is_dir(follow_symlinks=False):
                _remove_workspace_tree_no_symlinks(child)
            else:
                _delete_owned_workspace_path_with_retry(child, child.unlink)
    _delete_owned_workspace_path_with_retry(path, path.rmdir)


def _reset_owned_workspace(workspace: Path) -> None:
    """Remove one expected Autobuilder-owned lane workspace, Windows-safe and fail-closed."""
    if not _path_lexists(workspace):
        return
    if _is_link_like(workspace):
        _delete_owned_workspace_path_with_retry(workspace, workspace.unlink)
    else:
        _remove_workspace_tree_no_symlinks(workspace)
    if _path_lexists(workspace):
        raise CodexShadowError(
            f"worker workspace still present after reset: {workspace}"
        )


@dataclass(frozen=True)
class CodexHostConfig:
    source_repo: Path
    workspace_root: Path
    runtime_root: Path
    owner_id: str
    executable: str | None
    sandbox_mode: CodexSandboxMode
    max_concurrency: int
    timeout_seconds: int
    publication_enabled: bool = False
    reset_workspaces: bool = True


@dataclass(frozen=True)
class ShadowTaskSpec:
    task: BuildTask
    allow_changes: bool = False


@dataclass(frozen=True)
class CodexPreflightReport:
    ok: bool
    source_repo: str
    source_head: str | None
    source_clean: bool
    executable: str | None
    authenticated: bool
    sandbox_mode: str
    publication_enabled: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class CodexShadowReport:
    status: str
    source_head: str
    publication_enabled: bool
    owner_id: str
    evidence: tuple[dict[str, Any], ...]


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CodexConfigError(f"{field} must be a mapping")
    return value


def _strings(value: Any, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise CodexConfigError(f"{field} must be a list of non-empty strings")
    if not value and not allow_empty:
        raise CodexConfigError(f"{field} must not be empty")
    return tuple(value)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git command failed").strip()
        raise CodexShadowError(detail)
    return proc.stdout.strip()


def load_codex_host_config(path: str | Path) -> CodexHostConfig:
    config_path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    root = _mapping(raw, "host config")
    if root.get("version") != 1:
        raise CodexConfigError("only host config version 1 is supported")
    if root.get("publication_enabled", False) is not False:
        raise CodexConfigError("Codex shadow host config must keep publication disabled")

    codex = _mapping(root.get("codex", {}), "codex")
    source_repo = Path(str(root.get("source_repo", ""))).expanduser().resolve()
    workspace_root = Path(str(root.get("workspace_root", ""))).expanduser().resolve()
    runtime_root = Path(str(root.get("runtime_root", ""))).expanduser().resolve()
    owner_id = str(root.get("owner_id") or "codex-shadow-host").strip()
    executable_raw = codex.get("executable")
    executable = str(executable_raw).strip() if executable_raw else None
    sandbox_mode = CodexSandboxMode(
        str(codex.get("sandbox_mode") or CodexSandboxMode.WORKSPACE_WRITE.value)
    )
    max_concurrency = int(codex.get("max_concurrency", 1))
    timeout_seconds = int(codex.get("timeout_seconds", 7200))
    reset_workspaces = bool(root.get("reset_workspaces", True))

    if not str(root.get("source_repo") or "").strip():
        raise CodexConfigError("source_repo is required")
    if not str(root.get("workspace_root") or "").strip():
        raise CodexConfigError("workspace_root is required")
    if not str(root.get("runtime_root") or "").strip():
        raise CodexConfigError("runtime_root is required")
    if not owner_id:
        raise CodexConfigError("owner_id is required")
    if max_concurrency < 1:
        raise CodexConfigError("codex.max_concurrency must be positive")
    if timeout_seconds < 1:
        raise CodexConfigError("codex.timeout_seconds must be positive")

    WorkspacePolicy(
        product_root=source_repo,
        workspace_root=workspace_root,
        runtime_root=runtime_root,
    )
    return CodexHostConfig(
        source_repo=source_repo,
        workspace_root=workspace_root,
        runtime_root=runtime_root,
        owner_id=owner_id,
        executable=executable,
        sandbox_mode=sandbox_mode,
        max_concurrency=max_concurrency,
        timeout_seconds=timeout_seconds,
        reset_workspaces=reset_workspaces,
    )


def load_codex_shadow_manifest(path: str | Path) -> tuple[ShadowTaskSpec, ...]:
    manifest_path = Path(path).expanduser().resolve()
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    root = _mapping(raw, "shadow manifest")
    if root.get("version") != 1:
        raise CodexConfigError("only shadow manifest version 1 is supported")
    if root.get("publication_enabled", False) is not False:
        raise CodexConfigError("Codex shadow manifest must keep publication disabled")
    lanes = root.get("lanes")
    if not isinstance(lanes, list) or not lanes:
        raise CodexConfigError("lanes must be a non-empty list")

    specs: list[ShadowTaskSpec] = []
    for index, raw_lane in enumerate(lanes):
        lane_data = _mapping(raw_lane, f"lanes[{index}]")
        instruction = lane_data.get("instruction")
        prompt_file = lane_data.get("prompt_file")
        if bool(instruction) == bool(prompt_file):
            raise CodexConfigError(
                f"lanes[{index}] must define exactly one of instruction or prompt_file"
            )
        if prompt_file:
            prompt_path = (manifest_path.parent / str(prompt_file)).resolve()
            instruction = prompt_path.read_text(encoding="utf-8")
        if not isinstance(instruction, str) or not instruction.strip():
            raise CodexConfigError(f"lanes[{index}] instruction must not be empty")

        preferred = CostClass(str(lane_data.get("preferred_cost_class") or "standard"))
        required = lane_data.get("required_capabilities", ["codex", "code", "git-clone"])
        task = BuildTask(
            task_id=str(lane_data.get("task_id") or "").strip(),
            lane=BuildLane(
                lane_id=str(lane_data.get("lane_id") or "").strip(),
                chapter_id=str(lane_data.get("chapter_id") or "").strip(),
                branch=str(lane_data.get("branch") or "").strip(),
                layer=str(lane_data.get("layer") or "").strip(),
                allowed_paths=_strings(
                    lane_data.get("allowed_paths"),
                    f"lanes[{index}].allowed_paths",
                ),
                forbidden_paths=_strings(
                    lane_data.get("forbidden_paths", []),
                    f"lanes[{index}].forbidden_paths",
                    allow_empty=True,
                ),
                required_capabilities=frozenset(
                    _strings(required, f"lanes[{index}].required_capabilities")
                ),
                preferred_cost_class=preferred,
            ),
            instruction=instruction.strip(),
        )
        specs.append(
            ShadowTaskSpec(
                task=task,
                allow_changes=bool(lane_data.get("allow_changes", False)),
            )
        )
    return tuple(specs)


def codex_host_preflight(config: CodexHostConfig) -> CodexPreflightReport:
    errors: list[str] = []
    source_head: str | None = None
    source_clean = False
    if not (config.source_repo / ".git").exists():
        errors.append(f"source_repo is not a Git checkout: {config.source_repo}")
    else:
        try:
            source_head = _git(config.source_repo, "rev-parse", "HEAD")
            source_clean = not bool(_git(config.source_repo, "status", "--porcelain"))
            if not source_clean:
                errors.append("source_repo must be clean so shadow lanes use an unambiguous commit")
        except CodexShadowError as exc:
            errors.append(str(exc))

    executable = resolve_codex_cli(config.executable)
    authenticated = False
    if not executable:
        errors.append("Codex CLI not found; install Codex or set codex.executable")
    else:
        authenticated, detail = codex_authenticated(executable)
        if not authenticated:
            message = "Codex is not authenticated; run `codex login`"
            if detail:
                message += f" ({detail})"
            errors.append(message)

    return CodexPreflightReport(
        ok=not errors,
        source_repo=str(config.source_repo),
        source_head=source_head,
        source_clean=source_clean,
        executable=executable,
        authenticated=authenticated,
        sandbox_mode=config.sandbox_mode.value,
        publication_enabled=False,
        errors=tuple(errors),
    )


def _evidence_dict(evidence: ExecutionEvidence) -> dict[str, Any]:
    return {
        "task_id": evidence.task_id,
        "backend_id": evidence.backend_id,
        "status": evidence.status,
        "summary": evidence.summary,
        "changed_paths": list(evidence.changed_paths),
        "metadata": dict(evidence.metadata),
    }


def run_codex_shadow(
    config: CodexHostConfig,
    specs: tuple[ShadowTaskSpec, ...],
) -> CodexShadowReport:
    preflight = codex_host_preflight(config)
    if not preflight.ok or not preflight.source_head:
        raise CodexShadowError("; ".join(preflight.errors) or "Codex host preflight failed")

    policy = WorkspacePolicy(
        product_root=config.source_repo,
        workspace_root=config.workspace_root,
        runtime_root=config.runtime_root,
    )
    if config.reset_workspaces:
        for spec in specs:
            workspace = policy.expected_path(spec.task.lane)
            try:
                _reset_owned_workspace(workspace)
            except PermissionError as exc:
                raise CodexShadowError(
                    f"failed to reset worker workspace {workspace}: {exc}"
                ) from exc

    backend = CodexCliBackend(
        config.source_repo,
        executable=preflight.executable,
        sandbox_mode=config.sandbox_mode,
        max_concurrency=config.max_concurrency,
        timeout_seconds=config.timeout_seconds,
    )
    scheduler = ParallelScheduler(
        router=BackendRouter([backend]),
        lease_store=FileLeaseStore(config.runtime_root),
        workspace_policy=policy,
    )
    evidence = scheduler.run(
        [spec.task for spec in specs],
        owner_id=config.owner_id,
        lease_ttl_seconds=max(300, min(config.timeout_seconds + 300, 86400)),
    )
    by_task = {spec.task.task_id: spec for spec in specs}
    for item in evidence:
        if not by_task[item.task_id].allow_changes and item.changed_paths:
            raise CodexShadowError(
                f"task {item.task_id!r} was read-only but changed {list(item.changed_paths)}"
            )

    if _git(config.source_repo, "rev-parse", "HEAD") != preflight.source_head:
        raise CodexShadowError("source HEAD changed during shadow execution")
    if _git(config.source_repo, "status", "--porcelain"):
        raise CodexShadowError("source checkout became dirty during shadow execution")

    return CodexShadowReport(
        status="completed",
        source_head=preflight.source_head,
        publication_enabled=False,
        owner_id=config.owner_id,
        evidence=tuple(_evidence_dict(item) for item in evidence),
    )


def render_codex_preflight_json(report: CodexPreflightReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True) + "\n"


def render_codex_shadow_json(report: CodexShadowReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True) + "\n"
