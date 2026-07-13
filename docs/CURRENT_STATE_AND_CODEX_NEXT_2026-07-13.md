# Current State and Codex Next — 2026-07-13

## Executive state

The Autobuilder factory is operational end to end for configured product work.

```text
Approved job import              working
Local Codex execution            working
Complete patch relay             working
Disposable candidate gate        working
Bounded automatic revisions      working
Controlled draft PR publication  working
Automatic product merge          intentionally disabled
Fail-safe host self-update        not yet built
Self-directed improvement         not yet built
```

The first controlled publication witness is product draft PR `Probability-prediction-engine#5351` from job `ppe-frozen-evaluation-contract-v1-revision-1`.

That witness:

- passed 13 focused PPE tests;
- rejected snapshot-ID mismatch correctly;
- preserved `ppe_frozen_eval_v1` compatibility;
- created one bounded product branch, one commit, and one draft PR;
- did not write product `main`;
- did not enable merge authority.

## Strategic direction now accepted

The Autobuilder is a core internal product and technical-founder system, not temporary scaffolding.

The progression is:

1. **Founder-directed factory** — Daniel supplies ideas and product direction; ChatGPT turns them into bounded work; Codex executes; the factory tests, revises, and publishes draft PRs.
2. **Self-updating factory** — a separate fail-safe supervisor deploys exact approved Autobuilder releases with staging, health checks, and automatic rollback.
3. **Bounded continuous improvement** — the system reads its own operating evidence, selects low-risk internal improvements, executes them through the same pipeline, deploys through the supervisor, and reports the outcome to Daniel.

Product truth remains with Daniel throughout.

## What Codex should do next

The next Codex engineering chapter is **issue #32: Add fail-safe self-update supervisor**.

Do not expand autonomous improvement authority before this chapter closes with a real rollback witness.

### Objective

Create a small updater that is operationally separate from the Autobuilder version it manages. It must consume an explicit exact-commit update manifest, stage the release, validate it, cut over the Windows tasks, verify health, and restore the previous release automatically when validation fails.

### Required design

- The stable bootstrap/supervisor lives outside the versioned managed environment.
- Updates are pinned to an exact commit and verified; never blindly pull live `main`.
- Each release installs into a new versioned directory/environment.
- Staging runs package installation, Ruff, pytest, PowerShell parser checks, and Windows-specific smoke checks.
- Existing tasks stop only after staging passes.
- Cutover changes one active-version pointer or equivalent atomic routing boundary.
- The supervisor restarts Host, Result Relay, Candidate Gate, Revision Loop, and Controlled Publisher.
- Health verification checks expected task state plus an explicit machine-readable witness from the new release.
- Failed health verification automatically restores the previous active version and restarts it.
- Every attempt writes an immutable update report containing requested commit, previous version, staged version, checks, cutover result, health result, and rollback result.
- The worker cannot replace the currently executing supervisor in the same transaction.
- Credentials never appear in job prompts, reports, or committed fixtures.

### Acceptance sequence

1. manifest/schema and exact-commit verification tests;
2. versioned staging and parser/unit smoke tests;
3. no-cutover-on-staging-failure witness;
4. successful cutover and health witness;
5. deliberately broken release and automatic rollback witness;
6. repeat-safe exact-commit ledger;
7. first real zero-founder-touch update of the Autobuilder installation.

### Scope discipline

This chapter is infrastructure-only. Do not add product features, broaden merge authority, add an autonomous planner, or redesign the product job contract unless a concrete supervisor requirement proves the current boundary wrong.

## Chapter after issue #32

Issue #33 adds the bounded continuous-improvement planner.

Its first useful evidence sources should be:

- repeated founder-run commands;
- failed installer/cutover attempts;
- gate failure and revision counts;
- duplicate correction patterns;
- flaky or slow checks;
- manual rescue events;
- unnecessary complexity and duplicate execution paths.

It should rank one improvement at a time by founder time saved, reliability gained, throughput gained, and complexity removed. Low-risk internal improvements may eventually be implemented and deployed automatically; strategic or customer-facing changes remain proposals requiring Daniel's judgment.

## Open product state

Product draft PR `#5351` is open, mergeable, CI-green, and intentionally unmerged. It is the first completed factory output under the controlled publisher. Its merge is a product/repository decision, not an infrastructure prerequisite for issue #32.

## Context-window handoff

A fresh conversation should start from this document plus:

- `docs/AUTOBUILDER_OPERATING_MANUAL_V1.md`;
- issue #32;
- issue #33;
- product draft PR #5351;
- the latest `main` of `DanielTabakman/msos-autobuilder`.

Do not repeat the completed host, relay, candidate-gate, revision-loop, or controlled-publisher construction. Treat those chapters as closed unless new evidence shows a regression.

The immediate instruction in the next context is:

> Continue issue #32. Build the fail-safe self-update supervisor through tests and GitHub review, preserve the separate-updater boundary, and stop only at the unavoidable first local bootstrap/witness boundary. Do not ask Daniel to manually orchestrate ordinary engineering work.
