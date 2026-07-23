# MSOS Autobuilder Operating Manual V1

## Purpose

The Autobuilder is the internal engineering factory for MSOS/PPE. Its job is to turn a bounded, approved engineering objective into tested evidence and a completed product change while minimizing founder orchestration.

The system does not own product truth. Daniel owns product meaning, priorities, customer interpretation, trading semantics, risk appetite, materially different strategic outcomes, and authority-class expansion.

For work explicitly approved as `AUTO_MERGE_WHEN_GREEN`, Daniel's decision occurs when the objective and acceptance contract are approved. The system should not ask him to repeat that decision after implementation succeeds.

## Current operating mode

The installed Windows machine runs six persistent services:

1. **Host** — imports immutable approved jobs and invokes local Codex in disposable product clones.
2. **Result relay** — reconstructs complete patches and records immutable evidence on the `results` branch.
3. **Candidate gate** — applies the candidate to a fresh pinned clone and runs deterministic checks without a product write.
4. **Revision loop** — converts configured failed gates into bounded correction jobs.
5. **Controlled publisher** — revalidates a passing candidate on current product `main` and creates one draft product PR.
6. **Capacity-one refill controller** — preserves founder intent to keep one approved build occupied by periodically reconciling runtime capacity and dispatching only through `build-next`.

The current publisher cannot merge, write product `main`, mark a PR ready, add an automerge marker, or force-push. This remains true until the bounded guarded-merger capability in `docs/BOUNDED_AUTO_MERGE_AND_CLEANUP_V1.md` is implemented and witnessed.

The refill controller cannot create product scope, bypass `build-next`, increase capacity above one, publish, merge, or write product `main`.

## Normal idea-to-completion flow

```text
Daniel states an idea or desired outcome
        ↓
ChatGPT resolves product intent and defines the smallest coherent engineering objective
        ↓
The work item records one merge class:
AUTO_MERGE_WHEN_GREEN | FOUNDER_DECISION_REQUIRED | NEVER_MERGE
        ↓
A bounded approved job is created with exact source identity, allowed paths, forbidden paths, and tests
        ↓
Codex works in a disposable clone
        ↓
Complete patch and report are relayed
        ↓
Candidate gate tests the integrated result
        ↓
Failure → bounded revision job or evidence-backed escalation
Pass    → controlled publication validation
        ↓
AUTO_MERGE_WHEN_GREEN → guarded merge, verification, cleanup, digest
FOUNDER_DECISION_REQUIRED → reviewable draft PR and one genuine decision request
NEVER_MERGE → preserve evidence only
```

## What Daniel should normally do

Daniel should provide:

- the problem, product direction, or desired outcome;
- corrections when the system has misunderstood product truth;
- approval of the bounded objective, acceptance criteria, authority class, and materially irreversible actions;
- decisions only when execution reveals a genuinely new product, strategic, financial, legal, credential, destructive, external, or authority-expansion question.

Daniel should not normally have to:

- write implementation prompts for Codex;
- choose files, branches, test commands, or architecture details;
- copy reports between ChatGPT, Codex, and GitHub;
- manually retry failed implementation jobs;
- manually call `build-next` after each completed worker when capacity-one refill is enabled;
- supervise routine patch creation or candidate testing;
- decide again whether to merge work whose objective and merge authority were already approved;
- delete merged branches, disposable clones, temporary workspaces, or completed queue state.

## Capacity-one Refill Policy

Capacity-one refill is an explicit founder mode, not an implicit default. `refill-keep-one` records a durable policy under local host state, `refill-pause` preserves any current workers while stopping new automatic dispatch, and `refill-resume` fails closed unless a prior founder target exists.

The managed `refill-run` service holds a singleton process lock, writes `refill-status.json` with a heartbeat and last reconciliation identity, and uses a separate reconcile lock so CLI and service reconciliation cannot double-dispatch. Every reconciliation reports running jobs, local pending jobs, submitted feed jobs awaiting host import, active gate/review pressure by target repository, publisher disposition, and a single decision timestamp.

Queue and review pressure return `BACKPRESSURE`, not `BLOCKED`. Paused mode returns `PAUSED` and is recorded as automatic-mode evidence. Runtime health failures, stale host heartbeat, malformed policy, missing feed configuration, stale registry/source evidence, or missing dispatch authority fail closed before a new job is submitted. Item-level source/path/authority validation remains owned by `build-next`; refill owns runtime health and capacity evidence.

Mandatory coordination status for an enabled installation:

- `refill-status.json` is fresh and bound to the running exact release;
- `refill-policy.json` shows `enabled: true`, `desired_capacity: 1`, and capacity no greater than one;
- `refill-reconcile.lock` and `refill-service.lock` are local process coordination only;
- `last_decision_evidence.status` is one of `QUEUED`, `RUNNING`, `UNFILLED`, `BACKPRESSURE`, `BLOCKED`, or `PAUSED`;
- submitted-but-not-imported feed jobs count as occupied capacity until imported or terminal;
- passed candidates count as review pressure only while they are genuinely awaiting review for their target repository.

Issue #50 owns refill and its zero-founder-return A → B witness. It must not silently add merge authority.

## Merge authority policy

Every work item must record one authority class before implementation begins.

### `AUTO_MERGE_WHEN_GREEN`

The approved objective may merge automatically after all predetermined candidate, publication, GitHub, authority, ownership, and exact-head gates pass.

