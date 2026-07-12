from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from msos_autobuilder.codex_shadow import CodexShadowReport
from msos_autobuilder.persistent_host import (
    HostJobError,
    HostLockError,
    HostPaths,
    HostProcessLock,
    PersistentHost,
    PersistentHostConfig,
    PersistentHostConfigError,
    approve_pending_job,
    enqueue_manifest,
    load_persistent_host_config,
    parse_host_job,
    sync_git_job_feed,
)


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-qm", "init")
    return path


def _manifest(*, allow_changes: bool = False) -> dict[str, Any]:
    return {
        "version": 1,
        "publication_enabled": False,
        "lanes": [
            {
                "task_id": "web-task",
                "lane_id": "web-lane",
                "chapter_id": "chapter-web",
                "branch": "autobuilder/web",
                "layer": "msos-shell",
                "allowed_paths": ["apps/msos-web/**"],
                "instruction": "Inspect the bounded surface.",
                "allow_changes": allow_changes,
            }
        ],
    }


def _write_configs(
    root: Path,
    *,
    feed: dict[str, Any] | None = None,
) -> tuple[PersistentHostConfig, Path]:
    root.mkdir(parents=True, exist_ok=True)
    source = _init_repo(root / "source")
    host_root = root / "host"
    workspace_root = root / "workspaces"
    runtime_root = root / "runtime"
    codex_config_path = root / "codex-host.yaml"
    codex_config_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "publication_enabled": False,
                "source_repo": str(source),
                "workspace_root": str(workspace_root),
                "runtime_root": str(runtime_root),
                "owner_id": "test-host",
                "reset_workspaces": True,
                "codex": {
                    "sandbox_mode": "workspace-write",
                    "max_concurrency": 2,
                    "timeout_seconds": 30,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    service: dict[str, Any] = {
        "version": 1,
        "publication_enabled": False,
        "host_root": str(host_root),
        "codex_host_config": str(codex_config_path),
        "poll_seconds": 0.01,
        "heartbeat_seconds": 0.01,
    }
    if feed is not None:
        service["job_feed"] = feed
    service_path = root / "service.yaml"
    service_path.write_text(yaml.safe_dump(service, sort_keys=False), encoding="utf-8")
    return load_persistent_host_config(service_path), source


def _fake_runner(
    config: Any,
    specs: tuple[Any, ...],
) -> CodexShadowReport:
    for spec in specs:
        workspace = config.workspace_root / spec.task.lane.lane_id
        subprocess.run(
            ["git", "clone", "-q", str(config.source_repo), str(workspace)],
            check=True,
        )
    return CodexShadowReport(
        status="completed",
        source_head=_git(config.source_repo, "rev-parse", "HEAD"),
        publication_enabled=False,
        owner_id="test-host",
        evidence=(),
    )


def test_job_parser_rejects_publication_and_prompt_files() -> None:
    payload = {
        "version": 1,
        "job_id": "safe-job",
        "approved": True,
        "publication_enabled": False,
        "manifest": _manifest(),
    }
    parse_host_job(yaml.safe_dump(payload))

    payload["manifest"]["publication_enabled"] = True
    with pytest.raises(HostJobError, match="publication"):
        parse_host_job(yaml.safe_dump(payload))

    payload["manifest"] = _manifest()
    payload["manifest"]["lanes"][0]["prompt_file"] = "outside.txt"
    with pytest.raises(HostJobError, match="inline instructions"):
        parse_host_job(yaml.safe_dump(payload))


def test_host_config_rejects_publication(tmp_path: Path) -> None:
    path = tmp_path / "service.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "publication_enabled": True,
                "host_root": str(tmp_path / "host"),
                "codex_host_config": str(tmp_path / "codex.yaml"),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PersistentHostConfigError, match="disabled"):
        load_persistent_host_config(path)


def test_unapproved_job_waits_then_approved_job_completes(tmp_path: Path) -> None:
    config, _ = _write_configs(tmp_path)
    paths = HostPaths.from_root(config.host_root)
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(_manifest()), encoding="utf-8")
    enqueue_manifest(paths, manifest_path, job_id="job-1")

    host = PersistentHost(config, runner=_fake_runner)
    assert host.run_once(sync_feed=False).processed is False
    assert (paths.pending / "job-1.yaml").exists()

    approve_pending_job(paths, "job-1")
    result = host.run_once(sync_feed=False)
    assert result.outcome == "completed"
    report_path = paths.completed / "job-1" / "report.json"
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["publication_enabled"] is False
    assert report["outcome"] == "completed"
    assert not (paths.running / "job-1.yaml").exists()


