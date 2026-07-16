from __future__ import annotations

import base64
import json
from typing import Any

from msos_autobuilder.continuous_improvement import (
    PlannerConfig,
    collect_github_evidence,
    propose_one_improvement,
    rank_opportunities,
)


class FakeGitHub:
    def __init__(
        self,
        *,
        issues: list[dict[str, Any]],
        prs: list[dict[str, Any]] | None = None,
        result_reports: dict[str, dict[str, Any]] | None = None,
    ):
        self.issues = issues
        self.prs = prs or []
        self.result_reports = result_reports or {}
        self.text_calls: list[list[str]] = []
        self.json_calls: list[list[str]] = []

    def run_json(self, args: list[str]) -> Any:
        self.json_calls.append(args)
        if args[:2] == ["issue", "list"]:
            if "--label" in args:
                return [
                    item
                    for item in self.issues
                    if "msos-continuous-improvement:" in str(item.get("body") or "")
                ]
            return self.issues
        if args[:2] == ["pr", "list"]:
            return self.prs
        if args[:2] == ["issue", "view"]:
            return {"number": 99, "url": "https://github.com/DanielTabakman/msos-autobuilder/issues/99"}
        if args and args[0] == "api":
            endpoint = args[1]
            if endpoint.endswith("/git/trees/results?recursive=1"):
                return {
                    "tree": [
                        {"path": path, "type": "blob"}
                        for path in self.result_reports
                    ]
                }
            marker = "/contents/"
            if marker in endpoint:
                path = endpoint.split(marker, 1)[1].split("?ref=", 1)[0]
                content = json.dumps(self.result_reports[path]).encode("utf-8")
                return {"content": base64.b64encode(content).decode("ascii")}
        raise AssertionError(f"unexpected JSON call: {args}")

    def run_text(self, args: list[str]) -> str:
        self.text_calls.append(args)
        if args[:2] == ["issue", "create"]:
            return "https://github.com/DanielTabakman/msos-autobuilder/issues/99"
        return ""


def _issue33(*, state: str = "OPEN", body: str | None = None) -> dict[str, Any]:
    return {
        "number": 33,
        "title": "Add bounded continuous-improvement planner",
        "state": state,
        "body": body
        or "Founder priority: remove recurring Autobuilder-improvement orchestration.",
        "comments": [],
        "url": "https://github.com/DanielTabakman/msos-autobuilder/issues/33",
        "labels": [],
    }


def test_collects_durable_issue_evidence() -> None:
    client = FakeGitHub(issues=[_issue33()])

    evidence, _issues, _prs = collect_github_evidence(client, PlannerConfig())

    assert evidence == []


def _gate_report(*, status: str, state: str, message: str, job_id: str) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": status,
        "state": state,
        "finished_at": "2026-07-15T22:24:12+00:00",
        "errors": [{"message": message, "type": "CandidateGateError"}],
        "product_write_performed": False,
        "publication_enabled": False,
    }


def _update_report(*, outcome: str, attempt_id: str) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "attempted_at": "2026-07-13T09:14:13+00:00",
        "outcome": outcome,
    }


def test_collects_real_results_branch_gate_evidence() -> None:
    client = FakeGitHub(
        issues=[_issue33()],
        result_reports={
            "results/host/job-a/gate-report.json": _gate_report(
                status="unvalidated",
                state="awaiting_validation",
                message="immutable job predates candidate_validation",
                job_id="job-a",
            ),
            "results/host/job-b/gate-report.json": _gate_report(
                status="passed",
                state="candidate_passed",
                message="",
                job_id="job-b",
            ),
        },
    )

    evidence, _issues, _prs = collect_github_evidence(client, PlannerConfig())

    assert [item.evidence_id for item in evidence] == ["gate-report-job-a"]
    assert evidence[0].source_kind == "gate_report"
    assert evidence[0].recurrence_key == "candidate-gate-result-patterns"


def test_ranks_post_bootstrap_results_evidence_without_recreating_phase1() -> None:
    client = FakeGitHub(
        issues=[_issue33()],
        result_reports={
            "results/host/job-a/gate-report.json": _gate_report(
                status="failed",
                state="candidate_failed",
                message="dependency source SHA-256 does not match contract",
                job_id="job-a",
            )
        },
    )
    evidence, _issues, _prs = collect_github_evidence(client, PlannerConfig())

    ranked = rank_opportunities(evidence)

    assert ranked[0].opportunity_id == "candidate-gate-result-patterns"
    assert all(item.opportunity_id != "phase1-proposal-automation" for item in ranked)


def test_creates_only_one_issue_for_top_opportunity() -> None:
    client = FakeGitHub(
        issues=[_issue33()],
        result_reports={
            "results/host/job-a/gate-report.json": _gate_report(
                status="failed",
                state="candidate_failed",
                message="dependency source SHA-256 does not match contract",
                job_id="job-a",
            )
        },
    )

    result = propose_one_improvement(client, PlannerConfig())

    assert result.status == "created"
    assert result.issue_number == 99
    create_calls = [call for call in client.text_calls if call[:2] == ["issue", "create"]]
    assert len(create_calls) == 1
    assert result.digest.startswith("Created issue #99")


