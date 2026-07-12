from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from msos_autobuilder.revision_loop import (
    build_revision_manifest,
    RevisionLoop,
    RevisionLoopConfig,
    RevisionLoopError,
    RevisionPlan,
)


SOURCE_HEAD = "1" * 40
SOURCE_REPORT_SHA = "2" * 64
ROOT_JOB_ID = "mcd-boundary-and-frozen-contract-v1"
TARGET_TASK_ID = "ppe-frozen-evaluation-contract-v1"


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


def _gate_report(*, job_id: str = ROOT_JOB_ID) -> dict[str, object]:
    return {
        "version": 1,
        "job_id": job_id,
        "status": "failed",
        "publication_enabled": False,
        "product_write_performed": False,
        "source_head": SOURCE_HEAD,
        "source_report_sha256": SOURCE_REPORT_SHA,
        "started_at": "2026-07-12T16:16:43+00:00",
        "finished_at": "2026-07-12T16:16:48+00:00",
        "checks": [
            {
                "name": "snapshot-identity",
                "argv": ["python", "check.py"],
                "passed": False,
                "returncode": 1,
                "timed_out": False,
                "stdout": "",
                "stderr": "outer snapshot_id must match record_header",
            }
        ],
        "policy_blocks": [
            "Preserve ppe_frozen_eval_v1 until downstream migration is approved."
        ],
        "errors": [],
        "patches": [
            {
                "task_id": TARGET_TASK_ID,
                "patch_sha256": "3" * 64,
                "changed_paths": ["src/viz/frozen_evaluation_contract.py"],
            }
        ],
    }


def _job_yaml() -> dict[str, object]:
    return {
        "version": 1,
        "job_id": ROOT_JOB_ID,
        "approved": True,
        "publication_enabled": False,
        "expected_source_head": SOURCE_HEAD,
        "manifest": {
            "version": 1,
            "publication_enabled": False,
            "lanes": [
                {
                    "task_id": TARGET_TASK_ID,
                    "lane_id": "ppe-ui-frozen-contract",
                    "chapter_id": "MCD-FROZEN-EVALUATION-CONTRACT-V1",
                    "branch": "shadow/original",
                    "layer": "ppe-ui",
                    "preferred_cost_class": "standard",
                    "allowed_paths": [
                        "src/viz/**",
                        "tests/test_frozen_evaluation*.py",
                        "tests/fixtures/frozen_evaluation/**",
                    ],
                    "forbidden_paths": ["apps/msos-web/**", "src/engine/**"],
                    "allow_changes": True,
                    "instruction": "Implement the original frozen evaluation contract.",
                }
            ],
        },
    }


def _plan() -> RevisionPlan:
    return RevisionPlan(
        revision_job_prefix=TARGET_TASK_ID,
        target_task_ids=(TARGET_TASK_ID,),
        instruction_prefix=(
            "Retain ppe_frozen_eval_v1 and reject mismatched outer/header snapshot IDs."
        ),
    )


def test_build_revision_manifest_preserves_boundaries_and_exact_evidence() -> None:
    manifest = build_revision_manifest(
        _gate_report(),
        _job_yaml(),
        _plan(),
        max_revision_depth=3,
    )

    assert manifest["job_id"] == f"{TARGET_TASK_ID}-revision-1"
    assert manifest["approved"] is True
    assert manifest["publication_enabled"] is False
    assert manifest["expected_source_head"] == SOURCE_HEAD
    assert manifest["revision"] == {
        "version": 1,
        "depth": 1,
        "source_job_id": ROOT_JOB_ID,
        "source_report_sha256": SOURCE_REPORT_SHA,
    }

    lane = manifest["manifest"]["lanes"][0]
    assert lane["allowed_paths"] == _job_yaml()["manifest"]["lanes"][0]["allowed_paths"]
    assert lane["forbidden_paths"] == _job_yaml()["manifest"]["lanes"][0]["forbidden_paths"]
    assert "outer snapshot_id must match record_header" in lane["instruction"]
    assert "Preserve ppe_frozen_eval_v1" in lane["instruction"]
    assert "3" * 64 in lane["instruction"]
    assert "Do not commit, push, publish" in lane["instruction"]


