# Autobuilder Repair Admission and Current Decision V1

**Plane:** CONTROL-PLANE  
**Status:** Founder accepted on 2026-07-27; pending repository review and merge  
**Scope:** Development and installed-runtime certification of `msos-autobuilder`  
**Purpose:** Prevent an indefinite sequence of locally reasonable repairs while preserving bounded progress toward a useful integrated build factory.

## 1. Founder outcome

The Autobuilder rebuild is not trying to prove that an AI can write one product quickly. That capability has already been demonstrated manually and through the earlier integrated factory.

The rebuild must prove that approved product work can accumulate through one coherent, governed workflow rather than becoming disconnected prototypes.

The current bounded witness is:

> One founder instruction enables capacity-one refill; work item A reaches a trustworthy terminal disposition; the factory detects free capacity and dispatches work item B without a second founder instruction; the factory is then paused and no C item is dispatched.

Issue #50 owns this witness.

## 2. Current decision

Current repository evidence:

- Issue #89 is complete.
- PR #90 merged the evidence-preserving refill-generation supersession capability into `main` as commit `6159071cbb7e7d58480cf8f4fbbb24052d483edf`.
- The currently approved installed release manifest still points to `canonical-dependency-source-hash-v1` at commit `a6bb9849cb6bdea9feed7209316f0cec146d194e`.
- Therefore the supersession repair is merged in GitHub but is not yet proven in the managed Windows installation.

The selected next sequence is:

1. Prepare and review one managed release request containing the merged PR #90 capability.
2. Publish and install that exact approved release through the existing external supervisor.
3. Confirm all managed services identify the exact installed release and refill remains paused before mutation.
4. Explicitly supersede the preserved unresolved generation using its exact generation ID and SHA-256.
5. Run the locked Issue #50 A -> automatic B witness.
6. Pause after B and prove no C dispatch.

No unrelated Autobuilder capability should interrupt this sequence.

## 3. Operating roles

### Founder

Daniel owns:

- product value and priority;
- the intended outcome and definition of done;
- whether an autonomy level remains worth pursuing;
- decisions where product canon, cost, or risk materially conflict.

Daniel does not need to classify low-level runtime evidence or choose among implementation repairs.

### Regular ChatGPT control-room thread

Regular Chat owns:

- reading current GitHub and runtime evidence;
- comparing a new failure with earlier incidents;
- classifying the failure under this policy;
- deciding whether the next engineering action is retry, bounded repair, boundary redesign, or stopping at a simpler autonomy level;
- producing the bounded GitHub handoff for Codex;
- independently reviewing Codex results and returning the mandatory Coordination Status block.

Regular Chat does not implement or silently change runtime state.

### Codex / implementation agent

Codex owns:

- bounded code changes;
- commands, tests, managed-release work, and runtime debugging;
- exact evidence capture;
- draft implementation PRs.

Codex must not treat every newly observed failure as automatically authorized for repair. A new material repair requires a control-room classification and bounded GitHub handoff.

### GitHub

GitHub remains the source of truth for:

- the selected witness;
- accepted decisions;
- incident history;
- implementation ownership;
- PR review;
- test and runtime evidence.

Chat context helps coordinate the work but is not canonical by itself.

## 4. Repair admission classification

After a real witness fails, classify the incident before authorizing code changes.

### A. Operational failure

Examples:

- transient network or GitHub failure;
- expired authentication;
- incomplete installation;
- stale local checkout;
- stopped process or malformed operator invocation.

Default response:

> Retry or repair operations. Do not add permanent lifecycle logic unless repetition shows a systemic boundary defect.

### B. Invariant bug

A general rule is implemented incorrectly.

Example:

> Identical committed dependency content must have the same identity across Windows CRLF and Linux LF working-tree representation.

Default response:

> Permit a bounded repair in one canonical shared implementation, with regression tests covering the whole failure class.

A good invariant repair should compress or centralize logic rather than add an incident-specific exception.

### C. Missing lifecycle state

A legitimate state or transition exists in the real system but is absent from the model.

Example:

> A paused unresolved generation may need explicit founder-authorized supersession while its historical evidence remains immutable.

Default response:

> Permit one explicit state-model addition with named preconditions, forbidden transitions, crash/retry semantics, historical-evidence behavior, and tests.

### D. Repeated boundary failure

Different symptoms repeatedly arise because the same system boundary cannot determine or communicate canonical truth.

Examples:

- refill repeatedly cannot determine whether a work item is terminal;
- revision and publisher evidence repeatedly require new compatibility interpretation;
- retry completion repeatedly conflicts with later founder pause or resume intent;
- historical evidence repeatedly requires another special lifecycle branch.

Default response:

> Stop incident-by-incident repairs. Open an architecture review that consolidates ownership and removes duplicate interpretation before more implementation.

