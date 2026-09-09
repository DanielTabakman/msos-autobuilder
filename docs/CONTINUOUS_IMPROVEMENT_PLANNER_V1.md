# Continuous Improvement Planner V1

## Purpose

The continuous improvement planner removes recurring Autobuilder-improvement
orchestration from Daniel without expanding product, merge, or deployment authority
silently.

GitHub remains the source of truth. The planner may observe durable operating evidence,
rank bounded opportunities, and create or update one GitHub issue at a time. Later phases
may implement and deploy low-risk internal improvements only after the required witnesses
exist.

## Operating Principle

Optimize first for founder time removed, not maximum theoretical autonomy.

The planner should reduce repeated founder work such as noticing recurring failures,
chartering the same repair class, dispatching implementation threads, reviewing runtime
evidence chains, and coordinating release/deployment gates. It should not turn every
minor observation into noise.

## Architecture

### Phase 1 - Proposal Automation

```text
Durable evidence sources
  -> evidence normalizer
  -> friction and opportunity detector
  -> duplicate/noise suppressor
  -> ranked opportunity
  -> one GitHub issue create/update
  -> concise founder digest when meaningful state changes
```

Evidence sources include GitHub issues, issue comments, pull requests, CI/check state,
`jobs` branch manifests, `results` branch reports, gate reports, publication reports,
self-update reports, service witnesses, refill status, and founder commands that are
already recorded in durable state.

Phase 1 authority is proposal-only. It must not create jobs, run workers, publish release
manifests, merge pull requests, deploy code, or mutate installed runtime state.

### Phase 2 - Automatic Tested Draft PRs

```text
Bounded internal improvement issue
  -> Autobuilder repository execution adapter
  -> immutable job
  -> host execution
  -> result relay
  -> candidate gate
  -> bounded revision loop when configured
  -> controlled publisher opens a draft PR
```

Phase 2 requires an explicit execution-ready adapter for `DanielTabakman/msos-autobuilder`.
Building Autobuilder code is not the same as deploying it. Phase 2 may produce tested draft
PRs only.

### Phase 3 - Bounded Autonomous Deployment

```text
Accepted low-risk internal draft PR
  -> release request
  -> exact approved manifest
  -> external supervisor staging
  -> health witness
  -> rollback-capable activation
  -> concise founder digest
```

Phase 3 is blocked until:

- issue #32 has an accepted real rollback witness;
- issue #58 release compatibility is resolved and accepted;
- the selected change is inside a narrow low-risk whitelist.

## State Machines

### Phase 1

```text
OBSERVING
  -> EVIDENCE_READY
  -> OPPORTUNITY_CANDIDATE
  -> DUPLICATE_SUPPRESSED | NOISE_SUPPRESSED | RANKED
  -> ISSUE_CREATED | ISSUE_UPDATED
  -> AWAITING_IMPLEMENTATION_AUTHORITY
```

### Phase 2

```text
ISSUE_READY
  -> ADAPTER_VALIDATED
  -> JOB_QUEUED
  -> WORKER_RUNNING
  -> RESULT_RELAYED
  -> CANDIDATE_FAILED -> REVISION_QUEUED | ESCALATED
  -> CANDIDATE_PASSED
  -> DRAFT_PR_CREATED
  -> AWAITING_REVIEW
```

### Phase 3

```text
DRAFT_PR_ACCEPTED
  -> LOW_RISK_CLASSIFIED
  -> RELEASE_REQUEST_CREATED
  -> RELEASE_APPROVED
  -> STAGED
  -> CUTOVER_HEALTHY
  -> DEPLOYED
  -> ROLLED_BACK | ESCALATED
```

## Authority Classes

### A0 - Read and Observe

May read durable GitHub and local evidence. May not write issues, PRs, jobs, branches,
release manifests, runtime state, or installed services.

### A1 - Propose

May create or update one bounded GitHub improvement issue and produce one concise digest
when evidence changes materially. No code execution, job dispatch, release activation,
merge, deployment, or runtime mutation.

### A2 - Implement Draft

May queue bounded Autobuilder-repository implementation jobs after an execution-ready
adapter exists. May use host, relay, candidate gate, bounded revisions, and controlled
draft publisher. No merge, deployment, release activation, product-main write, or stable
bootstrap mutation.

### A3 - Reviewed Release Request

May create a normal release-control pull request using `commit: self` after review. The
release request is only an eligibility request. The external supervisor still owns staging,
health verification, and rollback.

### A4 - Narrow Autonomous Deployment

May deploy only an accepted low-risk internal class after the #32 and #58 gates are closed
with evidence. Initial whitelist:

- tests;
- diagnostics and observability;
- deterministic repairs;
- dead-code and duplication removal;
- installer hardening;
- internal workflow simplification.

## Escalation Rules

Escalate to Daniel for product meaning, customer-facing behavior, trading or financial
semantics, credentials, spending, legal/compliance, destructive data operations, external
commitments, strategic priority, new repository authority, merge authority, deployment
class expansion, or stable bootstrap/task changes outside the owning issue.

Never silently expand merge, deployment, product, credential, destructive, or external
authority.

## Evidence Schema

