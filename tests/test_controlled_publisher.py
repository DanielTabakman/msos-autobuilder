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
    value.write_text("VALUE = 1\n", encoding="utf-8")
    git(product_work, "add", ".")
    git(product_work, "commit", "-m", "source")
    source_head = git(product_work, "rev-parse", "HEAD")

    patch_work = tmp_path / "patch-work"
    git(None, "clone", str(product_work), str(patch_work))
    git(patch_work, "checkout", "--detach", source_head)
    (patch_work / "src" / "viz" / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
    patch = git(patch_work, "diff", "--binary", "HEAD") + "\n"

    if overlap:
        value.write_text("VALUE = 3\n", encoding="utf-8")
        git(product_work, "add", ".")
        git(product_work, "commit", "-m", "overlap")
    else:
        docs = product_work / "docs" / "note.md"
        docs.parent.mkdir(parents=True)
        docs.write_text("docs-only\n", encoding="utf-8")
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
    patch_path.write_text(patch, encoding="utf-8")
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
    gate_report = {
        "version": 1,
        "job_id": job_id,
        "status": "passed",
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


def test_controlled_publisher_rejects_overlapping_product_main_change(tmp_path: Path) -> None:
    config_path, product_bare, _, job_id = make_fixture(tmp_path, overlap=True)
    config = load_publisher_config(config_path)
    publisher = ControlledPublisher(config, github_client=FakeGitHubClient(product_bare))

    with pytest.raises(PublisherError, match="changed candidate paths"):
        publisher.run_once()
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

    drift = tmp_path / "drift"
    git(None, "clone", str(product_bare), str(drift))
    git(drift, "config", "user.name", "Drift")
    git(drift, "config", "user.email", "drift@example.invalid")
    git(drift, "checkout", "-B", f"autobuilder/{job_id}", f"origin/autobuilder/{job_id}")
    (drift / "drift.txt").write_text("drift\n", encoding="utf-8")
    git(drift, "add", ".")
    git(drift, "commit", "-m", "drift")
    git(drift, "push", "origin", f"HEAD:refs/heads/autobuilder/{job_id}")

    with pytest.raises(PublisherError, match="branch drifted"):
        publisher.run_once()


def test_config_rejects_merge_or_main_write_authority(tmp_path: Path) -> None:
    config_path, _, _, _ = make_fixture(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        text.replace("merge_enabled: false", "merge_enabled: true"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="merge authority"):
        load_publisher_config(config_path)
