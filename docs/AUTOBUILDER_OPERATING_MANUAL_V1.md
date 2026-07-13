# MSOS Autobuilder Operating Manual V1

## Purpose

The Autobuilder is the internal engineering factory for MSOS/PPE. Its job is to turn a bounded, approved engineering objective into tested evidence and a reviewable draft product pull request while minimizing founder orchestration.

The system does not own product truth. Daniel owns product meaning, priorities, customer interpretation, trading semantics, risk appetite, and materially different strategic outcomes.

## Current operating mode

The installed Windows machine runs five persistent services:

1. **Host** — imports immutable approved jobs and invokes local Codex in disposable product clones.
2. **Result relay** — reconstructs complete patches and records immutable evidence on the `results` branch.
3. **Candidate gate** — applies the candidate to a fresh pinned clone and runs deterministic checks without a product write.
4. **Revision loop** — converts configured failed gates into bounded correction jobs.
5. **Controlled publisher** — revalidates a passing candidate on current product `main` and creates one draft product PR.

The publisher cannot merge, write product `main`, mark a PR ready, add an automerge marker, or force-push.

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
- supervise routine patch creation or candidate testing.

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