```yaml
version: 1
evidence_id: string
observed_at_utc: datetime
source:
  kind: issue|pr|results_branch|jobs_branch|update_report|gate_report|publication_report|service_witness|ci|founder_command
  repository: owner/name
  url_or_ref: string
  commit: sha|null
classification:
  friction_type: failure|manual_rescue|duplicate_work|slow_stage|flaky_check|blocked_queue|rollback_gap|complexity|stale_state
  affected_component: host|relay|gate|revision|publisher|self_update|refill|build_next|docs|tests
  recurrence_key: string
  severity: low|medium|high|critical
facts:
  summary: string
  timestamps: []
  job_ids: []
  issue_ids: []
  pr_ids: []
  report_hashes: []
  changed_paths: []
founder_time:
  manual_steps_observed: integer
  estimated_minutes_per_occurrence: number
  occurrences_30d: integer
risk:
  blast_radius: local|repo|runtime|product|external
  rollback_available: boolean
  escalation_required: boolean
```

## Opportunity Schema

```yaml
version: 1
opportunity_id: string
title: string
problem_statement: string
evidence_ids: []
dedupe_key: string
authority_class_required: A1|A2|A3|A4
expected_benefit:
  founder_minutes_saved_30d: number
  reliability_gain: 0-5
  throughput_gain: 0-5
  complexity_removed: 0-5
cost:
  implementation_complexity: 0-5
  review_complexity: 0-5
  runtime_risk: 0-5
score:
  founder_time_removed: number
  weighted_total: number
handoff:
  goal: string
  allowed_paths: []
  forbidden_paths: []
  acceptance: []
  validation: []
  non_goals: []
  rollback: string
status: proposed|suppressed_duplicate|suppressed_noise|issue_created|issue_updated|blocked
```

Ranking formula:

```text
score =
  5 * founder_time_removed
+ 3 * reliability_gain
+ 2 * throughput_gain
+ 2 * complexity_removed
- 3 * runtime_risk
- 2 * implementation_complexity
- 1 * review_complexity
```

## Duplicate And Noise Suppression

Suppress as duplicate when the same `dedupe_key` maps to an open issue, open PR, recent
digest item, or active ownership chapter. Update the existing issue only when new evidence
materially changes its implementation handoff.

Suppress as noise when evidence is single-occurrence, low severity, already terminal,
lacks durable proof, is only a transient wait state, or requires founder judgment with no
new facts.

Never suppress critical evidence: rollback failure, product-main write risk, credential
exposure, destructive operation, competing writer, mutated immutable evidence, failed
health after cutover, or contradictory canon.

## One-Writer Boundaries

- PPE owns product registry, priority, readiness, founder command semantics, and product
  truth.
- `build-next` consumes PPE evidence; it does not invent product priority.
- Issue #58 owns stable bootstrap, task-control, stable probe, and installed compatibility
  while open.
- Issue #50 owns refill policy and reconciliation after #58 clears.
- Issue #33 Phase 1 owns proposal evidence and GitHub issue handoffs.
- Issue #33 Phase 2 owns Autobuilder internal implementation jobs only after adapter
  acceptance.
- The self-update supervisor owns installed release activation and rollback.
- The controlled publisher owns draft PR creation only.
- Daniel owns merges until merge authority is explicitly redesigned.

## Failure And Rollback Behavior

Phase 1 fails closed by writing no issue when evidence is incomplete or noisy. It may
return a local digest explaining why no proposal changed.

Phase 2 failures are handled by existing relay, gate, and bounded revision behavior.
Repeated failures become proposal evidence rather than blind retries.

Phase 3 may not run unless rollback is available and witnessed. Staging failure leaves the
active release untouched. Post-cutover health failure triggers supervisor rollback.
Rollback failure escalates immediately and disables further autonomous deployment for that
class.

## Minimum Rollout Sequence

1. Complete issue #58 compatibility repair and legacy five-service restart/rollback witness.
2. Re-review PR #60; resume issue #50 release-control activation and A to B capacity-one
   witness.
3. Implement Phase 1 proposal automation.
4. Add the msos-autobuilder execution-ready adapter for Phase 2.
5. Prove one internal improvement draft PR through host, relay, gate, revision if needed,
   and controlled draft publisher.
6. After #32 rollback acceptance and #58 compatibility closure, enable Phase 3 for one
   low-risk whitelist class.
7. Expand only after real success, failure, and rollback witnesses.

## Phase Acceptance

### Phase 1 - Proposal Automation

- durable operating evidence is collected from GitHub and result/update surfaces;
- repeated friction, failures, rescues, bottlenecks, and unnecessary complexity are
  normalized into evidence records;
- opportunities are ranked primarily by founder time removed;
- duplicate and noisy observations are suppressed;
- exactly one bounded GitHub improvement issue is created or materially updated per run;
- founder digest is concise and emitted only when meaningful state changes.

### Phase 2 - Automatic Tested Draft PRs

- an Autobuilder-repository execution adapter is accepted;
- internal improvement jobs use the immutable job, host, relay, gate, revision, and
  controlled draft-publisher pipeline;
- passing work produces tested draft PRs;
- no automatic merge, release activation, deployment, or product authority is introduced.

### Phase 3 - Bounded Autonomous Deployment

- issue #32 has an accepted rollback witness;
- issue #58 compatibility is resolved and accepted;
- only narrow low-risk classes are enabled initially;
- escalation remains mandatory for product, strategy, finance, credentials, destructive,
  external, merge, or deployment-class changes;
- founder receives one concise digest with evidence, risk, deployment state, and rollback
  state.
