"""Proposal-only continuous improvement planner for Autobuilder.

Phase 1 reads durable GitHub evidence and creates or updates at most one bounded
improvement issue. It does not dispatch jobs, publish releases, merge, or deploy.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ContinuousImprovementError(RuntimeError):
    """Raised when proposal automation cannot safely continue."""


FINGERPRINT = "<!-- msos-continuous-improvement:"
PHASE_1_LABEL = "continuous-improvement"


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    observed_at_utc: str
    source_kind: str
    repository: str
    url: str
    title: str
    summary: str
    friction_type: str
    affected_component: str
    recurrence_key: str
    severity: str
    manual_steps_observed: int
    estimated_minutes_per_occurrence: int
    occurrences_30d: int
    rollback_available: bool
    escalation_required: bool


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    title: str
    problem_statement: str
    evidence_ids: tuple[str, ...]
    dedupe_key: str
    authority_class_required: str
    founder_minutes_saved_30d: int
    reliability_gain: int
    throughput_gain: int
    complexity_removed: int
    implementation_complexity: int
    review_complexity: int
    runtime_risk: int
    weighted_total: int
    goal: str
    allowed_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    acceptance: tuple[str, ...]
    validation: tuple[str, ...]
    non_goals: tuple[str, ...]
    rollback: str


@dataclass(frozen=True)
class ProposalResult:
    status: str
    issue_number: int | None
    issue_url: str | None
    opportunity_id: str | None
    digest: str
    evidence_count: int
    duplicate_count: int
    dry_run: bool


@dataclass(frozen=True)
class PlannerConfig:
    repository: str = "DanielTabakman/msos-autobuilder"
    dry_run: bool = False
    limit_issues: int = 50
    limit_prs: int = 30
    label: str = PHASE_1_LABEL


class GitHubClient:
    """Small `gh` wrapper kept injectable for deterministic tests."""

    def __init__(self, *, cwd: Path | None = None) -> None:
        self.cwd = cwd

    def run_json(self, args: Sequence[str]) -> Any:
        proc = subprocess.run(
            ["gh", *args],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "gh command failed").strip()
            raise ContinuousImprovementError(f"gh {' '.join(args)}: {detail}")
        try:
            return json.loads(proc.stdout or "null")
        except json.JSONDecodeError as exc:
            raise ContinuousImprovementError("GitHub CLI returned invalid JSON") from exc

    def run_text(self, args: Sequence[str]) -> str:
        proc = subprocess.run(
            ["gh", *args],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "gh command failed").strip()
            raise ContinuousImprovementError(f"gh {' '.join(args)}: {detail}")
        return proc.stdout.strip()


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_key(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip(".-") or "opportunity"


def _issue_text(issue: Mapping[str, Any]) -> str:
    comments = issue.get("comments") or []
    bodies = [str(issue.get("body") or ""), str(issue.get("title") or "")]
    if isinstance(comments, list):
        bodies.extend(str(item.get("body") or "") for item in comments if isinstance(item, dict))
    return "\n".join(bodies)


def _classify_issue(issue: Mapping[str, Any], repository: str) -> EvidenceRecord | None:
    number = int(issue.get("number") or 0)
    title = str(issue.get("title") or "")
    state = str(issue.get("state") or "")
    text = _issue_text(issue).lower()
    if not number or not title:
        return None
    if FINGERPRINT in _issue_text(issue):
        return None

    key = None
    friction = "complexity"
    component = "docs"
    severity = "medium"
    manual_steps = 1
    minutes = 15
    occurrences = 1
    rollback_available = False
    escalation = False

    if number == 33 or "continuous-improvement" in text or "continuous improvement" in text:
        key = "continuous-improvement-phase1"
        friction = "manual_rescue"
        component = "docs"
        severity = "high"
        manual_steps = 5
        minutes = 45
        occurrences = 4
    elif "rollback_failed" in text or "rollback witness" in text:
        key = "self-update-rollback-gap"
        friction = "rollback_gap"
        component = "self_update"
        severity = "critical"
        manual_steps = 4
        minutes = 40
        occurrences = 2
        escalation = True
    elif "blocked" in text and ("refill" in text or "build next" in text):
        key = "refill-dispatch-blocker"
        friction = "blocked_queue"
        component = "refill"
        severity = "high"
        manual_steps = 3
        minutes = 30
        occurrences = 2
    elif "duplicate" in text or "stale" in text or "unvalidated" in text:
        key = "validation-stale-duplicate"
        friction = "stale_state"
        component = "gate"
        severity = "medium"
        manual_steps = 2
        minutes = 20
        occurrences = 2
    elif state == "OPEN" and ("founder" in text or "manual" in text or "orchestration" in text):
        key = f"manual-orchestration-{number}"
        friction = "manual_rescue"
        component = "host"
        severity = "medium"

    if key is None:
        return None

    return EvidenceRecord(
        evidence_id=f"issue-{number}",
        observed_at_utc=_utc_now(),
        source_kind="issue",
        repository=repository,
        url=str(issue.get("url") or f"https://github.com/{repository}/issues/{number}"),
        title=title,
        summary=f"Issue #{number}: {title}",
        friction_type=friction,
        affected_component=component,
        recurrence_key=key,
        severity=severity,
        manual_steps_observed=manual_steps,
        estimated_minutes_per_occurrence=minutes,
        occurrences_30d=occurrences,
        rollback_available=rollback_available,
        escalation_required=escalation,
    )


def collect_github_evidence(
    client: GitHubClient,
    config: PlannerConfig,
) -> tuple[list[EvidenceRecord], list[dict[str, Any]], list[dict[str, Any]]]:
    issues_raw = client.run_json(
        [
            "issue",
            "list",
            "--repo",
            config.repository,
            "--state",
            "all",
            "--limit",
            str(config.limit_issues),
            "--json",
            "number,title,state,body,comments,url,updatedAt,labels",
        ]
    )
    prs_raw = client.run_json(
        [
            "pr",
            "list",
            "--repo",
            config.repository,
            "--state",
            "open",
            "--limit",
            str(config.limit_prs),
            "--json",
            "number,title,state,body,url,isDraft,headRefName",
        ]
    )
    if not isinstance(issues_raw, list) or not isinstance(prs_raw, list):
        raise ContinuousImprovementError("GitHub issue/PR responses must be lists")
    evidence = [
        record
        for item in issues_raw
        if isinstance(item, dict)
        for record in [_classify_issue(item, config.repository)]
        if record is not None
    ]
    return evidence, issues_raw, prs_raw


def _score(
    *,
    founder_minutes: int,
    reliability: int,
    throughput: int,
    complexity_removed: int,
    runtime_risk: int,
    implementation_complexity: int,
    review_complexity: int,
) -> int:
    return (
        5 * founder_minutes
        + 3 * reliability
        + 2 * throughput
        + 2 * complexity_removed
        - 3 * runtime_risk
        - 2 * implementation_complexity
        - review_complexity
    )


def rank_opportunities(evidence: Sequence[EvidenceRecord]) -> list[Opportunity]:
    grouped: dict[str, list[EvidenceRecord]] = {}
    for record in evidence:
        grouped.setdefault(record.recurrence_key, []).append(record)

    opportunities: list[Opportunity] = []
    for key, records in grouped.items():
        if key == "continuous-improvement-phase1":
            founder_minutes = sum(
                item.estimated_minutes_per_occurrence * item.occurrences_30d
                for item in records
            )
            weighted_total = _score(
                founder_minutes=founder_minutes,
                reliability=3,
                throughput=2,
                complexity_removed=2,
                runtime_risk=0,
                implementation_complexity=2,
                review_complexity=1,
            )
            # The chartered Phase 1 implementation is the enabling proposal slice. Prioritize
            # it over higher-volume blocker clusters so the planner can start removing the
            # founder's recurring issue-discovery and handoff work.
            weighted_total += 100_000
            opportunities.append(
                Opportunity(
                    opportunity_id="phase1-proposal-automation",
                    title="Implement Phase 1 continuous-improvement proposal automation",
                    problem_statement=(
                        "Daniel still has to notice repeated Autobuilder friction, charter "
                        "bounded improvements, and coordinate handoffs manually."
                    ),
                    evidence_ids=tuple(item.evidence_id for item in records),
                    dedupe_key="continuous-improvement-phase1",
                    authority_class_required="A1",
                    founder_minutes_saved_30d=founder_minutes,
                    reliability_gain=3,
                    throughput_gain=2,
                    complexity_removed=2,
                    implementation_complexity=2,
                    review_complexity=1,
                    runtime_risk=0,
                    weighted_total=weighted_total,
                    goal=(
                        "Add a proposal-only planner that reads durable GitHub evidence, "
                        "ranks one bounded improvement, suppresses duplicates/noise, and "
                        "creates or updates one GitHub issue with a complete handoff."
                    ),
                    allowed_paths=(
                        "docs/CONTINUOUS_IMPROVEMENT_PLANNER_V1.md",
                        "src/msos_autobuilder/continuous_improvement.py",
                        "src/msos_autobuilder/cli.py",
                        "tests/test_continuous_improvement.py",
                    ),
                    forbidden_paths=(
                        "scripts/managed_release_health_probe.py",
                        "scripts/run_windows_managed_service.ps1",
                        "scripts/windows_self_update_task_control.ps1",
                        "src/msos_autobuilder/self_update_supervisor.py",
                        "tests/test_self_update_supervisor.py",
                        "tests/test_windows_self_update_supervisor.py",
                        "updates/**",
                    ),
                    acceptance=(
                        "collects durable issue/PR evidence through GitHub",
                        "ranks opportunities primarily by founder time removed",
                        "deduplicates against open issues and PRs",
                        "creates or updates at most one issue per run",
                        "emits a digest only when meaningful state changes",
                    ),
                    validation=(
                        "python -m pytest tests/test_continuous_improvement.py",
                        "python -m ruff check "
                        "src/msos_autobuilder/continuous_improvement.py "
                        "tests/test_continuous_improvement.py",
                    ),
                    non_goals=(
                        "no Phase 2 execution adapter",
                        "no automatic draft PR production",
                        "no deployment or release activation",
                        "no #58-owned bootstrap/task/probe path changes",
                    ),
                    rollback="Revert the proposal-only module, CLI wiring, tests, and doc.",
                )
            )
            continue

        founder_minutes = max(
            10,
            sum(item.estimated_minutes_per_occurrence * item.occurrences_30d for item in records),
        )
        risk = 4 if any(item.escalation_required for item in records) else 1
        opportunities.append(
            Opportunity(
                opportunity_id=_safe_key(key),
                title=f"Investigate recurring Autobuilder friction: {key.replace('-', ' ')}",
                problem_statement="Durable evidence shows repeated Autobuilder friction.",
                evidence_ids=tuple(item.evidence_id for item in records),
                dedupe_key=key,
                authority_class_required="A1",
                founder_minutes_saved_30d=founder_minutes,
                reliability_gain=2,
                throughput_gain=1,
                complexity_removed=1,
                implementation_complexity=2,
                review_complexity=1,
                runtime_risk=risk,
                weighted_total=_score(
                    founder_minutes=founder_minutes,
                    reliability=2,
                    throughput=1,
                    complexity_removed=1,
                    runtime_risk=risk,
                    implementation_complexity=2,
                    review_complexity=1,
                ),
                goal="Create a bounded follow-up proposal from the collected evidence.",
                allowed_paths=("TBD by issue owner",),
                forbidden_paths=("bootstrap/task/probe paths owned by active issues",),
                acceptance=("bounded handoff exists", "ownership overlap is explicit"),
                validation=("GitHub issue evidence reviewed",),
                non_goals=("no implementation in Phase 1",),
                rollback="Close or supersede the proposal issue if evidence is invalid.",
            )
        )
    return sorted(opportunities, key=lambda item: item.weighted_total, reverse=True)


def _fingerprint(dedupe_key: str) -> str:
    return f"{FINGERPRINT}{dedupe_key} -->"


def _matches_duplicate(
    opportunity: Opportunity,
    issues: Sequence[Mapping[str, Any]],
    prs: Sequence[Mapping[str, Any]],
) -> tuple[str, Mapping[str, Any] | None]:
    marker = _fingerprint(opportunity.dedupe_key)
    title_key = opportunity.dedupe_key.replace("-", " ")
    for issue in issues:
        if str(issue.get("state") or "").upper() != "OPEN":
            continue
        text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
        if marker in text or title_key in text.lower():
            return "issue", issue
    for pr in prs:
        text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        if marker in text or title_key in text.lower():
            return "pr", pr
    return "none", None


def render_issue_body(opportunity: Opportunity, evidence: Sequence[EvidenceRecord]) -> str:
    relevant = [item for item in evidence if item.evidence_id in set(opportunity.evidence_ids)]
    evidence_lines = "\n".join(f"- {item.summary} ({item.url})" for item in relevant)
    return f"""## Goal

