# Bounded Automatic Merge and Cleanup V1

**Plane:** CONTROL-PLANE  
**Status:** Founder-approved charter proposal  
**Purpose:** Remove routine founder merge and cleanup work after an already-approved bounded objective has passed every predetermined gate.

## Founder decision

Daniel's decision occurs when a bounded work objective, authority class, acceptance criteria, allowed paths, forbidden paths, and validation contract are approved.

The system must not ask Daniel to make the same decision again merely because the resulting pull request is ready to merge.

For eligible work, successful implementation and validation should lead automatically to an exact-head guarded merge, post-merge verification, cleanup, terminal evidence, and continuation to the next approved item.

Daniel is interrupted only when execution reveals a genuinely new product, strategic, financial, legal, credential, destructive, external, or materially irreversible decision that was not covered by the original approval.

## Core model

```text
Approved bounded objective
        ↓
Immutable implementation job
        ↓
Host execution
        ↓
Result relay
        ↓
Candidate gate
        ↓
Bounded revision when configured
        ↓
Controlled publication validation
        ↓
Required GitHub checks
        ↓
Exact-head guarded merge
        ↓
Verify accepted head is on current main
        ↓
Cleanup and archive evidence
        ↓
Mark item terminal and continue
```

Automatic merge does not mean automatic product chartering. It means completing an already-approved bounded objective without asking the founder to repeat the approval.

## Required authority class

Every work item must declare exactly one merge authority class before implementation begins.

### `AUTO_MERGE_WHEN_GREEN`

Use for bounded work where the desired outcome and acceptance contract are already approved, including:

- implementation of already-approved product behavior;
- deterministic bug fixes with reproducible evidence;
- tests, diagnostics, and observability;
- internal refactoring that preserves accepted semantics;
- bounded reliability and performance work;
- accepted UI implementation that does not reopen product meaning;
- documentation that records an already-accepted decision;
- cleanup or simplification whose effects are fully described by the approved task.

The system may merge only after every required gate passes.

### `FOUNDER_DECISION_REQUIRED`

Use when a valid implementation outcome still requires Daniel to choose between materially different product or authority outcomes, including:

- product meaning or customer-facing behavior not resolved by the approved objective;
- trading, financial, pricing, or risk semantics;
- new external commitments;
- credentials, spending, legal, or compliance decisions;
- destructive data operations or irreversible migrations;
- materially broader scope than approved;
- compatibility costs or authority expansion not covered by the task;
- an unresolved canon conflict.

This class may produce a draft PR and evidence, but it must not merge automatically.

### `NEVER_MERGE`

Use for evidence-only branches, including:

- deliberate negative witnesses;
- rollback-test commits;
- reproductions;
- comparison experiments;
- disposable diagnostics;
- intentionally broken releases.

These branches must never be merged even when their checks pass.

## Automatic merge eligibility

An `AUTO_MERGE_WHEN_GREEN` pull request may merge only when all of the following are independently evidenced:

1. The originating work item records `AUTO_MERGE_WHEN_GREEN` before implementation begins.
2. The product objective and acceptance criteria are frozen and identifiable.
3. Exact source repository and source commit are recorded.
4. Allowed and forbidden paths are recorded.
5. The implementation changes only authorized paths.
6. Result relay evidence is complete and hash-consistent.
7. Candidate gate passes on a fresh pinned clone.
8. Any configured revision lifecycle is terminal and within its retry limit.
9. Controlled publication re-applies the accepted candidate to current product `main`.
10. Publication-time validation passes.
11. Required GitHub checks pass on the exact PR head.
12. The PR is mergeable with no unresolved conflict.
13. No ownership, canon, evidence, or authority conflict remains.
14. No new founder-decision condition was discovered.
15. The exact PR head still matches the reviewed and validated head immediately before merge.

Missing, malformed, stale, contradictory, or ambiguous evidence must block merge.

## Merge operation

The merger must:

- use the repository's approved merge strategy;
- require an exact-head guard;
- never merge a different head than the validated head;
- never force-push;
- never bypass required checks;
- never suppress a merge conflict;
- never rewrite product history unless the repository's accepted policy explicitly requires it;
- record the PR number, validated head, merge commit, base identity, timestamp, check conclusions, and authority class.

After merge, the merger must verify:

- the PR is in a merged terminal state;
- the merge commit is on the current default branch;
- the validated head is an ancestor of the current default branch or otherwise preserved according to the approved merge strategy;
- the originating issue/work item remains open or closes according to its explicit completion contract;
- no unrelated branch, PR, release, runtime, or product state changed.

A merge is not complete until post-merge identity verification succeeds.

## Automatic cleanup

After a verified successful merge, the system should perform bounded cleanup without founder intervention:

- delete the merged feature branch when repository policy permits;
- remove disposable product clones and temporary workspaces;
- expire generation-specific leases and process locks;
- archive job, relay, gate, revision, publication, merge, and cleanup evidence;
- mark the approved work item terminal so it is excluded from future selection;
- close or update the originating issue according to the issue's explicit acceptance contract;
- remove stale queue or prepared-dispatch records only through their reviewed lifecycle rules;
- emit one concise founder digest containing what merged, evidence, remaining risks, and any genuine decision still required.

Cleanup must preserve failure, rollback, security, audit, and provenance evidence.

If cleanup fails after a verified merge, the product merge remains valid. Cleanup failure becomes a bounded maintenance item and must not trigger destructive retries or conceal the successful merge.

