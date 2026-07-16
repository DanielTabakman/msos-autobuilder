from __future__ import annotations

from typing import Any

from msos_autobuilder.continuous_improvement import (
    PlannerConfig,
    collect_github_evidence,
    propose_one_improvement,
    rank_opportunities,
)


class FakeGitHub:
    def __init__(self, *, issues: list[dict[str, Any]], prs: list[dict[str, Any]] | None = None):
        self.issues = issues
        self.prs = prs or []
        self.text_calls: list[list[str]] = []
        self.json_calls: list[list[str]] = []

    def run_json(self, args: list[str]) -> Any:
        self.json_calls.append(args)
        if args[:2] == ["issue", "list"]:
            return self.issues
        if args[:2] == ["pr", "list"]:
            return self.prs
        if args[:2] == ["issue", "view"]:
            return {"number": 99, "url": "https://github.com/DanielTabakman/msos-autobuilder/issues/99"}
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

    assert [item.evidence_id for item in evidence] == ["issue-33"]
    assert evidence[0].recurrence_key == "continuous-improvement-phase1"
    assert evidence[0].manual_steps_observed > 1


def test_ranks_founder_time_removed_first() -> None:
    client = FakeGitHub(
        issues=[
            _issue33(),
            {
                "number": 58,
                "title": "Rollback compatibility",
                "state": "OPEN",
                "body": "rollback witness and rollback_failed evidence",
                "comments": [],
                "url": "https://github.com/DanielTabakman/msos-autobuilder/issues/58",
            },
        ]
    )
    evidence, _issues, _prs = collect_github_evidence(client, PlannerConfig())

    ranked = rank_opportunities(evidence)

    assert ranked[0].opportunity_id == "phase1-proposal-automation"
    assert ranked[0].founder_minutes_saved_30d > ranked[1].founder_minutes_saved_30d


def test_creates_only_one_issue_for_top_opportunity() -> None:
    client = FakeGitHub(issues=[_issue33()])

    result = propose_one_improvement(client, PlannerConfig())

    assert result.status == "created"
    assert result.issue_number == 99
    create_calls = [call for call in client.text_calls if call[:2] == ["issue", "create"]]
    assert len(create_calls) == 1
    assert result.digest.startswith("Created issue #99")


def test_deduplicates_against_open_issue_without_digest_noise() -> None:
    existing = _issue33(
        body=(
            "Phase 1 implementation\n"
            "<!-- msos-continuous-improvement:continuous-improvement-phase1 -->"
        )
    )
    client = FakeGitHub(issues=[_issue33(), {**existing, "number": 77}])

    result = propose_one_improvement(client, PlannerConfig())

    assert result.status in {"unchanged", "updated"}
    assert result.issue_number == 77
    assert not [call for call in client.text_calls if call[:2] == ["issue", "create"]]


def test_suppresses_duplicate_open_pr() -> None:
    client = FakeGitHub(
        issues=[_issue33()],
        prs=[
            {
                "number": 12,
                "title": "Implement Phase 1 continuous-improvement proposal automation",
                "state": "OPEN",
                "body": "<!-- msos-continuous-improvement:continuous-improvement-phase1 -->",
                "url": "https://github.com/DanielTabakman/msos-autobuilder/pull/12",
            }
        ],
    )

    result = propose_one_improvement(client, PlannerConfig())

    assert result.status == "suppressed"
    assert result.issue_number is None
    assert "duplicates or noise" in result.digest


def test_generated_planner_issues_are_duplicates_not_evidence() -> None:
    generated = _issue33(
        body=(
            "Generated issue\n"
            "<!-- msos-continuous-improvement:continuous-improvement-phase1 -->"
        )
    )
    generated["number"] = 61
    client = FakeGitHub(issues=[_issue33(), generated])

    evidence, _issues, _prs = collect_github_evidence(client, PlannerConfig())

    assert [item.evidence_id for item in evidence] == ["issue-33"]


def test_dry_run_reports_would_create_without_mutating() -> None:
    client = FakeGitHub(issues=[_issue33()])

    result = propose_one_improvement(client, PlannerConfig(dry_run=True))

    assert result.status == "would_create"
    assert result.dry_run is True
    assert not [call for call in client.text_calls if call[:2] == ["issue", "create"]]
