# MSOS Autobuilder Operating Manual V1

## Purpose

The Autobuilder is the internal engineering factory for MSOS/PPE. Its job is to turn a bounded, approved engineering objective into tested evidence and a reviewable draft product pull request while minimizing founder orchestration.

The system does not own product truth. Daniel owns product meaning, priorities, customer interpretation, trading semantics, risk appetite, and materially different strategic outcomes.

## Current operating mode

The installed Windows machine runs six persistent services:

1. **Host** — imports immutable approved jobs and invokes local Codex in disposable product clones.
2. **Result relay** — reconstructs complete patches and records immutable evidence on the `results` branch.
3. **Candidate gate** — applies the candidate to a fresh pinned clone and runs deterministic checks without a product write.
4. **Revision loop** — converts configured failed gates into bounded correction jobs.
5. **Controlled publisher** — revalidates a passing candidate on current product `main` and creates one draft product PR.
6. **Capacity-one refill controller** — preserves founder intent to keep one approved build occupied by periodically reconciling runtime capacity and dispatching only through `build-next`.

The publisher cannot merge, write product `main`, mark a PR ready, add an automerge marker, or force-push.
The refill controller cannot create product scope, bypass `build-next`, increase capacity above one, publish, merge, or write product `main`.

## Normal idea-to-PR flow

```text
Daniel states an idea or desired outcome
        ↓
ChatGPT resolves product intent and defines the smallest coherent engineering objective
        ↓
A bounded approved job is created with exact source identity, allowed paths, forbidden paths, and tests
        ↓
Codex works in a disposable clone
        ↓
Complete patch and report are relayed
        ↓
Candidate gate tests the integrated result
        ↓
Failure → bounded revision job
Pass    → controlled draft product PR
        ↓
Daniel is notified of the result and any product decision still required
```

## What Daniel should normally do

Daniel should provide:

- the problem, product direction, or idea;
- corrections when the system has misunderstood product truth;
- approval for customer-facing, strategic, financial, legal, credential, destructive, or materially irreversible decisions;
- the final merge decision until merge authority is deliberately redesigned.

Daniel should not normally have to:

- write implementation prompts for Codex;
- choose files, branches, test commands, or architecture details;
- copy reports between ChatGPT, Codex, and GitHub;
- manually retry failed implementation jobs;
- manually call `build-next` after each completed worker when capacity-one refill is enabled;
- supervise routine patch creation or candidate testing.

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

## Draft PR policy

A draft product PR means:

- the implementation completed;
- the complete candidate patch was preserved;
- the disposable candidate gate passed;
- the candidate was re-applied to current product `main`;
- publication-time checks passed;
- product `main` was not changed.

A draft PR is evidence-backed and reviewable, but it is not automatically a product decision. It remains unmerged until the relevant product and repository checks are accepted.

## Failure behavior

The system should fail closed.

- A failed Codex job does not publish.
- An incomplete or hash-mismatched patch does not gate.
- A failed gate may produce only a bounded configured revision.
- Product path overlap or source drift blocks publication.
- A failed publication attempt must not write `main` or force-push.
- Host upgrades must eventually stage, health-check, and roll back through a separate supervisor.

Repeated failures are evidence about the design. Do not accumulate flags, shims, duplicate writers, or one-off bypasses. Re-derive the smallest coherent design that removes the failure class.

## Authority boundaries

### Suitable for bounded automatic execution

- tests and deterministic validation;
- diagnostics and observability;
- dead-code and duplication removal;
- installer and reliability hardening;
- internal workflow simplification;
- bounded performance work that preserves established semantics;
- correction of failures with explicit evidence and rollback.

### Requires Daniel's product judgment

- product meaning or customer-facing behavior;
- trading or financial semantics;
- new external commitments;
- credentials, spending, legal, or compliance choices;
- destructive data changes;
- materially different strategic outcomes.

## Current manual boundaries

The factory is operational, but two infrastructure chapters remain:

1. **Fail-safe self-update supervisor** — issue #32. This removes founder-run PowerShell for future Autobuilder releases.
2. **Bounded continuous-improvement planner** — issue #33. This lets the system identify and execute low-risk internal improvements from operating evidence.

The continuous-improvement planner must not autonomously deploy host changes until the self-update supervisor has a proven rollback witness.

## Operator evidence

Canonical evidence lives in GitHub:

- approved jobs: `jobs` branch;
- relayed reports, patches, gate reports, and publication reports: `results` branch;
- implementation and infrastructure issues: repository issues;
- reviewable product changes: draft PRs in `DanielTabakman/Probability-prediction-engine`.

Mutable leases, locks, heartbeats, and process state remain local under `%USERPROFILE%\.msos-autobuilder` and must not be treated as GitHub coordination state.

## Working rule

Close one coherent chapter with evidence before expanding authority. New autonomy is earned by a real witness, a failure witness, and a rollback witness—not by configuration alone.