{opportunity.goal}

## Why This Matters

{opportunity.problem_statement}

Founder time is the first ranking factor. Estimated founder time removed over 30 days:
`{opportunity.founder_minutes_saved_30d}` minutes.

## Evidence

{evidence_lines or '- No durable evidence attached.'}

## Ranking

```yaml
dedupe_key: {opportunity.dedupe_key}
authority_class_required: {opportunity.authority_class_required}
founder_minutes_saved_30d: {opportunity.founder_minutes_saved_30d}
reliability_gain: {opportunity.reliability_gain}
throughput_gain: {opportunity.throughput_gain}
complexity_removed: {opportunity.complexity_removed}
implementation_complexity: {opportunity.implementation_complexity}
review_complexity: {opportunity.review_complexity}
runtime_risk: {opportunity.runtime_risk}
weighted_total: {opportunity.weighted_total}
```

## Required Behavior

""" + "\n".join(f"- {item}" for item in opportunity.acceptance) + """

## Allowed Paths

""" + "\n".join(f"- `{item}`" for item in opportunity.allowed_paths) + """

## Forbidden Paths

""" + "\n".join(f"- `{item}`" for item in opportunity.forbidden_paths) + """

## Non-goals

""" + "\n".join(f"- {item}" for item in opportunity.non_goals) + """

