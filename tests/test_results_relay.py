from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from msos_autobuilder.results_relay import (
    ResultsRelay,
    ResultsRelayConfig,
    ResultsRelayError,
    build_complete_patch,
)


def _git(path: Path | None, *args: str) -> str:
    command = ["git"]
    if path is not None:
        command.extend(["-C", str(path)])
    command.extend(args)
    proc = subprocess.run(
        command,
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


def _create_results_remote(root: Path) -> Path:
    seed = _init_repo(root / "seed")
    _git(seed, "checkout", "-qb", "results")
    (seed / "results").mkdir()
    (seed / "results" / "README.md").write_text("review artifacts only\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "initialize results")
    remote = root / "results.git"
    _git(None, "clone", "-q", "--bare", str(seed), str(remote))
    return remote


def _write_host_config(host_root: Path, workspace_root: Path) -> None:
    host_root.mkdir(parents=True)
    (host_root / "host.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "publication_enabled: false",
                f"workspace_root: '{workspace_root.as_posix()}'",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_complete_patch_includes_untracked_files_without_touching_real_index(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path / "workspace")
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("new file\n", encoding="utf-8")

    patch, changed_paths = build_complete_patch(repo)

    assert changed_paths == ("README.md", "new.txt")
    assert "diff --git a/README.md b/README.md" in patch
    assert "diff --git a/new.txt b/new.txt" in patch
    assert "new file mode" in patch
    assert _git(repo, "diff", "--cached", "--name-only") == ""


def test_results_relay_reconstructs_and_pushes_complete_patch(tmp_path: Path) -> None:
    host_root = tmp_path / "host"
    workspace_root = tmp_path / "workspaces"
    workspace = _init_repo(workspace_root / "lane-a")
    (workspace / "README.md").write_text("changed\n", encoding="utf-8")
    (workspace / "new-contract.py").write_text("VERSION = 1\n", encoding="utf-8")
    _write_host_config(host_root, workspace_root)

    job_dir = host_root / "queue" / "completed" / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / "job.yaml").write_text("version: 1\njob_id: job-1\n", encoding="utf-8")
    report = {
        "version": 1,
        "job_id": "job-1",
        "outcome": "completed",
        "publication_enabled": False,
        "codex_report": {
            "evidence": [
                {
                    "task_id": "task-a",
                    "changed_paths": ["README.md", "new-contract.py"],
                }
            ]
        },
        "patches": [
            {
                "task_id": "task-a",
                "lane_id": "lane-a",
                "allow_changes": True,
                "patch_file": "patches/task-a.patch",
                "patch_sha256": "old-incomplete-hash",
            }
        ],
    }
    (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")

    remote = _create_results_remote(tmp_path)
    config = ResultsRelayConfig(
        host_root=host_root,
        repo_url=str(remote),
        branch="results",
        machine_id="test-host",
        poll_seconds=1,
    )

    assert ResultsRelay(config).run_once() == ("job-1",)

    review = tmp_path / "review"
    _git(None, "clone", "-q", "--branch", "results", str(remote), str(review))
    result_root = review / "results" / "test-host" / "job-1"
    relayed_report = json.loads((result_root / "report.json").read_text(encoding="utf-8"))
    source_report = json.loads((result_root / "source-report.json").read_text(encoding="utf-8"))
    integrity = json.loads((result_root / "result-integrity.json").read_text(encoding="utf-8"))
    patch = (result_root / "patches" / "task-a.patch").read_text(encoding="utf-8")

    assert relayed_report["relay"]["complete_patch_reconstruction"] is True
    assert relayed_report["relay"]["source_report_role"].startswith("original-worker")
    assert relayed_report["relay"]["canonical_report_role"].startswith("relay-corrected")
    assert relayed_report["patches"][0]["complete_patch"] is True
    assert source_report["patches"][0]["patch_sha256"] == "old-incomplete-hash"
    assert relayed_report["patches"][0]["patch_sha256"] != "old-incomplete-hash"
    assert integrity["source_report_sha256"] == hashlib.sha256(
        (result_root / "source-report.json").read_bytes()
    ).hexdigest()
    assert integrity["corrected_report_sha256"] == hashlib.sha256(
        (result_root / "report.json").read_bytes()
    ).hexdigest()
    assert relayed_report["patches"][0]["changed_paths"] == ["README.md", "new-contract.py"]
    assert "diff --git a/new-contract.py b/new-contract.py" in patch
    assert (result_root / "source-report.json").exists()


def test_results_relay_rejects_workspace_drift(tmp_path: Path) -> None:
    host_root = tmp_path / "host"
    workspace_root = tmp_path / "workspaces"
    workspace = _init_repo(workspace_root / "lane-a")
    (workspace / "unexpected.txt").write_text("drift\n", encoding="utf-8")
    _write_host_config(host_root, workspace_root)

    job_dir = host_root / "queue" / "completed" / "job-1"
    job_dir.mkdir(parents=True)
    (job_dir / "job.yaml").write_text("version: 1\njob_id: job-1\n", encoding="utf-8")
    report = {
        "version": 1,
        "job_id": "job-1",
        "outcome": "completed",
        "codex_report": {
            "evidence": [{"task_id": "task-a", "changed_paths": ["expected.txt"]}]
        },
        "patches": [
            {
                "task_id": "task-a",
                "lane_id": "lane-a",
                "allow_changes": True,
            }
        ],
    }
    (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")

    remote = _create_results_remote(tmp_path)
    relay = ResultsRelay(
        ResultsRelayConfig(
            host_root=host_root,
            repo_url=str(remote),
            branch="results",
            machine_id="test-host",
            poll_seconds=1,
        )
    )

    try:
        relay.run_once()
    except ResultsRelayError as exc:
        assert "workspace drift" in str(exc)
    else:
        raise AssertionError("workspace drift should fail closed")
