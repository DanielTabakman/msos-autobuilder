from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from msos_autobuilder.controlled_publisher import (
    ControlledPublisher,
    GitHubDraftClient,
    PublisherError,
    load_publisher_config,
)


def git(repo: Path | None, *args: str) -> str:
    command = ["git"]
    if repo is not None:
        command.extend(["-C", str(repo)])
    command.extend(args)
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stdout.strip()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publisher_success(config_path: Path) -> dict[str, Any]:
    config = load_publisher_config(config_path)
    return json.loads(
        (config.host_root / "state" / "publisher-service-success.json").read_text(
            encoding="utf-8"
        )
    )


class FakeGitHubClient(GitHubDraftClient):
    def __init__(self, product_bare: Path) -> None:
        self.product_bare = product_bare
        self.repo_full_name = "owner/product"
        self.owner = "owner"
        self.pulls: list[dict[str, Any]] = []

    def find_pull_requests(self, branch: str) -> list[dict[str, Any]]:
        return [pull for pull in self.pulls if pull["head"]["ref"] == branch]

    def create_draft(
        self,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> dict[str, Any]:
        commit = git(self.product_bare, "rev-parse", f"refs/heads/{branch}")
        pull = {
            "number": 1,
            "html_url": "https://example.invalid/pull/1",
            "state": "open",
            "draft": True,
            "title": title,
            "body": body,
            "head": {"ref": branch, "sha": commit},
            "base": {"ref": base},
        }
        self.pulls.append(pull)
        return pull


def make_fixture(tmp_path: Path, *, overlap: bool = False) -> tuple[Path, Path, Path, str]:
    product_work = tmp_path / "product-work"
    product_work.mkdir()
    git(product_work, "init", "-b", "main")
    git(product_work, "config", "user.name", "Fixture")
    git(product_work, "config", "user.email", "fixture@example.invalid")
    value = product_work / "src" / "viz" / "value.py"
    value.parent.mkdir(parents=True)
    value.write_bytes(b"VALUE = 1\n")
    git(product_work, "add", ".")
    git(product_work, "commit", "-m", "source")
    source_head = git(product_work, "rev-parse", "HEAD")

    patch_work = tmp_path / "patch-work"
    git(None, "clone", str(product_work), str(patch_work))
    git(patch_work, "checkout", "--detach", source_head)
    (patch_work / "src" / "viz" / "value.py").write_bytes(b"VALUE = 2\n")
    patch = git(patch_work, "diff", "--binary", "HEAD") + "\n"

    if overlap:
        value.write_bytes(b"VALUE = 3\n")
        git(product_work, "add", ".")
        git(product_work, "commit", "-m", "overlap")
    else:
        docs = product_work / "docs" / "note.md"
        docs.parent.mkdir(parents=True)
        docs.write_bytes(b"docs-only\n")
        git(product_work, "add", ".")
        git(product_work, "commit", "-m", "docs")

    product_bare = tmp_path / "product.git"
    git(None, "clone", "--bare", str(product_work), str(product_bare))

    evidence_work = tmp_path / "evidence-work"
    evidence_work.mkdir()
    git(evidence_work, "init", "-b", "results")
    git(evidence_work, "config", "user.name", "Fixture")
    git(evidence_work, "config", "user.email", "fixture@example.invalid")
    job_id = "candidate-revision-1"
    job_dir = evidence_work / "results" / "MACHINE" / job_id
    patch_path = job_dir / "patches" / "candidate.patch"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_bytes(patch.encode("utf-8"))
    patch_hash = sha256(patch_path)

    source_report = {
        "version": 1,
        "job_id": job_id,
        "outcome": "completed",
        "publication_enabled": False,
        "codex_report": {
            "status": "completed",
            "publication_enabled": False,
            "source_head": source_head,
        },
        "relay": {
            "version": 1,
            "publication_enabled": False,
            "complete_patch_reconstruction": True,
        },
        "patches": [
            {
                "task_id": job_id,
                "patch_file": "patches/candidate.patch",
                "patch_sha256": patch_hash,
                "changed_paths": ["src/viz/value.py"],
                "complete_patch": True,
            }
        ],
    }
    source_path = job_dir / "report.json"
    write_json(source_path, source_report)
    source_hash = sha256(source_path)
    original_report = {**source_report, "patches": [{**source_report["patches"][0], "patch_sha256": "worker-hash"}]}
    write_json(job_dir / "source-report.json", original_report)
    write_json(
        job_dir / "result-integrity.json",
        {
            "version": 1,
            "source_report_sha256": sha256(job_dir / "source-report.json"),
            "corrected_report_sha256": source_hash,
            "canonical_patch_sha256_by_task": {job_id: patch_hash},
            "publication_enabled": False,
        },
    )
    gate_report = {
        "version": 1,
        "job_id": job_id,
        "status": "passed",
        "state": "candidate_passed",
        "started_at": "2026-07-13T00:00:00+00:00",
        "finished_at": "2026-07-13T00:00:01+00:00",
        "publication_enabled": False,
        "product_write_performed": False,
        "workspace_removed": True,
        "source_head": source_head,
        "source_report_sha256": source_hash,
        "changed_paths": ["src/viz/value.py"],
        "checks": [{"name": "gate", "passed": True, "returncode": 0}],
        "policy_blocks": [],
        "errors": [],
        "patches": [
            {
                "task_id": job_id,
                "patch_file": "patches/candidate.patch",
                "patch_sha256": patch_hash,
                "changed_paths": ["src/viz/value.py"],
            }
        ],
    }
    write_json(job_dir / "gate-report.json", gate_report)
    git(evidence_work, "add", ".")
    git(evidence_work, "commit", "-m", "evidence")
    evidence_bare = tmp_path / "evidence.git"
    git(None, "clone", "--bare", str(evidence_work), str(evidence_bare))

    config = tmp_path / "publisher.yaml"
    config.write_text(
        f"""
version: 1
draft_pr_publication_enabled: true
merge_enabled: false
main_write_enabled: false
host_root: {tmp_path.as_posix()}/host
evidence_repo_url: {evidence_bare.as_posix()}
results_branch: results
product_repo_url: {product_bare.as_posix()}
product_repo_full_name: owner/product
product_base_branch: main
machine_id: MACHINE
poll_seconds: 1
plans:
  {job_id}:
    branch: autobuilder/{job_id}
    title: Controlled candidate
    commit_message: Apply controlled candidate
    checks:
      - name: publication-check
        argv:
          - {sys.executable}
          - -c
          - "from pathlib import Path; assert 'VALUE = 2' in Path('src/viz/value.py').read_text()"
        cwd: .
        timeout_seconds: 30
""".lstrip(),
        encoding="utf-8",
    )
    return config, product_bare, evidence_bare, job_id


def test_controlled_publisher_creates_one_draft_pr_and_is_repeat_safe(tmp_path: Path) -> None:
    config_path, product_bare, evidence_bare, job_id = make_fixture(tmp_path)
    config = load_publisher_config(config_path)
    client = FakeGitHubClient(product_bare)
    publisher = ControlledPublisher(config, github_client=client)

    assert publisher.run_once() == (job_id,)
    main_head = git(product_bare, "rev-parse", "refs/heads/main")
    branch_head = git(product_bare, "rev-parse", f"refs/heads/autobuilder/{job_id}")
    assert main_head != branch_head
    assert len(client.pulls) == 1
    assert client.pulls[0]["draft"] is True
    assert "ppe-automerge: true" not in client.pulls[0]["body"]

    evidence_check = tmp_path / "evidence-check"
    git(None, "clone", "--branch", "results", str(evidence_bare), str(evidence_check))
    publication = json.loads(
        (
            evidence_check
            / "results"
            / "MACHINE"
            / job_id
            / "publication-report.json"
        ).read_text(encoding="utf-8")
    )
    assert publication["status"] == "published-draft"
    assert publication["merge_enabled"] is False
    assert publication["main_write_enabled"] is False

    assert publisher.run_once() == ()
    assert len(client.pulls) == 1


def test_newly_published_job_is_processed_and_verified(tmp_path: Path) -> None:
    config_path, product_bare, _, job_id = make_fixture(tmp_path)
    config = load_publisher_config(config_path)
    publisher = ControlledPublisher(config, github_client=FakeGitHubClient(product_bare))

    assert publisher.run_once() == (job_id,)

    success = publisher_success(config_path)
    assert success["associated_jobs"] == [job_id]
    assert success["terminal_evidence"]["processed_jobs"] == [job_id]
    assert success["terminal_evidence"]["verified_jobs"] == [job_id]


def test_existing_valid_ledger_entry_is_verified_not_processed(tmp_path: Path) -> None:
    config_path, product_bare, _, job_id = make_fixture(tmp_path)
    config = load_publisher_config(config_path)
    client = FakeGitHubClient(product_bare)
    publisher = ControlledPublisher(config, github_client=client)
    assert publisher.run_once() == (job_id,)

    assert publisher.run_once() == ()

    success = publisher_success(config_path)
    assert success["associated_jobs"] == [job_id]
    assert success["terminal_evidence"]["processed_jobs"] == []
    assert success["terminal_evidence"]["verified_jobs"] == [job_id]


def test_configured_cycle_cannot_claim_unplanned_historical_job_verified(
    tmp_path: Path,
) -> None:
    config_path, product_bare, _, job_id = make_fixture(tmp_path)
    config = load_publisher_config(config_path)
    publisher = ControlledPublisher(config, github_client=FakeGitHubClient(product_bare))

    assert publisher.run_once() == (job_id,)

    success = publisher_success(config_path)
    assert "historical-job" not in success["associated_jobs"]
    assert "historical-job" not in success["terminal_evidence"]["verified_jobs"]


def test_controlled_publisher_ledger_save_failure_records_job_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, product_bare, _, job_id = make_fixture(tmp_path)
    config = load_publisher_config(config_path)
    publisher = ControlledPublisher(config, github_client=FakeGitHubClient(product_bare))

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise OSError("publisher ledger save failed")

    monkeypatch.setattr(publisher, "_save_ledger", fail_save)

    with pytest.raises(OSError, match="publisher ledger save failed"):
        publisher.run_once()

    marker = json.loads(
        (config.host_root / "state" / "controlled-publisher-error.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["service"] == "publisher"
    assert marker["associated"]["job_id"] == job_id
    assert marker["associated"]["repository"] == "owner/product"


def test_controlled_publisher_rejects_overlapping_product_main_change(tmp_path: Path) -> None:
    config_path, product_bare, _, job_id = make_fixture(tmp_path, overlap=True)
    config = load_publisher_config(config_path)
    publisher = ControlledPublisher(config, github_client=FakeGitHubClient(product_bare))

    with pytest.raises(PublisherError, match="changed candidate paths"):
        publisher.run_once()
    marker = json.loads(
        (config.host_root / "state" / "controlled-publisher-error.json").read_text(
            encoding="utf-8"
        )
    )
    assert marker["service"] == "publisher"
    assert marker["associated"]["job_id"] == job_id
    assert marker["associated"]["repository"] == "owner/product"
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(product_bare),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/autobuilder/{job_id}",
        ],
        check=False,
    )
    assert proc.returncode == 1


def test_controlled_publisher_detects_branch_drift(tmp_path: Path) -> None:
    config_path, product_bare, _, job_id = make_fixture(tmp_path)
    config = load_publisher_config(config_path)
    client = FakeGitHubClient(product_bare)
    publisher = ControlledPublisher(config, github_client=client)
    assert publisher.run_once() == (job_id,)
    (config.host_root / "state" / "publisher-service-success.json").unlink()

    drift = tmp_path / "drift"
    git(None, "clone", str(product_bare), str(drift))
    git(drift, "config", "user.name", "Drift")
    git(drift, "config", "user.email", "drift@example.invalid")
    git(drift, "checkout", "-B", f"autobuilder/{job_id}", f"origin/autobuilder/{job_id}")
    (drift / "drift.txt").write_bytes(b"drift\n")
    git(drift, "add", ".")
    git(drift, "commit", "-m", "drift")
    git(drift, "push", "origin", f"HEAD:refs/heads/autobuilder/{job_id}")

    with pytest.raises(PublisherError, match="branch drifted"):
        publisher.run_once()
    assert not (config.host_root / "state" / "publisher-service-success.json").exists()


@pytest.mark.parametrize(
    "mutate_pull,error",
    [
        (lambda pull: pull.update({"state": "closed"}), "not open"),
        (lambda pull: pull.update({"draft": False}), "not draft"),
        (lambda pull: pull["head"].update({"sha": "0" * 40}), "head drifted"),
    ],
)
def test_controlled_publisher_pr_drift_prevents_verified_success(
    tmp_path: Path,
    mutate_pull: object,
    error: str,
) -> None:
    config_path, product_bare, _, job_id = make_fixture(tmp_path)
    config = load_publisher_config(config_path)
    client = FakeGitHubClient(product_bare)
    publisher = ControlledPublisher(config, github_client=client)
    assert publisher.run_once() == (job_id,)
    (config.host_root / "state" / "publisher-service-success.json").unlink()
    mutate_pull(client.pulls[0])

    with pytest.raises(PublisherError, match=error):
        publisher.run_once()
    assert not (config.host_root / "state" / "publisher-service-success.json").exists()


def test_controlled_publisher_gate_hash_drift_prevents_verified_success(
    tmp_path: Path,
) -> None:
    config_path, product_bare, evidence_bare, job_id = make_fixture(tmp_path)
    config = load_publisher_config(config_path)
    publisher = ControlledPublisher(config, github_client=FakeGitHubClient(product_bare))
    assert publisher.run_once() == (job_id,)
    (config.host_root / "state" / "publisher-service-success.json").unlink()

    edit = tmp_path / "evidence-gate-drift"
    git(None, "clone", "--branch", "results", str(evidence_bare), str(edit))
    git(edit, "config", "user.name", "Fixture")
    git(edit, "config", "user.email", "fixture@example.invalid")
    gate_path = edit / "results" / "MACHINE" / job_id / "gate-report.json"
    gate = json.loads(gate_path.read_text(encoding="utf-8"))
    gate["checks"].append({"name": "drift", "passed": True, "returncode": 0})
    write_json(gate_path, gate)
    git(edit, "add", ".")
    git(edit, "commit", "-m", "gate drift")
    git(edit, "push", "origin", "HEAD:results")

    with pytest.raises(PublisherError, match="passed gate report changed"):
        publisher.run_once()
    assert not (config.host_root / "state" / "publisher-service-success.json").exists()


def test_config_rejects_merge_or_main_write_authority(tmp_path: Path) -> None:
    config_path, _, _, _ = make_fixture(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace("merge_enabled: false", "merge_enabled: true"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="merge authority"):
        load_publisher_config(config_path)


def test_controlled_publisher_rejects_source_report_only_evidence(tmp_path: Path) -> None:
    config_path, product_bare, evidence_bare, job_id = make_fixture(tmp_path)
    edit = tmp_path / "evidence-edit"
    git(None, "clone", "--branch", "results", str(evidence_bare), str(edit))
    git(edit, "config", "user.name", "Fixture")
    git(edit, "config", "user.email", "fixture@example.invalid")
    job_dir = edit / "results" / "MACHINE" / job_id
    source = json.loads((job_dir / "source-report.json").read_text(encoding="utf-8"))
    write_json(job_dir / "report.json", source)
    report_hash = sha256(job_dir / "report.json")
    gate = json.loads((job_dir / "gate-report.json").read_text(encoding="utf-8"))
    gate["source_report_sha256"] = report_hash
    write_json(job_dir / "gate-report.json", gate)
    integrity = json.loads((job_dir / "result-integrity.json").read_text(encoding="utf-8"))
    integrity["corrected_report_sha256"] = report_hash
    integrity["source_report_sha256"] = report_hash
    write_json(job_dir / "result-integrity.json", integrity)
    git(edit, "add", ".")
    git(edit, "commit", "-m", "source report only")
    git(edit, "push", "origin", "HEAD:results")

    publisher = ControlledPublisher(
        load_publisher_config(config_path),
        github_client=FakeGitHubClient(product_bare),
    )
    with pytest.raises(PublisherError, match="corrected canonical report"):
        publisher.run_once()
