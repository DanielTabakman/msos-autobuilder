from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from msos_autobuilder.backends.codex_cli import CodexSandboxMode
from msos_autobuilder.codex_shadow import (
    CodexConfigError,
    CodexHostConfig,
    codex_host_preflight,
    load_codex_shadow_manifest,
    run_codex_shadow,
)


def _run(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _git_repo(path: Path) -> Path:
    path.mkdir()
    _run("git", "init", "-q", cwd=path)
    _run("git", "config", "user.email", "test@example.com", cwd=path)
    _run("git", "config", "user.name", "Test", cwd=path)
    (path / "apps" / "msos-web").mkdir(parents=True)
    (path / "src" / "engine").mkdir(parents=True)
    (path / "apps" / "msos-web" / "README.md").write_text("web\n", encoding="utf-8")
    (path / "src" / "engine" / "README.md").write_text("core\n", encoding="utf-8")
    _run("git", "add", ".", cwd=path)
    _run("git", "commit", "-qm", "init", cwd=path)
    return path


def _fake_codex(path: Path) -> Path:
    script = path.with_suffix(".py")
    script.write_text(
        """import pathlib
import sys
import time
args = sys.argv[1:]
if args[:2] == [\"login\", \"status\"]:
    print(\"Logged in\")
    raise SystemExit(0)
if not args or args[0] != \"exec\":
    raise SystemExit(3)
workspace = pathlib.Path(args[args.index(\"-C\") + 1])
prompt = args[-1]
if \"WRITE_WEB\" in prompt:
    (workspace / \"apps\" / \"msos-web\" / \"codex-shadow.txt\").write_text(\"web\\n\")
if \"WRITE_OUTSIDE\" in prompt:
    (workspace / \"outside.txt\").write_text(\"bad\\n\")
if \"SLEEP\" in prompt:
    time.sleep(0.15)
print(\"fake codex: \" + prompt)
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = path.with_suffix(".cmd")
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
        return launcher
    script.write_text("#!/usr/bin/env python3\n" + script.read_text(encoding="utf-8"), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _manifest(path: Path, *, publication: bool = False, write: bool = False) -> Path:
    instruction = "WRITE_WEB" if write else "Inspect only; do not modify files."
    path.write_text(
        f"""version: 1
publication_enabled: {str(publication).lower()}
lanes:
  - task_id: web-shadow
    lane_id: web-shadow
    chapter_id: SHADOW-WEB
    branch: shadow/web
    layer: msos-shell
    allowed_paths:
      - apps/msos-web/**
    forbidden_paths:
      - artifacts/**
    allow_changes: {str(write).lower()}
    instruction: {instruction}
  - task_id: core-shadow
    lane_id: core-shadow
    chapter_id: SHADOW-CORE
    branch: shadow/core
    layer: ppe-core
    allowed_paths:
      - src/engine/**
    allow_changes: false
    instruction: Inspect core only; do not modify files.
""",
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path, source: Path, codex: Path) -> CodexHostConfig:
    return CodexHostConfig(
        source_repo=source,
        workspace_root=tmp_path / "workspaces",
        runtime_root=tmp_path / "runtime",
        owner_id="test-host",
        executable=str(codex),
        sandbox_mode=CodexSandboxMode.WORKSPACE_WRITE,
        max_concurrency=2,
        timeout_seconds=30,
    )


def test_manifest_requires_publication_disabled(tmp_path: Path) -> None:
    with pytest.raises(CodexConfigError, match="publication disabled"):
        load_codex_shadow_manifest(_manifest(tmp_path / "manifest.yaml", publication=True))


def test_preflight_reports_authenticated_clean_host(tmp_path: Path) -> None:
    source = _git_repo(tmp_path / "source")
    codex = _fake_codex(tmp_path / "codex")

    report = codex_host_preflight(_config(tmp_path, source, codex))

    assert report.ok
    assert report.authenticated
    assert report.source_clean
    assert report.publication_enabled is False


def test_two_fake_codex_lanes_run_without_touching_source(tmp_path: Path) -> None:
    source = _git_repo(tmp_path / "source")
    codex = _fake_codex(tmp_path / "codex")
    specs = load_codex_shadow_manifest(_manifest(tmp_path / "manifest.yaml"))
    original_head = _run("git", "rev-parse", "HEAD", cwd=source)

    report = run_codex_shadow(_config(tmp_path, source, codex), specs)

    assert report.status == "completed"
    assert report.publication_enabled is False
    assert len(report.evidence) == 2
    assert all(not item["changed_paths"] for item in report.evidence)
    assert _run("git", "rev-parse", "HEAD", cwd=source) == original_head
    assert _run("git", "status", "--porcelain", cwd=source) == ""


def test_explicitly_change_allowed_lane_is_path_scoped(tmp_path: Path) -> None:
    source = _git_repo(tmp_path / "source")
    codex = _fake_codex(tmp_path / "codex")
    specs = load_codex_shadow_manifest(_manifest(tmp_path / "manifest.yaml", write=True))

    report = run_codex_shadow(_config(tmp_path, source, codex), specs)

    web = next(item for item in report.evidence if item["task_id"] == "web-shadow")
    assert web["changed_paths"] == ["apps/msos-web/codex-shadow.txt"]
    assert _run("git", "status", "--porcelain", cwd=source) == ""