## 5. Current warning boundary and circuit breaker

The current warning boundary is:

> **Refill generation and terminal-evidence lifecycle**

The same failure family includes ambiguity involving:

- active or historical generation identity;
- `current_attempt` terminality;
- gate, revision, or publisher disposition;
- old marker or ledger compatibility;
- interrupted versus completed retries;
- a historical operation overriding later founder pause or resume intent;
- inability to determine whether A is terminal and B may be selected.

PR #90 / Issue #89 is admitted as one legitimate missing-lifecycle-state repair and should be installed and tested.

**Circuit breaker:**

> The next material failure in this same family does not automatically receive another bounded repair. It triggers a refill-lifecycle architecture review before further code changes.

An unrelated operational failure or invariant bug may still receive the appropriate narrower response.

## 6. Minimal incident record

No new service, dashboard, database, or automated classifier is required at this stage.

For each real witness failure, add one concise GitHub issue comment containing:

```text
Witness goal:

Last verified stage:

Plain-language failure:

Failed boundary:

Classification:
operational | invariant_bug | missing_lifecycle_state | repeated_boundary_failure

Related incidents:

Complexity change proposed:
new persistent state | new transition | new authority | compatibility branch | none

Decision:
retry | bounded repair | architecture review | stop at simpler autonomy level

Expected witness advancement:

Founder decision required:
yes | no
```

The record exists to support global judgment, not to create paperwork. A comment is sufficient unless an architecture review is triggered.

## 7. Evidence of convergence

A repair is not justified only because it is bounded. It must also plausibly advance the selected witness.

For Issue #50, the relevant progression is:

1. approved work exists;
2. A is selected;
3. immutable job is created;
4. job is imported;
5. worker executes;
6. patch is relayed;
7. candidate is reconstructed and validated;
8. revision is resolved if required;
9. publication disposition is established;
10. A is trustworthy terminal;
11. refill detects free capacity;
12. B is selected automatically;
13. B is dispatched;
14. refill is paused;
15. no C is dispatched.

A repair is convergent when the next installed witness reaches a materially later stage. Repeated repairs at the same boundary without witness advancement are evidence for architecture review.

## 8. Architecture-review response

When the circuit breaker triggers:

1. Freeze new refill capability and incident-specific repair work.
2. Gather the related issues, PRs, runtime reports, and preserved evidence.
3. Define one canonical work-item lifecycle and terminal-disposition contract.
4. Assign one owner for each transition and each persistent record.
5. Make refill consume the canonical disposition instead of reconstructing truth independently from multiple service artifacts.
6. Define one compatibility or migration boundary for historical evidence.
7. identify duplicated branches, markers, or classifications that can be deleted.
8. Replay all prior incidents as regression fixtures.
9. Resume the A -> B witness only after the simplified boundary is reviewed.

The objective of redesign is not to restart the Autobuilder. It is to make the failing boundary smaller and more authoritative.

## 9. Exit decisions

### A -> B witness passes

- Close the rebuild certification chapter owned by Issue #50.
- Freeze capability expansion temporarily.
- Use the factory on real integrated product work.
- Measure founder intervention and coherent product accumulation before adding capacity two, automatic merge, or self-improvement.

### Unrelated ordinary bug appears

- Permit a bounded fix and rerun the same witness.

### Same lifecycle/evidence family fails again

- Trigger architecture review under Section 8.
- Do not authorize another incident-specific state, marker, receipt, or compatibility branch first.

### Simpler system dominates

If evidence shows continuous refill costs more to maintain than it saves, preserve the working governed semi-automatic path: approved one-shot dispatch, isolated execution, validation, revision, and draft publication.

## 10. Non-goals

This document does not:

- implement product or Autobuilder code;
- change Issue #50's witness;
- authorize capacity above one;
- authorize automatic merge, deployment, new product scope, or autonomous improvement;
- require Daniel to understand internal implementation details;
- replace the repository control-plane contract;
- create a new governance application before the manual process is proven.

## COORDINATION STATUS

Agreement: aligned  
Compared: founder direction in the current control-room thread; `Probability-prediction-engine/docs/SOP/CHATGPT_GITHUB_CODEX_CONTROL_PLANE_V1.md`; Autobuilder Issues #50, #82, #89; PR #90; current approved release manifest  
Disagreement: none  
Evidence gap: independent review and merge of this control-plane document; managed release and installed A -> B witness remain outstanding  
Ownership overlap: this document governs classification and handoff only; Codex and existing runtime issues retain implementation ownership  
Risk if unresolved: locally reasonable repairs may continue without a global circuit breaker or durable founder-readable decision state  
Recommended default: review and merge this document without interrupting the already-selected PR #90 release and Issue #50 witness sequence  
Founder decision required: no