def test_build_revision_manifest_rejects_passed_or_unsafe_reports() -> None:
    passed = _gate_report()
    passed["status"] = "passed"
    with pytest.raises(RevisionLoopError, match="only failed"):
        build_revision_manifest(passed, _job_yaml(), _plan(), max_revision_depth=3)

    wrote_product = _gate_report()
    wrote_product["product_write_performed"] = True
    with pytest.raises(RevisionLoopError, match="product write"):
        build_revision_manifest(wrote_product, _job_yaml(), _plan(), max_revision_depth=3)


def test_build_revision_manifest_enforces_maximum_depth() -> None:
    report = _gate_report(job_id=f"{TARGET_TASK_ID}-revision-3")
    with pytest.raises(RevisionLoopError, match="maximum revision depth"):
        build_revision_manifest(report, _job_yaml(), _plan(), max_revision_depth=3)


def _create_remote(root: Path) -> Path:
    seed = _init_repo(root / "seed")

    _git(seed, "checkout", "-qb", "jobs")
    (seed / "jobs" / "approved").mkdir(parents=True)
    (seed / "jobs" / "approved" / "README.md").write_text(
        "approved jobs only\n",
        encoding="utf-8",
    )
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "initialize jobs")

    _git(seed, "checkout", "-qb", "results", "master")
    result = seed / "results" / "test-host" / ROOT_JOB_ID
    result.mkdir(parents=True)
    (result / "gate-report.json").write_text(
        json.dumps(_gate_report(), indent=2) + "\n",
        encoding="utf-8",
    )
    (result / "job.yaml").write_text(
        yaml.safe_dump(_job_yaml(), sort_keys=False),
        encoding="utf-8",
    )
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "add failed gate fixture")

    remote = root / "remote.git"
    _git(None, "clone", "-q", "--bare", str(seed), str(remote))
    return remote


def test_revision_loop_writes_only_jobs_branch_and_is_repeat_safe(tmp_path: Path) -> None:
    remote = _create_remote(tmp_path)
    config = RevisionLoopConfig(
        host_root=tmp_path / "host",
        repo_url=str(remote),
        results_branch="results",
        jobs_branch="jobs",
        machine_id="test-host",
        poll_seconds=1,
        max_revision_depth=3,
        plans={ROOT_JOB_ID: _plan()},
    )
    loop = RevisionLoop(config)

    assert loop.run_once() == (f"{TARGET_TASK_ID}-revision-1",)
    assert loop.run_once() == ()

    jobs_review = tmp_path / "jobs-review"
    _git(None, "clone", "-q", "--branch", "jobs", str(remote), str(jobs_review))
    manifest_path = (
        jobs_review
        / "jobs"
        / "approved"
        / f"{TARGET_TASK_ID}-revision-1.yaml"
    )
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["approved"] is True
    assert manifest["publication_enabled"] is False
    assert manifest["expected_source_head"] == SOURCE_HEAD

    results_review = tmp_path / "results-review"
    _git(None, "clone", "-q", "--branch", "results", str(remote), str(results_review))
    assert not list(results_review.rglob(f"{TARGET_TASK_ID}-revision-1.yaml"))


def test_revision_loop_rejects_mutated_gate_evidence(tmp_path: Path) -> None:
    remote = _create_remote(tmp_path)
    config = RevisionLoopConfig(
        host_root=tmp_path / "host",
        repo_url=str(remote),
        machine_id="test-host",
        poll_seconds=1,
        plans={ROOT_JOB_ID: _plan()},
    )
    loop = RevisionLoop(config)
    assert loop.run_once()

    editor = tmp_path / "editor"
    _git(None, "clone", "-q", "--branch", "results", str(remote), str(editor))
    _git(editor, "config", "user.email", "test@example.com")
    _git(editor, "config", "user.name", "Test")
    gate_path = editor / "results" / "test-host" / ROOT_JOB_ID / "gate-report.json"
    payload = json.loads(gate_path.read_text(encoding="utf-8"))
    payload["policy_blocks"].append("mutated after processing")
    gate_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _git(editor, "add", ".")
    _git(editor, "commit", "-qm", "mutate gate evidence")
    _git(editor, "push", "-q", "origin", "results")

    with pytest.raises(RevisionLoopError, match="changed after revision processing"):
        loop.run_once()


def test_revision_loop_rejects_default_branch_targets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="jobs_branch"):
        RevisionLoopConfig(
            host_root=tmp_path,
            repo_url="fixture",
            jobs_branch="main",
            plans={},
        )