def test_deduplicates_against_open_issue_without_digest_noise() -> None:
    existing = {
        "number": 77,
        "title": "Summarize recurring candidate-gate result patterns",
        "state": "OPEN",
        "body": (
            "Gate result patterns\n"
            "<!-- msos-continuous-improvement:candidate-gate-result-patterns -->"
        ),
        "comments": [],
        "url": "https://github.com/DanielTabakman/msos-autobuilder/issues/77",
    }
    client = FakeGitHub(
        issues=[_issue33(), existing],
        result_reports={
            "results/host/job-a/gate-report.json": _gate_report(
                status="failed",
                state="candidate_failed",
                message="dependency source SHA-256 does not match contract",
                job_id="job-a",
            )
        },
    )

    result = propose_one_improvement(client, PlannerConfig())

    assert result.status in {"unchanged", "updated"}
    assert result.issue_number == 77
    assert not [call for call in client.text_calls if call[:2] == ["issue", "create"]]


def test_suppresses_duplicate_open_pr() -> None:
    client = FakeGitHub(
        issues=[_issue33()],
        result_reports={
            "results/host/job-a/gate-report.json": _gate_report(
                status="failed",
                state="candidate_failed",
                message="dependency source SHA-256 does not match contract",
                job_id="job-a",
            )
        },
        prs=[
            {
                "number": 12,
                "title": "Summarize recurring candidate-gate result patterns",
                "state": "OPEN",
                "body": "<!-- msos-continuous-improvement:candidate-gate-result-patterns -->",
                "url": "https://github.com/DanielTabakman/msos-autobuilder/pull/12",
            }
        ],
    )

    result = propose_one_improvement(client, PlannerConfig())

    assert result.status == "suppressed"
    assert result.issue_number is None
    assert "duplicates or noise" in result.digest


def test_control_plane_and_generated_planner_issues_are_not_evidence() -> None:
    generated = _issue33(
        body=(
            "Generated issue\n"
            "<!-- msos-continuous-improvement:continuous-improvement-phase1 -->"
        )
    )
    generated["number"] = 61
    client = FakeGitHub(issues=[_issue33(), generated])

    evidence, _issues, _prs = collect_github_evidence(client, PlannerConfig())

    assert evidence == []


def test_dry_run_reports_would_create_without_mutating() -> None:
    client = FakeGitHub(
        issues=[_issue33()],
        result_reports={
            "results/host/job-a/gate-report.json": _gate_report(
                status="failed",
                state="candidate_failed",
                message="dependency source SHA-256 does not match contract",
                job_id="job-a",
            )
        },
    )

    result = propose_one_improvement(client, PlannerConfig(dry_run=True))

    assert result.status == "would_create"
    assert result.dry_run is True
    assert not [call for call in client.text_calls if call[:2] == ["issue", "create"]]


def test_human_owned_semantic_duplicate_is_not_overwritten() -> None:
    human = {
        "number": 88,
        "title": "Candidate gate result patterns need human review",
        "state": "OPEN",
        "body": "Human-authored packet without a planner fingerprint.",
        "comments": [],
        "url": "https://github.com/DanielTabakman/msos-autobuilder/issues/88",
    }
    client = FakeGitHub(
        issues=[human],
        result_reports={
            "results/host/job-a/gate-report.json": _gate_report(
                status="failed",
                state="candidate_failed",
                message="dependency source SHA-256 does not match contract",
                job_id="job-a",
            )
        },
    )

    result = propose_one_improvement(client, PlannerConfig())

    assert result.status == "suppressed"
    assert not [call for call in client.text_calls if call[:2] == ["issue", "edit"]]
    assert not [call for call in client.text_calls if call[:2] == ["issue", "create"]]


def test_older_planner_owned_duplicate_is_found_by_label_query() -> None:
    old_owned = {
        "number": 144,
        "title": "Summarize recurring candidate-gate result patterns",
        "state": "OPEN",
        "body": "<!-- msos-continuous-improvement:candidate-gate-result-patterns -->",
        "comments": [],
        "url": "https://github.com/DanielTabakman/msos-autobuilder/issues/144",
    }
    client = FakeGitHub(
        issues=[old_owned],
        result_reports={
            "results/host/job-a/gate-report.json": _gate_report(
                status="failed",
                state="candidate_failed",
                message="dependency source SHA-256 does not match contract",
                job_id="job-a",
            )
        },
    )

    result = propose_one_improvement(client, PlannerConfig(limit_issues=0))

    assert result.issue_number == 144
    assert not [call for call in client.text_calls if call[:2] == ["issue", "create"]]


def test_self_update_report_is_operating_evidence_but_not_phase1_seed() -> None:
    client = FakeGitHub(
        issues=[_issue33()],
        result_reports={
            "results/host/self-updates/a/update-report.json": _update_report(
                outcome="rollback_failed",
                attempt_id="a",
            )
        },
    )

    evidence, _issues, _prs = collect_github_evidence(client, PlannerConfig())

    assert [item.recurrence_key for item in evidence] == ["self-update-rollback-gap"]


def test_self_update_rollback_human_lane_is_suppressed_before_next_opportunity() -> None:
    human = {
        "number": 58,
        "title": "Add reviewed six-service stable-bootstrap handoff",
        "state": "OPEN",
        "body": "Reopened for self-update supervisor rollback compatibility.",
        "comments": [],
        "url": "https://github.com/DanielTabakman/msos-autobuilder/issues/58",
    }
    client = FakeGitHub(
        issues=[human],
        result_reports={
            "results/host/self-updates/a/update-report.json": _update_report(
                outcome="rollback_failed",
                attempt_id="a",
            ),
            "results/host/job-a/gate-report.json": _gate_report(
                status="failed",
                state="candidate_failed",
                message="dependency source SHA-256 does not match contract",
                job_id="job-a",
            ),
        },
    )

    result = propose_one_improvement(client, PlannerConfig(dry_run=True))

    assert result.status == "would_create"
    assert result.opportunity_id == "candidate-gate-result-patterns"
