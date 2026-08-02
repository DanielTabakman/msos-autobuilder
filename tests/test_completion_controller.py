from __future__ import annotations

import hashlib
import json
import shutil
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
        self.review_evidence_error: Exception | None = None

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

    def review_evidence(self, number: int) -> dict[str, Any]:
        if self.review_evidence_error is not None:
            raise self.review_evidence_error
        return {
            "source": "fake_graphql",
            "review_decision": self.review_decision,
            "unresolved_review_threads": self.unresolved_review_threads,
            "latest_opinionated_reviews": [],
        }

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
    omit_readiness: bool = False,
    omit_revision_lineage: bool = False,
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
    if not omit_readiness:
        write_json(
            job_dir / "completion-readiness.json",
            {
                "version": 1,
                "job_id": job_id,
                "pr_number": 7,
                "product_commit": candidate_head,
                "canon_conflict": False,
                "evidence_conflict": False,
                "ownership_conflict": False,
                "founder_decision_required": False,
            },
        )
    if not omit_revision_lineage:
        write_json(
            job_dir / "revision-lineage.json",
            {
                "version": 1,
                "job_id": job_id,
                "revision_disposition": "not_applicable",
                "not_applicable_evidence": True,
                "product_commit": candidate_head,
                "gate_report_sha256": sha256(job_dir / "gate-report.json"),
                "publication_report_sha256": sha256(job_dir / "publication-report.json"),
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
                "  work_item_source_sha256_v1: " + ("d" * 64),
                "  refill_attempt:",
                "    generation_id: refill-generation-1",
                "    attempt_ordinal: 1",
                "    retry_ordinal: 0",
                "    selected_work_item_id: fixture-work",
                "    reason: initial",
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

    cleanup_root = tmp_path / "managed-cleanup"
    cleanup_path = cleanup_root / "workspace-to-clean"
    cleanup_path.mkdir(parents=True)
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
    managed_cleanup_roots:
      - {cleanup_root.as_posix()}
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
    assert (state / "publisher-evidence" / "heads" / "publication-review").exists()

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
        config_path, _, _, client, job_id = make_fixture(tmp_path / authority, authority=authority)
        assert controller(config_path, client).run_once() == ()
        assert client.merge_calls == 0
        state = load_completion_config(config_path).host_root / "state"
        ledger = json.loads((state / "completion-controller-seen.json").read_text("utf-8"))
        assert ledger[job_id]["status"] == "escalated"
        assert message in ledger[job_id]["reason"]


def test_founder_decision_item_does_not_block_later_eligible_job(tmp_path: Path) -> None:
    config_path, _, _, client, job_id = make_fixture(
        tmp_path,
        authority=AUTHORITY_FOUNDER_REQUIRED,
    )
    config = load_completion_config(config_path)
    evidence_bare = Path(config.evidence_repo_url)
    work = evidence_bare.parent / "two-jobs"
    git(None, "clone", str(evidence_bare), str(work))
    git(work, "config", "user.name", "Fixture")
    git(work, "config", "user.email", "fixture@example.invalid")
    source = work / "results" / "MACHINE" / job_id
    second_id = "candidate-revision-2"
    target = source.parent / second_id
    shutil.copytree(source, target)
    for path in target.glob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("job_id") == job_id:
            payload["job_id"] = second_id
        write_json(path, payload)
    report_sha = sha256(target / "report.json")
    integrity = json.loads((target / "result-integrity.json").read_text(encoding="utf-8"))
    integrity["corrected_report_sha256"] = report_sha
    write_json(target / "result-integrity.json", integrity)
    gate = json.loads((target / "gate-report.json").read_text(encoding="utf-8"))
    gate["source_report_sha256"] = report_sha
    write_json(target / "gate-report.json", gate)
    gate_sha = sha256(target / "gate-report.json")
    publication = json.loads((target / "publication-report.json").read_text(encoding="utf-8"))
    publication["gate_report_sha256"] = gate_sha
    write_json(target / "publication-report.json", publication)
    publication_sha = sha256(target / "publication-report.json")
    lineage = json.loads((target / "revision-lineage.json").read_text(encoding="utf-8"))
    lineage["gate_report_sha256"] = gate_sha
    lineage["publication_report_sha256"] = publication_sha
    write_json(target / "revision-lineage.json", lineage)
    target_job = (target / "job.yaml").read_text(encoding="utf-8")
    target_job = target_job.replace(f"job_id: {job_id}", f"job_id: {second_id}")
    target_job = target_job.replace(
        f"  class: {AUTHORITY_FOUNDER_REQUIRED}",
        f"  class: {AUTHORITY_AUTO_MERGE}",
    )
    (target / "job.yaml").write_text(target_job, encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-m", "add eligible second job")
    git(work, "push", "origin", "HEAD:results")
    config_path.write_text(
        config_path.read_text(encoding="utf-8").split("plans:", 1)[0],
        encoding="utf-8",
    )

    assert controller(config_path, client).run_once() == (second_id,)
    ledger = json.loads(
        (config.host_root / "state" / "completion-controller-seen.json").read_text("utf-8")
    )
    assert ledger[job_id]["status"] == "escalated"
    assert ledger[second_id]["status"] == "merged"


def test_unresolved_conflicts_block(tmp_path: Path) -> None:
    config_path, _, _, client, job_id = make_fixture(tmp_path)
    readiness = (
        load_completion_config(config_path).host_root
        / "state"
        / "completion-results-repo"
        / "results"
        / "MACHINE"
        / job_id
        / "completion-readiness.json"
    )
    # The controller reads evidence from the results checkout; change the bare evidence source.
    with pytest.raises(FileNotFoundError):
        readiness.read_text(encoding="utf-8")
    evidence_bare = Path(load_completion_config(config_path).evidence_repo_url)
    work = evidence_bare.parent / "readiness-edit"
    git(None, "clone", str(evidence_bare), str(work))
    git(work, "config", "user.name", "Fixture")
    git(work, "config", "user.email", "fixture@example.invalid")
    path = work / "results" / "MACHINE" / job_id / "completion-readiness.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["ownership_conflict"] = True
    write_json(path, payload)
    git(work, "add", ".")
    git(work, "commit", "-m", "conflict readiness")
    git(work, "push", "origin", "HEAD:results")
    with pytest.raises(CompletionControllerError, match="ownership_conflict"):
        controller(config_path, client).run_once()


def test_crash_after_merge_before_report_recovers_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _, _, client, job_id = make_fixture(tmp_path)
    ctl = controller(config_path, client)
    real_publish = ctl.evidence.publish_report

    def crash_publish(job_dir: Path, payload: dict[str, Any]) -> str:
        raise RuntimeError("crash before report")

    monkeypatch.setattr(ctl.evidence, "publish_report", crash_publish)
    with pytest.raises(RuntimeError, match="crash before report"):
        ctl.run_once()
    assert client.merge_calls == 1

    recovered = controller(config_path, client)
    monkeypatch.setattr(recovered.evidence, "publish_report", real_publish)
    assert recovered.run_once() == (job_id,)
    assert client.merge_calls == 1


def test_missing_review_thread_evidence_fails_closed(tmp_path: Path) -> None:
    config_path, _, _, client, _ = make_fixture(tmp_path)
    client.review_evidence_error = CompletionControllerError(
        "review-thread evidence is unavailable"
    )
    with pytest.raises(CompletionControllerError, match="review-thread evidence"):
        controller(config_path, client).run_once()


@pytest.mark.parametrize(
    ("review_decision", "unresolved_threads", "message"),
    [
        ("CHANGES_REQUESTED", 0, "unresolved review"),
        ("APPROVED", 1, "unresolved review threads"),
    ],
)
def test_changes_requested_and_unresolved_threads_block(
    tmp_path: Path,
    review_decision: str,
    unresolved_threads: int,
    message: str,
) -> None:
    config_path, _, _, client, _ = make_fixture(tmp_path)
    client.review_decision = review_decision
    client.unresolved_review_threads = unresolved_threads
    with pytest.raises(CompletionControllerError, match=message):
        controller(config_path, client).run_once()


@pytest.mark.parametrize(
    ("omit", "mutate", "message"),
    [
        ("revision", None, "revision lineage evidence is missing"),
        ("none", {"revision_disposition": "queued"}, "revision lineage is not terminal"),
        ("none", {"product_commit": "0" * 40}, "stale or contradictory"),
    ],
)
def test_missing_stale_or_nonterminal_revision_lineage_blocks(
    tmp_path: Path,
    omit: str,
    mutate: dict[str, Any] | None,
    message: str,
) -> None:
    config_path, _, _, client, job_id = make_fixture(
        tmp_path,
        omit_revision_lineage=omit == "revision",
    )
    if mutate is not None:
        evidence_bare = Path(load_completion_config(config_path).evidence_repo_url)
        work = evidence_bare.parent / "revision-edit"
        git(None, "clone", str(evidence_bare), str(work))
        git(work, "config", "user.name", "Fixture")
        git(work, "config", "user.email", "fixture@example.invalid")
        path = work / "results" / "MACHINE" / job_id / "revision-lineage.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.update(mutate)
        write_json(path, payload)
        git(work, "add", ".")
        git(work, "commit", "-m", "mutate revision lineage")
        git(work, "push", "origin", "HEAD:results")
    with pytest.raises(CompletionControllerError, match=message):
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


def test_cleanup_outside_managed_roots_is_rejected(tmp_path: Path) -> None:
    config_path, _, _, client, _ = make_fixture(tmp_path)
    config = load_completion_config(config_path)
    raw = config_path.read_text(encoding="utf-8")
    outside = (tmp_path / "outside-cleanup").as_posix()
    (tmp_path / "outside-cleanup").mkdir()
    original_cleanup = next(iter(config.plans.values())).cleanup_paths[0].as_posix()
    config_path.write_text(
        raw.replace("      - " + original_cleanup, "      - " + outside),
        encoding="utf-8",
    )

    with pytest.raises(CompletionControllerError, match="outside approved managed roots"):
        controller(config_path, client).run_once()


def test_rebase_merge_method_is_rejected_in_v1(tmp_path: Path) -> None:
    config_path, _, _, _, _ = make_fixture(tmp_path)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "merge_method: merge",
            "merge_method: rebase",
        ),
        encoding="utf-8",
    )

    with pytest.raises((ValueError, CompletionControllerError), match="rebase is not supported"):
        load_completion_config(config_path)


def test_cli_parser_loads_completion_config(tmp_path: Path) -> None:
    config_path, _, _, _, _ = make_fixture(tmp_path)
    config = load_completion_config(config_path)
    assert config.required_checks == ("linux-ci",)
    assert config.merge_method == "merge"
    assert config.product_repo_full_name == "owner/product"