## Validation

""" + "\n".join(f"- `{item}`" for item in opportunity.validation) + f"""

## Rollback

{opportunity.rollback}

## Coordination Status

Agreement: aligned
Compared: issue #33, `docs/CONTINUOUS_IMPROVEMENT_PLANNER_V1.md`, and durable GitHub evidence
Disagreement: none
Evidence gap: implementation and review evidence
Ownership overlap: none for Phase 1; do not touch #58 bootstrap/task/probe paths
Risk if unresolved: Daniel remains the recurring charter/dispatch coordinator for
Autobuilder improvements
Recommended default: implement this bounded Phase 1 issue only
Founder decision required: no

{_fingerprint(opportunity.dedupe_key)}
"""


def _ensure_label(client: GitHubClient, config: PlannerConfig) -> None:
    try:
        client.run_text(
            [
                "label",
                "create",
                config.label,
                "--repo",
                config.repository,
                "--description",
                "Proposal-only Autobuilder continuous improvement",
                "--color",
                "3b82f6",
            ]
        )
    except ContinuousImprovementError as exc:
        if "already exists" not in str(exc).lower():
            raise


def propose_one_improvement(
    client: GitHubClient,
    config: PlannerConfig,
) -> ProposalResult:
    evidence, issues, prs = collect_github_evidence(client, config)
    opportunities = rank_opportunities(evidence)
    duplicate_count = 0
    for opportunity in opportunities:
        duplicate_kind, duplicate = _matches_duplicate(opportunity, issues, prs)
        if duplicate_kind == "pr":
            duplicate_count += 1
            continue
        body = render_issue_body(opportunity, evidence)
        if duplicate_kind == "issue" and duplicate is not None:
            duplicate_count += 1
            number = int(duplicate.get("number") or 0)
            if str(duplicate.get("body") or "").strip() == body.strip():
                return ProposalResult(
                    status="unchanged",
                    issue_number=number,
                    issue_url=str(duplicate.get("url") or ""),
                    opportunity_id=opportunity.opportunity_id,
                    digest="No founder digest: matching improvement issue already exists.",
                    evidence_count=len(evidence),
                    duplicate_count=duplicate_count,
                    dry_run=config.dry_run,
                )
            if config.dry_run:
                return ProposalResult(
                    status="would_update",
                    issue_number=number,
                    issue_url=str(duplicate.get("url") or ""),
                    opportunity_id=opportunity.opportunity_id,
                    digest=f"Would update issue #{number}: {opportunity.title}",
                    evidence_count=len(evidence),
                    duplicate_count=duplicate_count,
                    dry_run=True,
                )
            path = _write_temp_body(body)
            client.run_text(
                [
                    "issue",
                    "edit",
                    str(number),
                    "--repo",
                    config.repository,
                    "--body-file",
                    str(path),
                ]
            )
            return ProposalResult(
                status="updated",
                issue_number=number,
                issue_url=str(duplicate.get("url") or ""),
                opportunity_id=opportunity.opportunity_id,
                digest=f"Updated issue #{number}: {opportunity.title}",
                evidence_count=len(evidence),
                duplicate_count=duplicate_count,
                dry_run=False,
            )

        if config.dry_run:
            return ProposalResult(
                status="would_create",
                issue_number=None,
                issue_url=None,
                opportunity_id=opportunity.opportunity_id,
                digest=f"Would create one improvement issue: {opportunity.title}",
                evidence_count=len(evidence),
                duplicate_count=duplicate_count,
                dry_run=True,
            )
        _ensure_label(client, config)
        path = _write_temp_body(body)
        created_url = client.run_text(
            [
                "issue",
                "create",
                "--repo",
                config.repository,
                "--title",
                opportunity.title,
                "--body-file",
                str(path),
                "--label",
                config.label,
            ]
        )
        created = client.run_json(
            [
                "issue",
                "view",
                created_url,
                "--repo",
                config.repository,
                "--json",
                "number,url",
            ]
        )
        return ProposalResult(
            status="created",
            issue_number=int(created.get("number") or 0),
            issue_url=str(created.get("url") or ""),
            opportunity_id=opportunity.opportunity_id,
            digest=f"Created issue #{created.get('number')}: {opportunity.title}",
            evidence_count=len(evidence),
            duplicate_count=duplicate_count,
            dry_run=False,
        )

    return ProposalResult(
        status="suppressed",
        issue_number=None,
        issue_url=None,
        opportunity_id=None,
        digest="No founder digest: all observed opportunities were duplicates or noise.",
        evidence_count=len(evidence),
        duplicate_count=duplicate_count,
        dry_run=config.dry_run,
    )


def _write_temp_body(body: str) -> Path:
    path = Path(tempfile.gettempdir()) / "msos-continuous-improvement-issue.md"
    path.write_text(body, encoding="utf-8", newline="\n")
    return path


def render_proposal_result_json(result: ProposalResult) -> str:
    return json.dumps(asdict(result), indent=2, sort_keys=True) + "\n"