## Failure and escalation behavior

Automatic merge must stop and return evidence when:

- the PR head changes after validation;
- required checks are absent, pending beyond policy, cancelled, or failed;
- candidate or publication validation is stale;
- source drift invalidates the approved candidate;
- a merge conflict exists;
- changed paths exceed authority;
- another writer owns overlapping paths or state;
- a review identifies a material defect;
- a new founder-decision condition appears;
- GitHub or repository protection prevents the guarded operation;
- post-merge identity cannot be verified.

The system must not resolve these conditions by weakening checks, broadening authority, retrying through alternate wrappers, or rewriting canon to match the implementation.

## Product repositories versus Autobuilder deployment

### Product repositories

After a real witness is accepted, bounded pre-approved PPE/MSOS product work may automatically merge when green.

This authority does not permit:

- autonomous product chartering;
- changes to trading or financial semantics not already approved;
- credential use outside the accepted execution boundary;
- deployment to external production environments unless separately authorized;
- creation of new external commitments.

### Autobuilder repository

Bounded low-risk Autobuilder changes may later receive automatic merge authority under the same exact gates.

Automatic merge of Autobuilder source is not the same as automatic installation.

Autonomous release activation and installed-runtime mutation remain blocked until issue #32 has an accepted real rollback witness and the exact-release supervisor boundary is independently proven for that authority class.

## Sequencing

1. Complete issue #50's installed zero-founder-return A → B witness.
2. Implement guarded product-repository merge and cleanup as a separate chapter.
3. Witness one real `AUTO_MERGE_WHEN_GREEN` PPE/MSOS item from approved objective through verified merge and cleanup.
4. Operate bounded product production with concise digests rather than merge prompts.
5. Add cross-repository adapters and witness each new repository independently.
6. Extend bounded merge to low-risk Autobuilder changes.
7. Permit autonomous Autobuilder installation only after issue #32's rollback and replay witnesses are accepted.

## Initial capacity and restrictions

The first witness is limited to:

- one product repository;
- one merge at a time;
- one pre-approved bounded work item;
- no simultaneous overlapping writer;
- no external deployment;
- no product-main direct write outside the guarded PR merge;
- no branch protection bypass;
- no force-push;
- no autonomous authority-class promotion;
- no automatic merge for `FOUNDER_DECISION_REQUIRED` or `NEVER_MERGE`.

## Required evidence contract

The durable merge record must include at minimum:

- originating work-item ID;
- merge authority class;
- objective/acceptance artifact identity;
- source repository and pinned source commit;
- PR number and repository;
- validated head SHA;
- required check names and terminal conclusions;
- candidate-gate report identity;
- publication report identity;
- revision lineage when present;
- merge method;
- merge commit SHA;
- post-merge default-branch SHA;
- ancestry or equivalent merge-strategy proof;
- cleanup actions and outcomes;
- originating issue/work-item terminal state;
- recorded timestamp;
- any founder escalation reason.

## Founder experience

For ordinary eligible work, Daniel should receive one completion digest such as:

```text
Merged automatically
Work: <approved objective>
PR: <number>
Merge commit: <sha>
Validation: candidate gate + publication + required checks passed
Cleanup: branch/workspace/state cleanup completed
Next: <next approved item or no eligible work>
Decision required: none
```

The system should not ask Daniel to review routine implementation mechanics after the objective and acceptance contract were already approved.

## Relationship to existing authority

This charter intentionally changes the current operating assumption that Daniel must make every final merge decision.

Until this charter is merged and the guarded merger is implemented and witnessed, the existing controlled publisher remains draft-only and no automatic merge authority exists.

Issue #50 remains responsible only for bounded capacity-one refill and its A → B witness. Automatic merge must not be smuggled into the refill witness.

Issue #33 remains the broader continuous-improvement program, but bounded automatic merge and cleanup should be implemented as its own independently reviewable capability rather than silently expanding planner authority.

## Acceptance for the capability

- [ ] canonical merge authority classes and schema;
- [ ] immutable merge-eligibility evidence;
- [ ] exact-head guarded merge implementation;
- [ ] required-check and mergeability enforcement;
- [ ] authority/canon/ownership conflict blocking;
- [ ] post-merge identity and ancestry verification;
- [ ] bounded branch/workspace/state cleanup;
- [ ] issue/work-item terminalization;
- [ ] concise founder digest;
- [ ] tests for stale head, failed/missing checks, conflict, authority breach, duplicate merge, and cleanup failure;
- [ ] one real PPE/MSOS `AUTO_MERGE_WHEN_GREEN` witness;
- [ ] no autonomous Autobuilder installation before issue #32 acceptance.

## COORDINATION STATUS

Agreement: aligned
Compared: founder direction, MSOS Autobuilder Operating Manual V1, issue #33, issue #50, issue #32, controlled publisher authority, and current exact-release architecture
Disagreement: current canon requires founder final merge decisions; this charter replaces that default for explicitly pre-approved bounded work after implementation and witness
Evidence gap: charter merge, guarded merger implementation, cleanup lifecycle, product witness, and later self-deployment rollback evidence
Ownership overlap: controlled publisher owns validated draft PR creation; the future guarded merger owns only terminal merge and cleanup for eligible PRs
Risk if unresolved: Daniel remains the routine merge operator and a queue of already-decided draft PRs accumulates
Recommended default: complete issue #50's witness, then implement bounded product auto-merge and cleanup as the immediate next production chapter
Founder decision required: no
