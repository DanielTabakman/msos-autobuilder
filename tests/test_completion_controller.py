from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from msos_autobuilder.completion_controller import (
    AUTHORITY_AUTO_MERGE,
    AUTHORITY_FOUNDER_REQUIRED,
    AUTHORITY_NEVER_MERGE,
    CompletionController,
    CompletionControllerError,
    CompletionGitHubClient,
    load_completion_config,
)


def git(repo: Path | None, *args: str, accepted: tuple[int, ...] = (0,)) -> str:
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
    assert proc.returncode in accepted, proc.stderr or proc.stdout
    return proc.stdout.strip()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class FakeCompletionGitHubClient(CompletionGitHubClient):
    def __init__(self, product_bare: Path, *, head: str, branch: str, base: str = "main") -> None:
        self.product_bare = product_bare
        self.repo_full_name = "owner/product"
        self.branch = branch
        self.base = base
        self.head = head
        self.mergeable = True
        self.mergeable_state = "clean"
        self.review_decision = "APPROVED"
        self.unresolved_review_threads = 0
        self.canon_conflict = False
        self.evidence_conflict = False
        self.ownership_conflict = False
        self.founder_decision_required = False
        self.checks = [{"name": "linux-ci", "state": "success", "source": "check_run"}]
        self.delete_branch_error: Exception | None = None
        self.merge_result_ok = True
        self.merge_calls = 0
        self.delete_calls = 0
        self.reread_head: str | None = None

    def get_pull_request(self, number: int) -> dict[str, Any]:
        current_head = self.reread_head if self.merge_calls == 0 and self.reread_head else self.head
        merged = self.merge_calls > 0
        return {
            "number": number,
            "state": "closed" if merged else "open",
            "merged": merged,
            "merge_commit_sha": (
                git(self.product_bare, "rev-parse", "refs/heads/main") if merged else None
            ),
            "mergeable": self.mergeable,
            "mergeable_state": self.mergeable_state,
            "review_decision": self.review_decision,
            "unresolved_review_threads": self.unresolved_review_threads,
            "canon_conflict": self.canon_conflict,
            "evidence_conflict": self.evidence_conflict,
            "ownership_conflict": self.ownership_conflict,
            "founder_decision_required": self.founder_decision_required,
            "head": {"ref": self.branch, "sha": current_head},
            "base": {"ref": self.base},
        }

    def checks_for_ref(self, ref: str) -> list[dict[str, Any]]:
        assert ref == self.head
        return list(self.checks)

    def merge_pull_request(
        self,
        number: int,
        *,
        expected_head: str,
        method: str,
    ) -> dict[str, Any]:
        if expected_head != self.head:
            raise CompletionControllerError("exact-head guard rejected stale merge")
        if not self.merge_result_ok:
            return {"merged": False, "message": "not merged"}
        clone = self.product_bare.parent / f"merge-{self.merge_calls}"
        git(None, "clone", str(self.product_bare), str(clone))
        git(clone, "config", "user.name", "Fixture")
        git(clone, "config", "user.email", "fixture@example.invalid")
        git(clone, "checkout", "main")
        merge = subprocess.run(
            ["git", "-C", str(clone), "merge", "--no-ff", expected_head, "-m", "Merge PR"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if merge.returncode != 0:
            raise CompletionControllerError("merge conflict")
        git(clone, "push", "origin", "HEAD:main")
        self.merge_calls += 1
        return {"merged": True, "sha": git(clone, "rev-parse", "HEAD")}

    def delete_branch(self, branch: str) -> dict[str, Any]:
        self.delete_calls += 1
        if self.delete_branch_error is not None:
            raise self.delete_branch_error
        git(self.product_bare, "update-ref", "-d", f"refs/heads/{branch}", accepted=(0,))
        return {}


def make_fixture(
    tmp_path: Path,
    *,
    authority: str = AUTHORITY_AUTO_MERGE,
    unauthorized_path: bool = False,
    conflict: bool = False,
    post_hoc_authority: bool = False,
) -> tuple[Path, Path, str, FakeCompletionGitHubClient, str]:
    product_work = tmp_path / "product-work"
    product_work.mkdir(parents=True)
    git(product_work, "init", "-b", "main")
    git(product_work, "config", "user.name", "Fixture")
    git(product_work, "config", "user.email", "fixture@example.invalid")
    value = product_work / "src" / "viz" / "value.py"
    value.parent.mkdir(parents=True)
    value.write_text("VALUE = 1\n", encoding="utf-8")
    git(product_work, "add", ".")
    git(product_work, "commit", "-m", "source")
    source_head = git(product_work, "rev-parse", "HEAD")

    branch = "autobuilder/candidate-revision-1"
    git(product_work, "checkout", "-b", branch)
    changed = product_work / ("src/other/value.py" if unauthorized_path else "src/viz/value.py")
    changed.parent.mkdir(parents=True, exist_ok=True)
    changed.write_text("VALUE = 2\n", encoding="utf-8")
    git(product_work, "add", ".")
    git(product_work, "commit", "-m", "candidate")
    candidate_head = git(product_work, "rev-parse", "HEAD")
    git(product_work, "checkout", "main")
    if conflict:
        value.write_text("VALUE = 3\n", encoding="utf-8")
        git(product_work, "add", ".")
        git(product_work, "commit", "-m", "conflict")

    product_bare = tmp_path / "product.git"
    git(None, "clone", "--bare", str(product_work), str(product_bare))

    evidence_work = tmp_path / "evidence-work"
    evidence_work.mkdir()
    git(evidence_work, "init", "-b", "results")
    git(evidence_work, "config", "user.name", "Fixture")
    git(evidence_work, "config", "user.email", "fixture@example.invalid")
    job_id = "candidate-revision-1"
    job_dir = evidence_work / "results" / "MACHINE" / job_id
    path_text = changed.relative_to(product_work).as_posix()
    report = {
        "version": 1,
        "job_id": job_id,
        "outcome": "completed",
        "publication_enabled": False,
        "codex_report": {"source_head": source_head, "status": "completed"},
        "relay": {
            "version": 1,
            "publication_enabled": False,
            "complete_patch_reconstruction": True,
            "source_report_sha256": "b" * 64,
        },
        "patches": [
            {
                "task_id": job_id,
                "patch_file": "patches/candidate.patch",
                "patch_sha256": "c" * 64,
                "changed_paths": [path_text],
                "complete_patch": True,
            }
        ],
    }
    write_json(job_dir / "report.json", report)
    report_sha = sha256(job_dir / "report.json")
    write_json(
        job_dir / "result-integrity.json",
        {
            "version": 1,
            "source_report_sha256": "b" * 64,
            "corrected_report_sha256": report_sha,
            "canonical_patch_sha256_by_task": {job_id: "c" * 64},
            "publication_enabled": False,
        },
    )
    gate = {
        "version": 1,
        "job_id": job_id,
        "status": "passed",
        "state": "candidate_passed",
        "source_head": source_head,
        "source_report_sha256": report_sha,
        "changed_paths": [path_text],
        "checks": [{"name": "gate", "passed": True, "returncode": 0}],
        "policy_blocks": [],
        "errors": [],
        "publication_enabled": False,
        "product_write_performed": False,
        "workspace_removed": True,
    }
    write_json(job_dir / "gate-report.json", gate)
    write_json(
        job_dir / "publication-report.json",
        {
            "version": 1,
            "job_id": job_id,
            "status": "published-draft",
            "published_at": "2026-07-20T00:00:00+00:00",
            "draft": True,
            "merge_enabled": False,
            "main_write_enabled": False,
            "product_base_branch": "main",
            "product_branch": branch,
            "product_commit": candidate_head,
            "pr_number": 7,
            "pr_url": "https://example.invalid/pull/7",
            "gate_report_sha256": sha256(job_dir / "gate-report.json"),
            "source_report_sha256": report_sha,
            "changed_paths": [path_text],
            "checks": [{"name": "publication-check", "passed": True, "returncode": 0}],
        },
    )
    declared_at = "2026-07-19T00:00:00+00:00"
    if post_hoc_authority:
        declared_at = "2026-07-21T00:00:00+00:00"
    (job_dir / "job.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                f"job_id: {job_id}",
                "approved: true",
                "approved_at: '2026-07-20T00:00:00+00:00'",
                "publication_enabled: false",
                "merge_authority:",
                f"  class: {authority}",
                f"  declared_at: '{declared_at}'",
                "founder_build_next:",
                "  pipeline_id: ppe",
                "  work_item_id: fixture-work",
                "candidate_validation:",
                "  target_repository: owner/product",
                f"  source_commit: {source_head}",
                "  allowed_changed_paths:",
                "    - src/viz",
                "  forbidden_changed_paths:",
                "    - src/secret",
                "",
            ]
        ),
        encoding="utf-8",
    )
    git(evidence_work, "add", ".")
    git(evidence_work, "commit", "-m", "completion evidence")
    evidence_bare = tmp_path / "evidence.git"
    git(None, "clone", "--bare", str(evidence_work), str(evidence_bare))

    cleanup_path = tmp_path / "workspace-to-clean"
    cleanup_path.mkdir()
    config = tmp_path / "completion.yaml"
    config.write_text(
        f"""
version: 1
host_root: {tmp_path.as_posix()}/host
evidence_repo_url: {evidence_bare.as_posix()}
results_branch: results
product_repo_url: {product_bare.as_posix()}
product_repo_full_name: owner/product
product_base_branch: main
machine_id: MACHINE
poll_seconds: 1
required_checks:
  - linux-ci
merge_method: merge
plans:
  {job_id}:
    cleanup_paths:
      - {cleanup_path.as_posix()}
""".lstrip(),
        encoding="utf-8",
    )
    client = FakeCompletionGitHubClient(product_bare, head=candidate_head, branch=branch)
    return config, product_bare, candidate_head, client, job_id


