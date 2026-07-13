from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from msos_autobuilder.candidate_gate import (
    CandidateGate,
    CandidateGateError,
    load_candidate_gate_config,
)


def _git(path: Path | None, *args: str, check: bool = True) -> str:
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
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return proc.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "app.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "app.txt")
    _git(path, "commit", "-qm", "initial source")
    return path


def _candidate_patch(source: Path) -> tuple[str, tuple[str, ...]]:
    (source / "app.txt").write_text("candidate\n", encoding="utf-8")
    (source / "new.txt").write_text("new\n", encoding="utf-8")
    _git(source, "add", "-N", "new.txt")
    patch = _git(source, "diff", "--binary", "HEAD") + "\n"
    changed = tuple(sorted(_git(source, "diff", "--name-only", "HEAD").splitlines()))
    _git(source, "reset", "--hard", "HEAD")
    _git(source, "clean", "-fd")
    return patch, changed


def _results_remote(
    root: Path,
    *,
    source_head: str,
    patch: str,
    changed_paths: tuple[str, ...],
    patch_sha: str | None = None,
    job_id: str = "job-1",
) -> Path:
    seed = root / "results-seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    _git(seed, "checkout", "-qb", "results")
    job = seed / "results" / "test-host" / job_id
    patches = job / "patches"
    patches.mkdir(parents=True)
    patch_path = patches / "task-a.patch"
    patch_path.write_bytes(patch.encode("utf-8"))
    digest = patch_sha or hashlib.sha256(patch_path.read_bytes()).hexdigest()
    report = {
        "version": 1,
        "job_id": job_id,
        "outcome": "completed",
        "publication_enabled": False,
        "codex_report": {
            "source_head": source_head,
            "publication_enabled": False,
            "evidence": [{"task_id": "task-a", "changed_paths": list(changed_paths)}],
        },
        "patches": [
            {
                "task_id": "task-a",
                "lane_id": "lane-a",
                "allow_changes": True,
                "complete_patch": True,
                "patch_file": "patches/task-a.patch",
                "patch_sha256": digest,
                "changed_paths": list(changed_paths),
            }
        ],
        "relay": {
            "version": 1,
            "machine_id": "test-host",
            "complete_patch_reconstruction": True,
            "publication_enabled": False,
        },
    }
    (job / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (job / "job.yaml").write_text(f"version: 1\njob_id: {job_id}\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "seed result")
    remote = root / "results.git"
    _git(None, "clone", "-q", "--bare", str(seed), str(remote))
    return remote


def _write_config(
    root: Path,
    *,
    host_root: Path,
    source: Path,
    remote: Path,
    check_code: str,
    policy_blocks: tuple[str, ...] = (),
    job_id: str = "job-1",
) -> Path:
    config = {
        "version": 1,
        "publication_enabled": False,
        "host_root": str(host_root),
        "source_repo": str(source),
        "results_repo_url": str(remote),
        "results_branch": "results",
        "machine_id": "test-host",
        "poll_seconds": 1,
        "plans": {
            job_id: {
                "checks": [
                    {
                        "name": "fixture-check",
                        "argv": [sys.executable, "-c", check_code],
                        "cwd": ".",
                        "timeout_seconds": 30,
                    }
                ],
                "policy_blocks": list(policy_blocks),
            }
        },
    }
    path = root / "candidate-gate.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _read_gate_report(root: Path, remote: Path, job_id: str = "job-1") -> dict:
    review = root / f"review-{job_id}"
    _git(None, "clone", "-q", "--branch", "results", str(remote), str(review))
    report_path = review / "results" / "test-host" / job_id / "gate-report.json"
    return json.loads(report_path.read_text(encoding="utf-8"))


def test_candidate_gate_applies_complete_patch_and_runs_checks(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
    )
    host_root = tmp_path / "host"
    config_path = _write_config(
        tmp_path,
        host_root=host_root,
        source=source,
        remote=remote,
        check_code=(
            "from pathlib import Path; "
            "assert Path('app.txt').read_text() == 'candidate\\n'; "
            "assert Path('new.txt').read_text() == 'new\\n'"
        ),
    )

    gate = CandidateGate(load_candidate_gate_config(config_path))
    assert gate.run_once() == ("job-1",)
    assert gate.run_once() == ()

    report = _read_gate_report(tmp_path, remote)
    assert report["status"] == "passed"
    assert report["publication_enabled"] is False
    assert report["product_write_performed"] is False
    assert report["changed_paths"] == ["app.txt", "new.txt"]
    assert report["checks"][0]["passed"] is True
    assert report["workspace_removed"] is True
    assert _git(source, "rev-parse", "HEAD") == source_head
    assert _git(source, "status", "--porcelain") == ""


def test_candidate_gate_records_check_failure_and_policy_block(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
    )
    config_path = _write_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
        check_code="raise SystemExit(7)",
        policy_blocks=("schema compatibility decision unresolved",),
    )

    assert CandidateGate(load_candidate_gate_config(config_path)).run_once() == ("job-1",)
    report = _read_gate_report(tmp_path, remote)
    assert report["status"] == "failed"
    assert report["checks"][0]["returncode"] == 7
    assert report["checks"][0]["passed"] is False
    assert report["policy_blocks"] == ["schema compatibility decision unresolved"]
    assert report["errors"] == []


def test_candidate_gate_fails_closed_on_patch_hash_mismatch(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
        patch_sha="0" * 64,
    )
    config_path = _write_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
        check_code="raise AssertionError('must not run')",
    )

    assert CandidateGate(load_candidate_gate_config(config_path)).run_once() == ("job-1",)
    report = _read_gate_report(tmp_path, remote)
    assert report["status"] == "failed"
    assert report["checks"] == []
    assert "patch hash mismatch" in report["errors"][0]["message"]
    assert report["publication_enabled"] is False


def test_candidate_gate_rejects_default_branch_and_mutated_result(tmp_path: Path) -> None:
    source = _init_repo(tmp_path / "source")
    source_head = _git(source, "rev-parse", "HEAD")
    patch, changed_paths = _candidate_patch(source)
    remote = _results_remote(
        tmp_path,
        source_head=source_head,
        patch=patch,
        changed_paths=changed_paths,
    )
    config_path = _write_config(
        tmp_path,
        host_root=tmp_path / "host",
        source=source,
        remote=remote,
        check_code="pass",
    )
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["results_branch"] = "main"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="main or master"):
        load_candidate_gate_config(config_path)

    config["results_branch"] = "results"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    gate = CandidateGate(load_candidate_gate_config(config_path))
    assert gate.run_once() == ("job-1",)
    local_report = (
        gate.results.checkout / "results" / "test-host" / "job-1" / "report.json"
    )
    payload = json.loads(local_report.read_text(encoding="utf-8"))
    payload["tampered"] = True
    local_report.write_text(json.dumps(payload), encoding="utf-8")
    _git(gate.results.checkout, "add", str(local_report))
    _git(gate.results.checkout, "commit", "-qm", "tamper result")
    _git(gate.results.checkout, "push", "origin", "HEAD:results")

    with pytest.raises(CandidateGateError, match="changed after gate processing"):
        gate.run_once()