def test_malformed_job_moves_to_failed(tmp_path: Path) -> None:
    config, _ = _write_configs(tmp_path)
    paths = HostPaths.from_root(config.host_root)
    paths.ensure()
    (paths.pending / "bad.yaml").write_text("not: [valid", encoding="utf-8")

    result = PersistentHost(config, runner=_fake_runner).run_once(sync_feed=False)
    assert result.processed is False
    failed = list(paths.failed.iterdir())
    assert len(failed) == 1
    assert (failed[0] / "error.json").exists()


def test_interrupted_running_job_is_recovered(tmp_path: Path) -> None:
    config, _ = _write_configs(tmp_path)
    paths = HostPaths.from_root(config.host_root)
    paths.ensure()
    payload = {
        "version": 1,
        "job_id": "interrupted",
        "approved": True,
        "publication_enabled": False,
        "manifest": _manifest(),
    }
    (paths.running / "interrupted.yaml").write_text(
        yaml.safe_dump(payload),
        encoding="utf-8",
    )

    PersistentHost(config, runner=_fake_runner).initialize()
    assert not (paths.running / "interrupted.yaml").exists()
    error = json.loads(
        (paths.failed / "interrupted" / "error.json").read_text(encoding="utf-8")
    )
    assert error["outcome"] == "interrupted"


def test_duplicate_live_lock_fails_closed(tmp_path: Path) -> None:
    lock_path = tmp_path / "host.lock"
    first = HostProcessLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(HostLockError, match="already running"):
            HostProcessLock(lock_path).acquire()
    finally:
        first.release()


def test_expected_source_head_mismatch_fails_before_runner(tmp_path: Path) -> None:
    config, _ = _write_configs(tmp_path)
    paths = HostPaths.from_root(config.host_root)
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(_manifest()), encoding="utf-8")
    enqueue_manifest(
        paths,
        manifest_path,
        job_id="wrong-head",
        approved=True,
        expected_source_head="deadbee",
    )
    calls = 0

    def runner(config: Any, specs: tuple[Any, ...]) -> CodexShadowReport:
        nonlocal calls
        calls += 1
        return _fake_runner(config, specs)

    result = PersistentHost(config, runner=runner).run_once(sync_feed=False)
    assert result.outcome == "failed"
    assert calls == 0
    assert (paths.failed / "wrong-head" / "error.json").exists()


def test_feed_imports_approved_job_once(tmp_path: Path) -> None:
    feed_repo = _init_repo(tmp_path / "feed")
    _git(feed_repo, "checkout", "-qb", "jobs")
    job_dir = feed_repo / "jobs" / "approved"
    job_dir.mkdir(parents=True)
    payload = {
        "version": 1,
        "job_id": "remote-1",
        "approved": True,
        "publication_enabled": False,
        "manifest": _manifest(),
    }
    (job_dir / "remote-1.yaml").write_text(
        yaml.safe_dump(payload),
        encoding="utf-8",
    )
    _git(feed_repo, "add", ".")
    _git(feed_repo, "commit", "-qm", "add approved job")

    config, _ = _write_configs(
        tmp_path / "host-case",
        feed={
            "enabled": True,
            "repo_url": str(feed_repo),
            "branch": "jobs",
            "path": "jobs/approved",
            "refresh_seconds": 1,
        },
    )
    paths = HostPaths.from_root(config.host_root)
    assert sync_git_job_feed(config, paths) == ("remote-1",)
    assert sync_git_job_feed(config, paths) == ()
    assert (paths.pending / "remote-1.yaml").exists()


def test_unapproved_feed_job_is_not_imported(tmp_path: Path) -> None:
    feed_repo = _init_repo(tmp_path / "feed")
    _git(feed_repo, "checkout", "-qb", "jobs")
    job_dir = feed_repo / "jobs" / "approved"
    job_dir.mkdir(parents=True)
    payload = {
        "version": 1,
        "job_id": "not-approved",
        "approved": False,
        "publication_enabled": False,
        "manifest": _manifest(),
    }
    (job_dir / "not-approved.yaml").write_text(
        yaml.safe_dump(payload),
        encoding="utf-8",
    )
    _git(feed_repo, "add", ".")
    _git(feed_repo, "commit", "-qm", "add unapproved job")

    config, _ = _write_configs(
        tmp_path / "host-case",
        feed={
            "enabled": True,
            "repo_url": str(feed_repo),
            "branch": "jobs",
            "path": "jobs/approved",
        },
    )
    paths = HostPaths.from_root(config.host_root)
    assert sync_git_job_feed(config, paths) == ()
    assert not (paths.pending / "not-approved.yaml").exists()