The merger must verify the exact merge identity and perform bounded cleanup before declaring the work complete.

### `FOUNDER_DECISION_REQUIRED`

The system may produce a validated draft PR, but Daniel must be asked only for the new product or authority decision that remains unresolved—not for routine implementation review.

### `NEVER_MERGE`

The branch is evidence only and must never be merged. Deliberate negative witnesses, rollback-test commits, reproductions, and comparison experiments belong here.

Canonical gates, cleanup, sequencing, and evidence are defined in:

`docs/BOUNDED_AUTO_MERGE_AND_CLEANUP_V1.md`

Until that capability is implemented and a real product witness is accepted, all controlled publisher output remains draft-only.

## Automatic merge and cleanup boundary

An eligible guarded merger may operate only when:

- the merge class was recorded before implementation;
- the objective and acceptance criteria are frozen;
- candidate and publication validation passed;
- required checks passed on the exact PR head;
- changed paths remain authorized;
- no canon, evidence, ownership, or authority conflict remains;
- no new founder decision was discovered;
- the exact validated head still matches immediately before merge.

After merge it should:

- prove the merge commit and default-branch identity;
- delete the merged branch when policy permits;
- remove disposable clones and temporary workspaces;
- archive job, gate, revision, publication, merge, and cleanup evidence;
- terminalize the work item and originating issue according to their explicit contracts;
- emit one concise founder digest;
- allow refill to select the next approved item.

Ambiguous evidence blocks. The system may not weaken checks, broaden authority, or silently reinterpret the approved objective to obtain a merge.

## Pull-request policy

A validated product PR means:

- the implementation completed;
- the complete candidate patch was preserved;
- the disposable candidate gate passed;
- the candidate was re-applied to current product `main`;
- publication-time checks passed;
- no direct product-main write occurred outside an authorized guarded merge.

For `AUTO_MERGE_WHEN_GREEN`, the PR is an intermediate evidence surface and should merge automatically when every gate passes.

For `FOUNDER_DECISION_REQUIRED`, the PR remains unmerged until the named product or authority decision is resolved.

For `NEVER_MERGE`, the PR or branch remains evidence only.

## Failure behavior

The system should fail closed.

- A failed Codex job does not publish.
- An incomplete or hash-mismatched patch does not gate.
- A failed gate may produce only a bounded configured revision.
- Product path overlap or source drift blocks publication and merge.
- A failed publication attempt must not write `main` or force-push.
- A stale or changed PR head must not merge.
- Missing, pending, cancelled, or failed required checks must not merge.
- A merge conflict or new founder-decision condition must escalate with evidence.
- Cleanup failure after a verified merge must preserve the valid merge and become a bounded maintenance item.
- Host upgrades must stage, health-check, and roll back through a separate supervisor.

Repeated failures are evidence about the design. Do not accumulate flags, shims, duplicate writers, or one-off bypasses. Re-derive the smallest coherent design that removes the failure class.

## Authority boundaries

### Suitable for bounded automatic execution and eventual merge

- implementation of already-approved behavior;
- tests and deterministic validation;
- diagnostics and observability;
- dead-code and duplication removal;
- installer and reliability hardening;
- internal workflow simplification;
- bounded performance work that preserves established semantics;
- correction of failures with explicit evidence and rollback;
- accepted documentation and UI implementation that does not reopen product meaning.

### Requires Daniel's judgment

- product meaning or customer-facing behavior not settled by the approved objective;
- trading or financial semantics;
- new external commitments;
- credentials, spending, legal, or compliance choices;
- destructive data changes;
- materially different strategic outcomes;
- expansion of automatic merge or deployment authority.

## Product merge versus Autobuilder deployment

Automatic product merge does not grant autonomous external deployment.

Automatic merge of bounded Autobuilder source changes does not grant autonomous installation.

Autonomous Autobuilder release activation remains blocked until issue #32 has an accepted real rollback witness and replay-block evidence. The external supervisor remains the only authority for exact-release installation and rollback.

## Current remaining infrastructure chapters

The factory is operational, but these chapters remain:

1. **Capacity-one installed witness** — issue #50. Prove one founder command carries approved item A through terminal disposition and automatically dispatches item B.
2. **Bounded guarded merge and cleanup** — implement `docs/BOUNDED_AUTO_MERGE_AND_CLEANUP_V1.md` after issue #50 acceptance and prove one real PPE/MSOS automatic merge.
3. **Fail-safe self-update supervisor acceptance** — issue #32. Complete the deliberate rollback and replay-block witness before autonomous Autobuilder installation.
4. **Bounded continuous-improvement planner** — issue #33. Identify and execute low-risk internal improvements from operating evidence without silently expanding product or deployment authority.
5. **Cross-repository adapters** — independently register and witness each additional product repository before automatic production expands beyond PPE/MSOS.

## Operator evidence

Canonical evidence lives in GitHub:

- approved jobs: `jobs` branch;
- relayed reports, patches, gate reports, publication reports, merge reports, and cleanup reports: `results` branch;
- implementation and infrastructure issues: repository issues;
- product pull requests and merge identities: the target product repository.

Mutable leases, locks, heartbeats, and process state remain local under `%USERPROFILE%\.msos-autobuilder` and must not be treated as GitHub coordination state.

## Working rule

Close one coherent chapter with evidence before expanding authority. New autonomy is earned by a real witness, a failure witness, and where installation is involved, a rollback witness—not by configuration alone.
