from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from msos_autobuilder.backends.codex_cli import CodexSandboxMode
from msos_autobuilder.codex_shadow import (
    WORKSPACE_DELETE_RETRY_SECONDS,
    CodexConfigError,
    CodexHostConfig,
    CodexShadowError,
    _is_link_like,
    _make_owned_workspace_path_writable,
    _reset_owned_workspace,
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
    script.write_text(
        "#!/usr/bin/env python3\n" + script.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
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


def test_ordinary_existing_workspace_is_removed_before_shadow(tmp_path: Path) -> None:
    source = _git_repo(tmp_path / "source")
    codex = _fake_codex(tmp_path / "codex")
    specs = load_codex_shadow_manifest(_manifest(tmp_path / "manifest.yaml"))
    workspace = tmp_path / "workspaces" / "web-shadow"
    stale = workspace / ".pytest_cache" / "v" / "cache"
    stale.mkdir(parents=True)
    (stale / "nodeids").write_text("stale\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    original_head = _run("git", "rev-parse", "HEAD", cwd=source)

    report = run_codex_shadow(_config(tmp_path, source, codex), specs)

    assert report.status == "completed"
    assert report.publication_enabled is False
    assert not (workspace / ".pytest_cache").exists()
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert _run("git", "rev-parse", "HEAD", cwd=source) == original_head
    assert _run("git", "status", "--porcelain", cwd=source) == ""


def test_reset_owned_workspace_removes_ordinary_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "lane"
    nested = workspace / ".pytest_cache" / "v"
    nested.mkdir(parents=True)
    (nested / "cache").write_text("stale\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")

    _reset_owned_workspace(workspace)

    assert not workspace.exists()
    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_reset_owned_workspace_noop_when_absent(tmp_path: Path) -> None:
    workspace = tmp_path / "missing-lane"

    _reset_owned_workspace(workspace)

    assert not workspace.exists()


def test_permission_error_on_owned_file_is_normalized_and_reset_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msos_autobuilder.codex_shadow as shadow

    workspace = tmp_path / "lane"
    workspace.mkdir()
    owned = workspace / "readonly-object"
    owned.write_text("blob", encoding="utf-8")
    chmod_paths: list[Path] = []
    original_unlink = Path.unlink
    attempts = {"count": 0}

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == owned and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError(5, "Access is denied", str(self))
        return original_unlink(self, *args, **kwargs)

    real_chmod = os.chmod

    def tracking_chmod(
        path: str | bytes | os.PathLike[str],
        mode: int,
        *args: object,
        **kwargs: object,
    ) -> None:
        chmod_paths.append(Path(os.fspath(path)))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(os, "chmod", tracking_chmod)
    monkeypatch.setattr(shadow, "WORKSPACE_DELETE_SLEEP_SECONDS", 0.0)

    _reset_owned_workspace(workspace)

    assert not workspace.exists()
    assert attempts["count"] == 1
    assert any(path == owned for path in chmod_paths)


def test_transient_permission_error_within_retry_bound_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msos_autobuilder.codex_shadow as shadow

    workspace = tmp_path / "lane"
    workspace.mkdir()
    owned = workspace / "locked-object"
    owned.write_text("blob", encoding="utf-8")
    original_unlink = Path.unlink
    failures_remaining = {"count": 3}

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == owned and failures_remaining["count"] > 0:
            failures_remaining["count"] -= 1
            raise PermissionError(5, "Access is denied", str(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(shadow, "WORKSPACE_DELETE_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(shadow, "_make_owned_workspace_path_writable", lambda path: None)

    _reset_owned_workspace(workspace)

    assert not workspace.exists()
    assert failures_remaining["count"] == 0


def test_persistent_permission_error_raises_instead_of_swallowing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msos_autobuilder.codex_shadow as shadow

    workspace = tmp_path / "lane"
    workspace.mkdir()
    owned = workspace / "locked-object"
    owned.write_text("blob", encoding="utf-8")
    original_unlink = Path.unlink

    def always_denied(self: Path, *args: object, **kwargs: object) -> None:
        if self == owned:
            raise PermissionError(5, "Access is denied", str(self))
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", always_denied)
    monkeypatch.setattr(shadow, "WORKSPACE_DELETE_SLEEP_SECONDS", 0.0)
    monkeypatch.setattr(shadow, "WORKSPACE_DELETE_RETRY_SECONDS", 0.0)
    monkeypatch.setattr(shadow, "_make_owned_workspace_path_writable", lambda path: None)

    with pytest.raises(PermissionError, match="Access is denied"):
        _reset_owned_workspace(workspace)

    assert workspace.exists()
    assert owned.exists()


def test_symlink_permission_retry_does_not_normalize_or_traverse_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import msos_autobuilder.codex_shadow as shadow

    workspace = tmp_path / "lane"
    workspace.mkdir()
    target = tmp_path / "outside-target.txt"
    target.write_text("keep\n", encoding="utf-8")
    nested_link = workspace / "linked-file"
    try:
        nested_link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    chmod_paths: list[Path] = []
    original_unlink = Path.unlink
    attempts = {"count": 0}

    def flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == nested_link and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError(5, "Access is denied", str(self))
        return original_unlink(self, *args, **kwargs)

    real_chmod = os.chmod

    def tracking_chmod(
        path: str | bytes | os.PathLike[str],
        mode: int,
        *args: object,
        **kwargs: object,
    ) -> None:
        chmod_paths.append(Path(os.fspath(path)).resolve())
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    monkeypatch.setattr(os, "chmod", tracking_chmod)
    monkeypatch.setattr(shadow, "WORKSPACE_DELETE_SLEEP_SECONDS", 0.0)

    _reset_owned_workspace(workspace)

    assert not workspace.exists()
    assert target.read_text(encoding="utf-8") == "keep\n"
    assert target.resolve() not in chmod_paths
    assert attempts["count"] == 1


def test_reset_success_requires_workspace_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "lane"
    workspace.mkdir()
    (workspace / "residue.txt").write_text("still here\n", encoding="utf-8")

    monkeypatch.setattr(
        "msos_autobuilder.codex_shadow._remove_workspace_tree_no_symlinks",
        lambda path: None,
    )

    with pytest.raises(CodexShadowError, match="still present after reset"):
        _reset_owned_workspace(workspace)

    assert workspace.exists()
    assert (workspace / "residue.txt").read_text(encoding="utf-8") == "still here\n"


def test_writable_normalize_refuses_link_like_entry(tmp_path: Path) -> None:
    target = tmp_path / "outside-target.txt"
    target.write_text("keep\n", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(CodexShadowError, match="refusing to normalize permissions"):
        _make_owned_workspace_path_writable(link)

    assert target.read_text(encoding="utf-8") == "keep\n"


def test_python311_reparse_fallback_detects_link_like_without_is_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows reparse detection must work when Path.is_junction is absent (Python 3.11)."""
    entry = tmp_path / "junction-standin"
    entry.mkdir()
    ordinary = tmp_path / "ordinary-dir"
    ordinary.mkdir()

    if hasattr(Path, "is_junction"):
        monkeypatch.delattr(Path, "is_junction")
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    real_lstat = Path.lstat

    def fake_lstat(self: Path) -> object:
        base = real_lstat(self)
        if self == entry:
            return type(
                "ReparseStat",
                (),
                {
                    "st_mode": base.st_mode,
                    "st_file_attributes": reparse_flag,
                },
            )()
        return type(
            "OrdinaryStat",
            (),
            {
                "st_mode": base.st_mode,
                "st_file_attributes": 0,
            },
        )()

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    assert _is_link_like(entry) is True
    assert _is_link_like(ordinary) is False


def test_python311_reparse_fallback_unlinks_without_chmod_or_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reparse-like children must be unlinked, never chmod'd or walked into."""
    import msos_autobuilder.codex_shadow as shadow

    workspace = tmp_path / "lane"
    workspace.mkdir()
    reparse_entry = workspace / "junction-standin"
    reparse_entry.mkdir()
    nested = reparse_entry / "nested.txt"
    nested.write_text("nested-keep\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    parked = tmp_path / "parked-junction"

    if hasattr(Path, "is_junction"):
        monkeypatch.delattr(Path, "is_junction")
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    monkeypatch.setattr(shadow, "WORKSPACE_DELETE_SLEEP_SECONDS", 0.0)

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    real_lstat = Path.lstat

    def fake_lstat(self: Path) -> object:
        base = real_lstat(self)
        if self == reparse_entry:
            return type(
                "ReparseStat",
                (),
                {
                    "st_mode": base.st_mode,
                    "st_file_attributes": reparse_flag,
                },
            )()
        return base

    monkeypatch.setattr(Path, "lstat", fake_lstat)

    chmod_paths: list[Path] = []
    real_chmod = os.chmod

    def tracking_chmod(
        path: str | bytes | os.PathLike[str],
        mode: int,
        *args: object,
        **kwargs: object,
    ) -> None:
        chmod_paths.append(Path(os.fspath(path)))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", tracking_chmod)

    original_unlink = Path.unlink

    def junction_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == reparse_entry:
            # Simulate removing a junction entry without traversing its target tree.
            self.rename(parked)
            return
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", junction_unlink)

    _reset_owned_workspace(workspace)

    assert not workspace.exists()
    assert outside.read_text(encoding="utf-8") == "keep\n"
    assert parked.exists()
    assert (parked / "nested.txt").read_text(encoding="utf-8") == "nested-keep\n"
    assert not any(path == reparse_entry or path == nested for path in chmod_paths)
    assert not hasattr(Path, "is_junction")


def test_retry_window_is_short_and_finite() -> None:
    assert 0 < WORKSPACE_DELETE_RETRY_SECONDS <= 2.0
