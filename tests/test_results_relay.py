from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from msos_autobuilder.lifecycle_evidence import attempt_identity_from_job, identity_digest
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


def _job_identity(job_id: str) -> dict[str, object]:
    return {
        "version": 1,
        "job_id": job_id,
        "founder_build_next": {
            "pipeline_id": "ppe",
            "work_item_id": "fixture-work",
            "work_item_source_sha256_v1": "a" * 64,
            "refill_attempt": {
                "generation_id": "refill-12345678",
                "attempt_ordinal": 1,
                "retry_ordinal": 0,
            },
        },
    }


def _write_completed_job(host_root: Path, job_id: str, job_yaml: str) -> Path:
    job_dir = host_root / "queue" / "completed" / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "job.yaml").write_text(job_yaml, encoding="utf-8")
    report = {
        "version": 1,
        "job_id": job_id,
        "outcome": "completed",
        "publication_enabled": False,
        "codex_report": {"evidence": []},
        "patches": [],
    }
    (job_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return job_dir


def _read_single_relay_head(host_root: Path) -> tuple[dict, dict]:
    heads = list((host_root / "state" / "relay-evidence" / "heads" / "result").glob("*.json"))
    assert len(heads) == 1
    head = json.loads(heads[0].read_text(encoding="utf-8"))
    envelope = json.loads((host_root / head["envelope_path"]).read_text(encoding="utf-8"))
    return head, envelope


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
    (job_dir / "job.yaml").write_text(
        yaml.safe_dump(_job_identity("job-1"), sort_keys=False),
        encoding="utf-8",
    )
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

    head, envelope = _read_single_relay_head(host_root)
    assert head["producer_sequence"] == 1
    assert envelope["payload"] == {
        "relay_disposition": "relayed",
        "relayed_commit": _git(review, "rev-parse", "HEAD"),
        "canonical_report_sha256": integrity["corrected_report_sha256"],
        "source_report_sha256": integrity["source_report_sha256"],
        "complete_patch_reconstruction": True,
    }
    source_path = host_root / envelope["source"]["path"]
    staging_report = host_root / "state" / "results-relay-staging" / "job-1" / "report.json"
    assert source_path.read_bytes() == staging_report.read_bytes()
    assert envelope["source_sha256"] == hashlib.sha256(source_path.read_bytes()).hexdigest()
    assert head["envelope_sha256"] == hashlib.sha256(
        (host_root / head["envelope_path"]).read_bytes()
    ).hexdigest()

    assert ResultsRelay(config).run_once() == ()
    assert (host_root / head["envelope_path"]).read_text(encoding="utf-8") == json.dumps(
        envelope,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ) + "\n"


@pytest.mark.parametrize(
    ("job_id", "job_yaml", "message"),
    [
        ("invalid-yaml", "[", "job YAML is invalid"),
        ("non-mapping-yaml", "- item\n", "job YAML must be a mapping"),
    ],
)
def test_results_relay_malformed_job_yaml_preserves_primary_and_records_diagnostic(
    tmp_path: Path,
    job_id: str,
    job_yaml: str,
    message: str,
) -> None:
    host_root = tmp_path / "host"
    workspace_root = tmp_path / "workspaces"
    _write_host_config(host_root, workspace_root)
    _write_completed_job(host_root, job_id, job_yaml)
    remote = _create_results_remote(tmp_path)
    config = ResultsRelayConfig(
        host_root=host_root,
        repo_url=str(remote),
        branch="results",
        machine_id="test-host",
        poll_seconds=1,
    )

    assert ResultsRelay(config).run_once() == (job_id,)

    ledger = json.loads((host_root / "state" / "results-relay-seen.json").read_text("utf-8"))
    assert job_id in ledger
    review = tmp_path / f"review-{job_id}"
    _git(None, "clone", "-q", "--branch", "results", str(remote), str(review))
    assert (review / "results" / "test-host" / job_id / "report.json").exists()
    diagnostics = list(
        (host_root / "state" / "producer-evidence-errors" / "results_relay").glob("*.json")
    )
    assert len(diagnostics) == 1
    diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
    assert diagnostic["primary_outcome_preserved"] is True
    assert diagnostic["primary_outcome"]["job_id"] == job_id
    assert diagnostic["identity_digest"] == "unknown"
    assert message in diagnostic["message"]


def test_results_relay_unreadable_job_yaml_preserves_primary_and_records_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_root = tmp_path / "host"
    workspace_root = tmp_path / "workspaces"
    _write_host_config(host_root, workspace_root)
    _write_completed_job(
        host_root,
        "unreadable-yaml",
        yaml.safe_dump(_job_identity("unreadable-yaml"), sort_keys=False),
    )
    remote = _create_results_remote(tmp_path)
    original_read_text = Path.read_text

    def deny_staging_job_yaml(path: Path, *args: object, **kwargs: object) -> str:
        if (
            path.name == "job.yaml"
            and "results-relay-staging" in path.as_posix()
            and path.parent.name == "unreadable-yaml"
        ):
            raise PermissionError("job yaml denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny_staging_job_yaml)
    config = ResultsRelayConfig(
        host_root=host_root,
        repo_url=str(remote),
        branch="results",
        machine_id="test-host",
        poll_seconds=1,
    )

    assert ResultsRelay(config).run_once() == ("unreadable-yaml",)

    ledger = json.loads((host_root / "state" / "results-relay-seen.json").read_text("utf-8"))
    assert "unreadable-yaml" in ledger
    diagnostics = list(
        (host_root / "state" / "producer-evidence-errors" / "results_relay").glob("*.json")
    )
    assert len(diagnostics) == 1
    diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
    assert diagnostic["primary_outcome"]["job_id"] == "unreadable-yaml"
    assert diagnostic["identity_digest"] == "unknown"
    assert "job YAML is unreadable" in diagnostic["message"]


def test_results_relay_valid_non_refill_job_yaml_preserves_primary_without_evidence(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "host"
    workspace_root = tmp_path / "workspaces"
    _write_host_config(host_root, workspace_root)
    _write_completed_job(host_root, "ordinary-job", "version: 1\njob_id: ordinary-job\n")
    remote = _create_results_remote(tmp_path)
    config = ResultsRelayConfig(
        host_root=host_root,
        repo_url=str(remote),
        branch="results",
        machine_id="test-host",
        poll_seconds=1,
    )

    assert ResultsRelay(config).run_once() == ("ordinary-job",)

    ledger = json.loads((host_root / "state" / "results-relay-seen.json").read_text("utf-8"))
    assert "ordinary-job" in ledger
    assert not (host_root / "state" / "relay-evidence").exists()
    assert not (host_root / "state" / "producer-evidence-errors").exists()


def test_results_relay_two_job_malformed_identity_diagnostic_does_not_leak_first_identity(
    tmp_path: Path,
) -> None:
    host_root = tmp_path / "host"
    workspace_root = tmp_path / "workspaces"
    _write_host_config(host_root, workspace_root)
    first = _job_identity("first-job")
    second = _job_identity("second-job")
    second["founder_build_next"]["refill_attempt"]["attempt_ordinal"] = "not-an-int"  # type: ignore[index]
    _write_completed_job(host_root, "first-job", yaml.safe_dump(first, sort_keys=False))
    _write_completed_job(host_root, "second-job", yaml.safe_dump(second, sort_keys=False))
    remote = _create_results_remote(tmp_path)
    config = ResultsRelayConfig(
        host_root=host_root,
        repo_url=str(remote),
        branch="results",
        machine_id="test-host",
        poll_seconds=1,
    )

    assert ResultsRelay(config).run_once() == ("first-job", "second-job")

    ledger = json.loads((host_root / "state" / "results-relay-seen.json").read_text("utf-8"))
    assert set(ledger) == {"first-job", "second-job"}
    diagnostics = list(
        (host_root / "state" / "producer-evidence-errors" / "results_relay").glob("*.json")
    )
    assert len(diagnostics) == 1
    diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
    assert diagnostic["primary_outcome"]["job_id"] == "second-job"
    assert diagnostic["identity_digest"] == "unknown"
    assert diagnostic["identity_digest"] != identity_digest(attempt_identity_from_job(first))


def test_results_relay_emits_not_applicable_for_host_failure(tmp_path: Path) -> None:
    host_root = tmp_path / "host"
    workspace_root = tmp_path / "workspaces"
    _write_host_config(host_root, workspace_root)
    failed_dir = host_root / "queue" / "failed" / "job-failed"
    failed_dir.mkdir(parents=True)
    (failed_dir / "job.yaml").write_text(
        yaml.safe_dump(_job_identity("job-failed"), sort_keys=False),
        encoding="utf-8",
    )
    error = {
        "job_id": "job-failed",
        "outcome": "failed",
        "error_type": "HostJobError",
        "message": "source mismatch",
        "recorded_at": "2026-07-29T00:00:00+00:00",
        "publication_enabled": False,
    }
    (failed_dir / "error.json").write_text(json.dumps(error, sort_keys=True), encoding="utf-8")
    remote = _create_results_remote(tmp_path)
    config = ResultsRelayConfig(
        host_root=host_root,
        repo_url=str(remote),
        branch="results",
        machine_id="test-host",
        poll_seconds=1,
    )

    assert ResultsRelay(config).run_once() == ()

    head, envelope = _read_single_relay_head(host_root)
    assert head["producer_sequence"] == 1
    assert envelope["closed_status"] == "not_applicable"
    assert envelope["payload"] == {
        "relay_disposition": "not_applicable",
        "relayed_commit": None,
        "canonical_report_sha256": None,
        "source_report_sha256": None,
        "complete_patch_reconstruction": False,
    }
    assert envelope["source"]["path"] == "queue/failed/job-failed/error.json"
    assert envelope["source_sha256"] == hashlib.sha256(
        (failed_dir / "error.json").read_bytes()
    ).hexdigest()


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
