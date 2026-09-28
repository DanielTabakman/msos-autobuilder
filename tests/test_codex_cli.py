from __future__ import annotations

import os
from pathlib import Path

import pytest

from msos_autobuilder.backends.codex_cli import (
    CodexCliBackend,
    CodexSandboxMode,
    iter_codex_cli_candidates,
)
from msos_autobuilder.models import BuildLane, BuildTask


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_candidate_order_prefers_explicit_then_env_then_windows_then_path(tmp_path: Path) -> None:
    explicit = _touch(tmp_path / "explicit" / "codex")
    env_exe = _touch(tmp_path / "env" / "codex")
    local_app = tmp_path / "local"
    windows_exe = _touch(local_app / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe")
    path_exe = _touch(tmp_path / "path" / "codex")

    result = iter_codex_cli_candidates(
        explicit,
        environ={
            "MSOS_AUTOBUILDER_CODEX_EXE": str(env_exe),
            "LOCALAPPDATA": str(local_app),
        },
        which=lambda _: str(path_exe),
    )

    expected = (explicit, env_exe, windows_exe, path_exe)
    assert result[:4] == tuple(str(path.resolve()) for path in expected)


def _task() -> BuildTask:
    return BuildTask(
        task_id="web-shadow",
        lane=BuildLane(
            lane_id="web",
            chapter_id="chapter-web",
            branch="shadow/web",
            layer="msos-shell",
            allowed_paths=("apps/msos-web/**",),
            required_capabilities=frozenset({"codex", "code", "git-clone"}),
        ),
        instruction="Inspect only; do not modify files.",
    )


def _git_repo(path: Path) -> Path:
    os.system(f'git init -q "{path}"')
    os.system(f'git -C "{path}" config user.email test@example.com')
    os.system(f'git -C "{path}" config user.name Test')
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    os.system(f'git -C "{path}" add README.md')
    os.system(f'git -C "{path}" commit -qm init')
    return path


def test_command_defaults_to_workspace_write_and_prompt_is_one_argument(tmp_path: Path) -> None:
    source = _git_repo(tmp_path / "source")
    executable = _touch(tmp_path / "codex")
    backend = CodexCliBackend(source, executable=executable)
    workspace = tmp_path / "workspace"

    command = backend.command_for(_task(), workspace)

    assert command[:6] == (
        str(executable.resolve()),
        "exec",
        "-C",
        str(workspace),
        "-c",
        'approval_policy="never"',
    )
    assert command[-3:-1] == ("-s", "workspace-write")
    assert command[-1] == "Inspect only; do not modify files."


def test_dangerous_bypass_requires_explicit_mode(tmp_path: Path) -> None:
    source = _git_repo(tmp_path / "source")
    executable = _touch(tmp_path / "codex")
    backend = CodexCliBackend(
        source,
        executable=executable,
        sandbox_mode=CodexSandboxMode.DANGEROUS_BYPASS,
    )

    command = backend.command_for(_task(), tmp_path / "workspace")

    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert "workspace-write" not in command


@pytest.mark.parametrize("mode", ["not-a-mode", "bypass"])
def test_unknown_sandbox_mode_fails(tmp_path: Path, mode: str) -> None:
    source = _git_repo(tmp_path / "source")
    executable = _touch(tmp_path / "codex")
    with pytest.raises(ValueError):
        CodexCliBackend(source, executable=executable, sandbox_mode=mode)


def test_execute_records_process_identity_and_timing(tmp_path: Path, monkeypatch) -> None:
    source = _git_repo(tmp_path / "source")
    executable = _touch(tmp_path / "codex")
    backend = CodexCliBackend(source, executable=executable)
    workspace = tmp_path / "workspace"
    backend.prepare_workspace(_task(), workspace)

    class FakeProcess:
        pid = 4242
        returncode = 0

        def __init__(self, *args, **kwargs) -> None:
            self.args = args
            self.kwargs = kwargs

        def communicate(self, timeout=None):
            assert timeout == backend.capabilities.timeout_seconds
            return ("completed", "")

        def kill(self) -> None:
            raise AssertionError("successful process should not be killed")

    monkeypatch.setattr(
        "msos_autobuilder.backends.codex_cli.codex_authenticated",
        lambda *args, **kwargs: (True, "logged in"),
    )
    monkeypatch.setattr(
        "msos_autobuilder.backends.codex_cli._git",
        lambda _workspace, *args: "fixture-head" if args[:2] == ("rev-parse", "HEAD") else "",
    )
    monkeypatch.setattr(
        "msos_autobuilder.backends.codex_cli._changed_paths",
        lambda _workspace: (),
    )
    monkeypatch.setattr(
        "msos_autobuilder.backends.codex_cli.subprocess.Popen",
        FakeProcess,
    )

    evidence = backend.execute(_task(), workspace)
    metadata = dict(evidence.metadata)

    assert metadata["process_id"] == "4242"
    assert float(metadata["duration_seconds"]) >= 0
    assert metadata["started_at"]
    assert metadata["finished_at"]
    assert metadata["output_tail"] == "completed"