def controller(config_path: Path, client: FakeCompletionGitHubClient) -> CompletionController:
    return CompletionController(load_completion_config(config_path), github_client=client)


def test_eligible_exact_head_merge_cleanup_terminalization_and_digest(tmp_path: Path) -> None:
    config_path, product_bare, head, client, job_id = make_fixture(tmp_path)

    assert controller(config_path, client).run_once() == (job_id,)

    main = git(product_bare, "rev-parse", "refs/heads/main")
    assert git(product_bare, "merge-base", "--is-ancestor", head, main, accepted=(0,)) == ""
    assert client.delete_calls == 1
    state = load_completion_config(config_path).host_root / "state"
    terminal = json.loads(
        (state / "completion-terminal-work-items" / f"{job_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert terminal["status"] == "merged"
    assert terminal["authority_class"] == AUTHORITY_AUTO_MERGE
    ledger = json.loads((state / "completion-controller-seen.json").read_text(encoding="utf-8"))
    assert "Merged automatically" in ledger[job_id]["founder_digest"]

    assert controller(config_path, client).run_once() == ()
    assert client.merge_calls == 1


def test_changed_head_fails_before_merge(tmp_path: Path) -> None:
    config_path, _, _, client, _ = make_fixture(tmp_path)
    client.head = "0" * 40
    with pytest.raises(CompletionControllerError, match="head changed"):
        controller(config_path, client).run_once()
    assert client.merge_calls == 0


@pytest.mark.parametrize(
    ("state", "message"),
    [
        ("pending", "pending"),
        ("cancelled", "cancelled"),
        ("failure", "failed"),
    ],
)
def test_missing_pending_cancelled_and_failed_checks_block(
    tmp_path: Path,
    state: str,
    message: str,
) -> None:
    config_path, _, _, client, _ = make_fixture(tmp_path)
    client.checks = [{"name": "linux-ci", "state": state, "source": "check_run"}]
    with pytest.raises(CompletionControllerError, match=message):
        controller(config_path, client).run_once()
    assert client.merge_calls == 0


def test_missing_check_blocks(tmp_path: Path) -> None:
    config_path, _, _, client, _ = make_fixture(tmp_path)
    client.checks = []
    with pytest.raises(CompletionControllerError, match="missing"):
        controller(config_path, client).run_once()


def test_merge_conflict_blocks(tmp_path: Path) -> None:
    config_path, _, _, client, _ = make_fixture(tmp_path, conflict=True)
    with pytest.raises(CompletionControllerError, match="merge conflict"):
        controller(config_path, client).run_once()


def test_unauthorized_paths_block(tmp_path: Path) -> None:
    config_path, _, _, client, _ = make_fixture(tmp_path, unauthorized_path=True)
    with pytest.raises(CompletionControllerError, match="exceed authority"):
        controller(config_path, client).run_once()


def test_post_hoc_authority_blocks(tmp_path: Path) -> None:
    config_path, _, _, client, _ = make_fixture(tmp_path, post_hoc_authority=True)
    with pytest.raises(CompletionControllerError, match="after implementation approval"):
        controller(config_path, client).run_once()


def test_founder_and_never_merge_authorities_block(tmp_path: Path) -> None:
    for authority, message in (
        (AUTHORITY_FOUNDER_REQUIRED, "FOUNDER_DECISION_REQUIRED"),
        (AUTHORITY_NEVER_MERGE, "NEVER_MERGE"),
    ):
        config_path, _, _, client, _ = make_fixture(tmp_path / authority, authority=authority)
        with pytest.raises(CompletionControllerError, match=message):
            controller(config_path, client).run_once()


def test_unresolved_conflicts_block(tmp_path: Path) -> None:
    config_path, _, _, client, _ = make_fixture(tmp_path)
    client.ownership_conflict = True
    with pytest.raises(CompletionControllerError, match="canon/evidence/ownership"):
        controller(config_path, client).run_once()


def test_post_merge_verification_failure_blocks_completion(tmp_path: Path) -> None:
    config_path, product_bare, _, client, _ = make_fixture(tmp_path)
    old_push = client.merge_pull_request

    def merge_without_default_update(
        number: int,
        *,
        expected_head: str,
        method: str,
    ) -> dict[str, Any]:
        client.merge_calls += 1
        return {"merged": True, "sha": git(product_bare, "rev-parse", expected_head)}

    client.merge_pull_request = merge_without_default_update  # type: ignore[method-assign]
    with pytest.raises(CompletionControllerError, match="validated head is not preserved"):
        controller(config_path, client).run_once()
    client.merge_pull_request = old_push  # type: ignore[method-assign]


def test_cleanup_failure_preserves_valid_merge_as_maintenance_state(tmp_path: Path) -> None:
    config_path, _, _, client, job_id = make_fixture(tmp_path)
    client.delete_branch_error = RuntimeError("delete denied")

    assert controller(config_path, client).run_once() == (job_id,)

    state = load_completion_config(config_path).host_root / "state"
    ledger = json.loads((state / "completion-controller-seen.json").read_text(encoding="utf-8"))
    cleanup = ledger[job_id]["cleanup"]
    assert cleanup[0]["status"] == "maintenance_required"
    assert "maintenance required" in ledger[job_id]["founder_digest"]


def test_cli_parser_loads_completion_config(tmp_path: Path) -> None:
    config_path, _, _, _, _ = make_fixture(tmp_path)
    config = load_completion_config(config_path)
    assert config.required_checks == ("linux-ci",)
    assert config.merge_method == "merge"
    assert config.product_repo_full_name == "owner/product"
