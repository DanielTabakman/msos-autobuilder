# Canonical Refill Attempt Disposition V1

Status: Draft architecture for Issue #98

Scope: This document defines the target lifecycle contract for capacity-one refill attempts.
It does not authorize implementation, runtime state changes, Issue #50 retry/exclusion, or
B/C dispatch.

## Inputs Reviewed

- Issue #98, "Architecture review: canonical terminal disposition for refill attempts".
- Issue #98 decision comment `#5108427193`.
- Independent architecture review `#4800845999` on draft PR #99.
- Draft PR #92 repair-admission document state.
- Issue #50, especially runtime evidence comment `#5108108854` and the circuit-breaker
  comment that opened #98.
- Issue #89, "Add evidence-preserving supersession for paused unresolved refill generations".
- Issue #82, "Canonicalize dependency-source hashes across Git line endings".
- Issue #95, "Make refill service graceful stop race-free and deterministic".
- `docs/PERSISTENT_WINDOWS_HOST_V1.md`.
- `docs/CANDIDATE_INTEGRATION_GATE_V1.md`.
- `docs/CONTROLLED_DRAFT_PUBLISHER_V1.md`.
- `docs/AUTOBUILDER_OPERATING_MANUAL_V1.md`.
- `src/msos_autobuilder/refill_controller.py`.
- `src/msos_autobuilder/results_relay.py`.
- `src/msos_autobuilder/candidate_gate.py`.
- `src/msos_autobuilder/revision_loop.py`.
- `src/msos_autobuilder/controlled_publisher.py`.
- `src/msos_autobuilder/service_error_lifecycle.py`.
- Managed MSOS source copy of `docs/SOP/CHATGPT_GITHUB_CODEX_CONTROL_PLANE_V1.md`.

Evidence note: `docs/AUTOBUILDER_REPAIR_ADMISSION_AND_CURRENT_DECISION_V1.md` exists on
open draft PR #92. It is not on `main`. Issue #98 contains the active circuit-breaker
decision for Issue #50.

## Current Lifecycle And Evidence Owners

Current refill state is generation-scoped and lives under the host root:

| Durable record | Current writer | Current reader | Current meaning |
|---|---|---|---|
| `state/refill-policy.json` | Refill CLI/service | Refill service | Enabled, desired capacity, pause/resume, dispatch limits |
| `state/refill-generation.json` | Refill CLI/service | Refill service | Active generation, current attempt, exclusions, provider retry state |
| `state/refill-generation-history/*.json` | Refill CLI/service | Refill service/operator | Immutable archived generation bytes |
| `state/refill-generation-supersessions/*.json` | Refill CLI/service | Refill service/operator | Exact supersession receipt for acknowledged ambiguous historical generations |
| `queue/pending/<job-id>/job.yaml` | Host feed importer | Host/refill | Imported but not yet running job |
| `queue/running/<job-id>/job.yaml` | Host | Host/refill | Claimed running job |
| `queue/completed/<job-id>/report.json` | Host | Relay/refill | Worker completed with patches/report |
| `queue/failed/<job-id>/error.json` | Host | Refill/operator | Worker or host failed before a completed result |
| `state/results-relay-seen.json` | Results relay | Relay/operator | Completed host result was relayed to results branch |
| `state/candidate-gate-seen.json` | Candidate gate | Gate/error lifecycle | Gate processed immutable result evidence |
| `state/candidate-gate-results-repo/results/*/<job-id>/gate-report.json` | Candidate gate | Refill/revision/publisher | Candidate validation result |
| `state/revision-loop-seen.json` | Revision loop | Refill/error lifecycle | Failed gate produced a revision job |
| `state/controlled-publisher-seen.json` | Controlled publisher | Refill/error lifecycle | Passed candidate produced verified draft-publication disposition |
| `state/*-error.json` | Gate/revision/publisher | Refill/error lifecycle | Service-local error marker |
| `state/*-service-success.json` | Gate/revision/publisher | Error lifecycle/refill | Later service success that may supersede service-local errors |

Current refill reconstructs terminality by reading host archives, gate reports, revision
ledger entries, publisher ledger entries, service error markers, and feed/queue state.
`_classify_failed_attempt` only recognizes structured provider failures and a small
provider text-token fallback. Ordinary worker failures, including the Issue #50
`.pytest_cache` `PermissionError`, become `category: unknown`, and reconcile converts that
into `BLOCKED` with `reason: ambiguous_attempt`.

## Required V1 Architecture Decision

The Attempt Lifecycle Recorder is accepted as the sole logical canonical disposition
writer. It owns canonical attempt, work-item, generation, and refill-action reduction.

For v1, the recorder is hosted inside the existing persistent results relay service and
process. This preserves the existing six-service managed topology:

1. persistent host;
2. result relay, now also hosting the logical Attempt Lifecycle Recorder;
3. candidate gate;
4. revision loop;
5. controlled draft publisher;
6. capacity-one refill service.

This is a logical ownership decision, not a seventh managed service. A future standalone
recorder service would require a separate accepted architecture decision, supervisor and
witness changes, and proof that splitting the process is simpler than the relay-hosted v1.

The boundaries are:

| Boundary | Decision |
|---|---|
| Logical component ownership | Attempt Lifecycle Recorder is the only canonical disposition writer and reducer. |
| Process/service deployment | Recorder runs inside the existing persistent relay process for v1. |
| Lifecycle lock | Recorder alone writes through `state/attempt-lifecycle.lock`; refill, host, gate, revision, and publisher do not hold it. |
| Durable canonical paths | Recorder writes only under `state/attempt-lifecycle/`; refill continues to own `state/refill-generation*.json`. |
| Evidence ownership | Refill, host, relay, gate, revision, publisher, and service-error lifecycle own their source evidence envelopes. |

Current-versus-target data flow:

```mermaid
flowchart LR
  subgraph Current
    H["Host queue archives"] --> R1["Refill classifiers"]
    G["Gate reports"] --> R1
    V["Revision ledger"] --> R1
    P["Publisher ledger"] --> R1
    E["Service markers"] --> R1
    R1 --> RG["refill-generation.json"]
  end

  subgraph Target
    BD["Build-next/refill dispatch envelope"] --> L["Attempt Lifecycle Recorder in relay process"]
    H2["Host execution envelope"] --> L
    RR["Relay envelope"] --> L
    G2["Gate validation envelope"] --> L
    V2["Revision disposition envelope"] --> L
    P2["Publisher/review envelope"] --> L
    L --> J["append-only transition journal"]
    J --> S["materialized current snapshot"]
    S --> R2["Refill consumer"]
    R2 --> RG2["refill-generation.json"]
  end
```

## Versioned Lifecycle Evidence Envelopes

Every post-recorder attempt must be represented by small versioned structured lifecycle
evidence envelopes. The recorder validates and reduces these envelopes. It must not parse
arbitrary service text, infer terminality from raw ledgers, or inherit refill's current
multi-artifact interpretation role for post-recorder attempts.

Raw service-ledger adapters and text interpretation are migration-only. They are allowed
only for historical evidence created before the recorder release and must be fenced by
generation metadata and source hashes.

Each envelope includes at least:

| Field | Meaning |
|---|---|
| `schema_version` | Evidence schema version, with reducer compatibility declared explicitly. |
| `evidence_kind` | One of the producer kinds below. |
| `evidence_id` | Stable immutable identity for this evidence event. |
| `attempt_identity` | Canonical repository, pipeline, work item, generation, and job identity. |
| `source_path` or `source_ref` | Durable local path, Git ref/path, or feed identity that produced the envelope. |
| `source_sha256` | SHA-256 of the immutable source bytes. |
| `producer` | Service/component name and release identity. |
| `recorded_at` or `observed_at` | Producer timestamp. |
| `payload` | Structured stable codes for the relevant outcome or disposition. |

Required post-recorder envelope kinds:

| Envelope kind | Producer | Required stable payload codes |
|---|---|---|
| `dispatch.prepared` | Refill/build-next | selected work item, generation ID, dispatch intent hash, capacity slot identity |
| `dispatch.submitted` | Build-next/refill | jobs feed commit, feed path, submitted job hash |
| `host.execution` | Persistent host | pending/running/completed/failed execution outcome, host archive path, error class code when failed |
| `relay.result` | Results relay | relayed commit, canonical report hash, complete patch reconstruction integrity |
| `gate.validation` | Candidate gate | passed/failed/blocked/missing/conflict, validation contract identity, gate report hash |
| `revision.disposition` | Revision loop | queued/exhausted/blocked/not required/missing/conflict, descendant job identity when queued |
| `publication_review.disposition` | Controlled publisher or review owner | awaiting review/drafted/rejected/terminal no-publication/blocked/missing/conflict |

## Canonical Record Model

The canonical model is one orthogonal current record per attempt plus one current record per
canonical work item. It is not one overloaded lifecycle-state enum.

Canonical attempt identity contains:

| Field | Meaning |
|---|---|
| `repository_identity` | Canonical owner/repository and product repository digest or configured repository ID. |
| `pipeline_id` | Autobuilder pipeline or managed release pipeline identity. |
| `work_item_id` | Stable work item identifier. |
| `work_item_digest` | Digest of the work item source bytes or canonical request body. |
| `generation_id` | Refill generation that created this attempt. |
| `job_id` | Host job ID for this attempt. |
| `attempt_number` | Monotonic attempt number within the generation/work-item lineage. |

Canonical repository/pipeline/work-item identity is required for both attempt and
work-item records. Bare `work-item-id` is insufficient.

The current attempt snapshot includes these fields:

| Field | Values |
|---|---|
| `lifecycle_phase` | `dispatch_prepared`, `dispatch_submitted`, `host_awaiting_import`, `host_pending`, `host_running`, `execution_archived`, `result_relayed`, `validation_recorded`, `revision_recorded`, `publication_review_recorded`, `blocked` |
| `execution_outcome` | `not_started`, `running`, `completed`, `failed`, `interrupted`, `missing`, `conflict` |
| `validation_outcome` | `not_started`, `passed`, `failed`, `blocked`, `missing`, `conflict` |
| `revision_disposition` | `not_required`, `queued`, `exhausted`, `blocked`, `missing`, `conflict` |
| `publication_review_disposition` | `not_required`, `awaiting_review`, `drafted`, `rejected`, `terminal_no_publication`, `blocked`, `missing`, `conflict` |
| `retry_eligibility` | `none`, `same_item_once_after`, `same_item_denied`, `operator_required` |
| `evidence_integrity` | `complete`, `pending`, `missing`, `conflict`, `stale`, `migration_only` |
| `attempt_terminal` | `true`, `false` |
| `item_terminal` | `true`, `false` |
| `refill_action` | `occupy_capacity`, `retry_same_item`, `exclude_item_and_select_next`, `block_fail_closed` |

Historical supersession is not a lifecycle phase. It is generation and migration metadata
attached to records that preserve pre-recorder ambiguity.

## Immutable Transition Journal And Snapshot

Canonical lifecycle storage has two layers:

| Record | Writer | Semantics |
|---|---|---|
| `state/attempt-lifecycle/transitions/<identity-digest>/<sequence>.json` | Attempt Lifecycle Recorder | Immutable append-only transition journal. |
| `state/attempt-lifecycle/current/attempts/<identity-digest>.json` | Attempt Lifecycle Recorder | Materialized current attempt snapshot derived from the journal. |
| `state/attempt-lifecycle/current/work-items/<identity-digest>.json` | Attempt Lifecycle Recorder | Materialized current work-item snapshot derived from attempt snapshots. |
| `state/attempt-lifecycle/current/generations/<generation-id>.json` | Attempt Lifecycle Recorder | Generation metadata, migration status, and reduced-through watermarks. |
| `state/attempt-lifecycle/recovery-ledger.json` | Attempt Lifecycle Recorder | Recorder recovery and replay status. |

Each transition includes:

| Field | Meaning |
|---|---|
| `sequence` | Monotonic sequence number within the attempt identity journal. |
| `previous_transition_sha256` | SHA-256 of the previous transition bytes, or `null` for sequence 1. |
| `transition_sha256` | SHA-256 of the canonical transition bytes, excluding this field. |
| `source_evidence` | List of source evidence identities, source refs/paths, and SHA-256 values reduced into this transition. |
| `recorded_at` | Recorder timestamp. |
| `reducer_version` | Deterministic reducer implementation version. |
| `schema_version` | Canonical transition/schema version. |
| `from_snapshot_sha256` | SHA-256 of the previous materialized snapshot, or `null` for first transition. |
| `to_snapshot_sha256` | SHA-256 of the resulting materialized snapshot. |

The materialized snapshots are replaceable derived state. The transition journal is the
audit source of truth. Replay from journal plus immutable source evidence must deterministically
produce the same current snapshots and `reduced_through` watermarks.

## Refill Freshness Handshake

Refill may not retry, exclude an item, or select the next item from stale canonical evidence.
Before any such decision, refill must do one of the following:

1. synchronously invoke the canonical reducer in the relay-hosted recorder and wait for the
   resulting transition/snapshot or an explicit fail-closed result; or
2. verify an exact durable `reduced_through` watermark that covers every latest known source
   evidence identity and SHA-256 relevant to the active generation and attempt.

`reduced_through` is exact, not time-based. It binds each producer's latest known evidence
identity and SHA-256 for the attempt or generation. A heartbeat alone is insufficient.

Stale, absent, missing, or conflicting canonical evidence blocks fail-closed. For
post-recorder attempts, refill may never fall back to direct host, gate, revision, publisher,
relay, service-ledger, or text interpretation. Compatibility fallback is migration-only and
is forbidden once an attempt has post-recorder dispatch evidence.

## Item Disposition To Refill Action

The work-item snapshot contains a deterministic `item_disposition` and total mapping to
`refill_action`:

| `item_disposition` | Required canonical facts | `refill_action` |
|---|---|---|
| `active_dispatch_prepared` | Dispatch prepared or submitted; attempt not terminal | `occupy_capacity` |
| `active_host_running` | Host pending/running/importing; attempt not terminal | `occupy_capacity` |
| `active_relay_or_gate_pending` | Execution completed; relay/gate not yet terminal; evidence integrity pending | `occupy_capacity` |
| `active_revision_pending` | Validation failed; revision disposition not terminal; evidence integrity pending | `occupy_capacity` |
| `active_publication_review_pending` | Validation passed; publication/review disposition awaiting review or pending | `occupy_capacity` |
| `retry_same_item_authorized` | Attempt terminal, item non-terminal, retry eligibility `same_item_once_after`, bounded retry count available, freshness proven | `retry_same_item` |
| `operator_required_execution_failed` | Execution failed, attempt terminal, item non-terminal, retry eligibility `operator_required` | `block_fail_closed` |
| `operator_required_validation_blocked` | Validation blocked/conflicting without queued revision | `block_fail_closed` |
| `operator_required_revision_blocked` | Revision disposition blocked/missing/conflict | `block_fail_closed` |
| `operator_required_publication_blocked` | Publication/review blocked/missing/conflict or rejected without item-terminal reason | `block_fail_closed` |
| `evidence_missing` | Required canonical evidence absent after reducer/watermark proof | `block_fail_closed` |
| `evidence_conflict` | Contradictory or mutated evidence hashes | `block_fail_closed` |
| `evidence_stale` | Durable `reduced_through` does not cover latest known evidence | `block_fail_closed` |
| `item_terminal_success_drafted` | Publication/review disposition drafted and verified | `exclude_item_and_select_next` |
| `item_terminal_no_publication` | Publication/review disposition terminal no-publication with accepted reason | `exclude_item_and_select_next` |
| `item_terminal_rejected` | Publication/review disposition rejected with accepted item-terminal reason | `exclude_item_and_select_next` |
| `item_terminal_revision_exhausted` | Validation failed and revision exhausted with accepted item-terminal reason | `exclude_item_and_select_next` |
| `migration_historical_preserved` | Pre-recorder generation preserved byte-for-byte; migration metadata only | `block_fail_closed` |

Any new `item_disposition` value must extend this table in the same PR that introduces it.
There is no free-form "per reason" terminal prose.

Semantics:

1. `attempt_terminal=true` means the specific job attempt has stopped changing and should
   not be rerun by accident.
2. `item_terminal=true` means refill may exclude the work item and select the next eligible item.
3. `attempt_terminal=true` does not imply `item_terminal=true`. Failed execution, failed
   gate, and publisher rejection can still require retry, revision, founder review, or a
   fail-closed block.
4. Ordinary host execution failures, including filesystem `PermissionError`, are deterministic:
   `execution_outcome=failed`, `attempt_terminal=true`, `retry_eligibility=operator_required`,
   `item_terminal=false`, and `item_disposition=operator_required_execution_failed` unless a
   configured structured rule marks the class as safe for an automatic same-item retry.
5. Provider/systemic failures use structured provider evidence only. Text-token fallback in
   refill is a migration-only compatibility behavior and should be deleted after migration.
6. Automatic retry is same-item only, bounded, explicitly counted, freshness-proven, and never
   item-terminal.
7. Failed validation is not item-terminal until revision disposition is canonical.
8. Passed validation is not item-terminal until publication/review disposition is canonical.
9. Evidence missing, conflict, and stale states are not retry signals. They block fail-closed.

## Crash, Restart, And Delayed Evidence

The lifecycle recorder must be idempotent and restart-safe:

1. It reads current durable evidence envelopes and writes through `state/attempt-lifecycle.lock`.
2. It appends journal transitions atomically before replacing materialized snapshots.
3. A crash before the journal append is retried by rereading source evidence.
4. A crash after the journal append but before snapshot replacement is repaired by replaying
   the journal.
5. A crash after snapshot replacement is detected by matching transition and snapshot hashes
   and is not duplicated.
6. Delayed evidence may advance a non-terminal snapshot only when all prior evidence hashes
   still match. For example, a gate envelope appearing after completed relay evidence advances
   validation outcome to `failed` or `passed`.
7. Delayed evidence may supersede `evidence_missing` only if the missing state was recorded as
   provisional, the recovery window is still open, and the previous transition hashes replay.
   `evidence_conflict` is never automatically superseded.
8. Service restarts do not clear canonical lifecycle records. They may only add new source
   evidence envelopes that the recorder consumes.
9. Refill restart consumes the latest canonical work-item snapshot after the freshness
   handshake. It must not reclassify host, gate, revision, relay, or publisher evidence
   independently for post-recorder attempts.

## Historical Migration Boundary

Migration is one explicit boundary, not a growing list of incident branches:

1. Existing Issue #89 evidence and the current Issue #50 A evidence remain byte-for-byte
   unchanged. No migration rewrites, moves, retries, excludes, or regenerates them.
2. Migration creates canonical records under a one-time transaction ID:
   `migration.issue89-issue50A.<utc-timestamp>.<migration-input-sha256>`.
3. The migration transaction input lists every source path/ref, source byte length, source
   SHA-256, generation ID, job ID, canonical repository/pipeline/work-item identity, and the
   current refill classification bytes it preserves.
4. The migration transaction output lists every journal transition and snapshot digest it
   creates. The transaction is valid only if every input SHA-256 still matches at commit time.
5. Current A is migrated with known structured facts, not merely historical supersession:
   `execution_outcome=failed`, `attempt_terminal=true`, `item_terminal=false`,
   `retry_eligibility=operator_required`, `evidence_integrity=migration_only`, and
   `item_disposition=operator_required_execution_failed`.
6. Issue #89 unresolved historical generation records are migrated with
   `evidence_integrity=migration_only`, `item_disposition=migration_historical_preserved`,
   and generation metadata that records supersession. Historical supersession is metadata,
   not `lifecycle_phase`.
7. Existing Issue #89 supersession machinery is reused only as the byte-preserving
   generation acknowledgement source and receipt format. It is not reused as a normal
   lifecycle reducer, attempt phase, retry authorization, or item-terminal proof.
8. Historical migration records cannot authorize B because their `refill_action` is
   `block_fail_closed`, their `evidence_integrity` is `migration_only`, their generation
   metadata marks them `pre_recorder=true`, and the freshness handshake rejects them as a
   basis for post-recorder next-item selection.
9. Clean post-recorder generation creation starts with fresh `dispatch.prepared` and
   `dispatch.submitted` envelopes from the reducer-enabled release. It does not inherit
   historical `reduced_through` watermarks, retry counts, terminality, or exclusions except
   through explicit byte-bound generation metadata.
10. New attempts created after the lifecycle-recorder release must have canonical lifecycle
    records from dispatch onward. Absence is `evidence_missing`, not compatibility fallback.
11. Migration closes after the first accepted managed release and installed proof that a fresh
    A attempt is lifecycle-recorded from dispatch through terminal or blocked disposition.

## Duplicate Interpretation And Deletion Plan

After migration and a successful fresh lifecycle witness, delete or demote these duplicate
interpretation paths:

| Current path | Target action |
|---|---|
| `refill_controller._classify_attempt` reading host/gate/revision/publisher evidence | Replace with canonical attempt/work-item disposition read plus freshness handshake |
| `refill_controller._classify_failed_attempt` text-token provider fallback | Delete after historical migration |
| Refill direct reads of `candidate-gate-results-repo` for terminality | Delete; lifecycle recorder owns interpretation |
| Refill direct reads of `revision-loop-seen.json` for terminality | Delete; lifecycle recorder owns interpretation |
| Refill direct reads of `controlled-publisher-seen.json` for terminality | Delete for terminality; preserve only as migration-only raw input until producer envelopes exist |
| Refill `revision_disposition_missing` and `revision_descendant_disposition_missing` categories | Replace with canonical `revision_disposition=missing` and `item_disposition=operator_required_revision_blocked` |
| Refill `ambiguous_attempt` caused by ordinary failed host archive | Replace with canonical `operator_required_execution_failed` |
| Incident-specific supersession as normal lifecycle tool | Retain only as historical migration/operator escape hatch |
| Service error marker evaluation used by refill as terminal proof | Lifecycle recorder may consume migration-only markers; refill should not |

No source evidence is deleted. Only duplicate interpretation code paths are removed after
the canonical writer has proven coverage.

## Prior-Incident Regression Fixture Map

| Incident fixture | Source issue/evidence | Expected canonical result |
|---|---|---|
| Provider retry/backpressure | Existing structured provider failure tests and refill provider retry behavior | `retry_same_item_authorized` only after trustworthy retry time and only once |
| Failed candidate gate requiring revision | Issue #82 first A dependency-hash gate failure | `validation_outcome=failed`, then `revision_disposition=queued` or `exhausted`; not item-terminal until revision disposition exists |
| Missing publisher/revision disposition | Issue #89 preserved generation | Migration metadata with `item_disposition=migration_historical_preserved`; fresh equivalent becomes `evidence_missing` and blocks |
| Interrupted generation recovery | Existing prepared dispatch/recovery and Issue #95 restart-stop boundary | Restart replays canonical transition or blocks on conflict without duplicate dispatch |
| Ordinary host execution failure | Issue #50 comment `#5108108854`, `.pytest_cache` `PermissionError` | `execution_outcome=failed`, `attempt_terminal=true`, `retry_eligibility=operator_required`, `item_terminal=false`, refill blocks on canonical disposition |
| Successful A terminality followed by automatic B | Issue #50 acceptance goal | A reaches `item_terminal=true` through drafted, terminal no-publication, terminal rejected, or revision-exhausted item disposition; freshness handshake passes before refill excludes A and dispatches B |
| Feed checkout cache corruption | Issue #50 comment `#5107850091` and recovery update | Not an attempt disposition until a job is submitted; remains runtime health/recovery fixture |
| Graceful stop during lifecycle reconciliation | Issue #95 | Stop request does not lose lifecycle writes; no new reconcile starts after accepted stop |

## Smallest Bounded Implementation Sequence

This sequence preserves the existing six-service topology. It does not add a seventh
managed service.

1. Add canonical schemas, path constants, journal validators, and read-only snapshot
   validators for attempt, generation, lineage, and work-item records. No refill behavior
   change yet.
2. Add lifecycle evidence envelope producers for build-next/refill dispatch, host execution,
   relay, gate validation, revision disposition, and publication/review disposition. Keep raw
   adapters migration-only.
3. Host the Attempt Lifecycle Recorder inside the existing persistent relay service with one
   lifecycle lock, append-only journal writes, materialized snapshot replacement, replay, and
   `reduced_through` watermark production.
4. Add an explicit ID/hash-bound migration command/tests for the preserved Issue #89 generation
   and current Issue #50 A generation without editing either evidence source.
5. Wire refill to perform the synchronous reducer or exact-watermark freshness handshake and
   consume canonical work-item disposition for post-recorder attempts. Old interpretation
   remains fenced behind the migration boundary only.
6. Add focused fixtures from the map above, including the exact `.pytest_cache`
   `PermissionError` archive shape.
7. Remove refill direct interpretation for post-recorder attempts.
8. Open a managed release request, review, merge, install through the external supervisor, and
   verify the recorder-hosting relay plus all six existing managed services are healthy.
9. Run a fresh Issue #50 A attempt only after the release is installed, and prove canonical
   disposition is written and fresh before refill decides whether to block, retry, exclude, or
   dispatch B.
10. After the fresh witness is accepted, delete migration-only classifier branches and
    text-token fallback in a separate cleanup PR.

## Criteria For Resuming Issue #50

Issue #50 may resume only when all of the following are true:

1. This architecture, or an explicit accepted replacement, is reviewed in GitHub.
2. The bounded implementation issues derived from this design are opened and accepted.
3. A draft implementation PR proves the canonical lifecycle writer, migration boundary,
   freshness handshake, and refill consumer behavior with focused tests.
4. Linux and Windows CI pass.
5. A managed exact release containing the canonical lifecycle implementation is published and
   installed through the external supervisor.
6. Runtime evidence shows active release and all managed service witnesses match that exact
   release.
7. The preserved Issue #89 generation and current Issue #50 A evidence remain byte-preserved.
8. The current Issue #50 A `PermissionError` is represented by canonical lifecycle migration
   evidence, not by a manual retry, exclusion, or new refill classifier.
9. Refill remains capacity one. No B or C has been manually submitted.
10. The next A attempt is fresh post-recorder evidence, and refill consumes a fresh canonical
    disposition before advancing to automatic B.

## Coordination Status

Agreement: partial

Compared: Issue #98; Issue #98 decision comment `#5108427193`; independent architecture
review `#4800845999`; PR #99 head `60f9f76`; PR #92 repair-admission draft; Issue #50
comment `#5108108854`; Issue #89; Issue #82; Issue #95; current refill, relay, host, gate,
revision, publisher, and service-error lifecycle code; active control-plane SOP.

Disagreement: resolved within this draft revision. The accepted direction is a single
logical Attempt Lifecycle Recorder, hosted in the existing persistent relay process for v1,
with structured post-recorder evidence envelopes, orthogonal canonical fields, append-only
transitions, exact freshness proof, and hash-bound migration. No runtime implementation is
authorized until independent re-review accepts the architecture.

Evidence gap: independent re-review of this corrected architecture; accepted canonical
schemas; recorder implementation; envelope producer implementation; migration evidence; no
fresh post-recorder A-to-B witness yet.

Ownership overlap: current refill overlaps host, gate, relay, revision, publisher, and
service-error evidence interpretation. Target design makes those services evidence owners,
the relay-hosted recorder the only logical disposition writer, and refill a freshness-checked
consumer of canonical work-item disposition.

Risk if unresolved: every new worker, gate, revision, relay, or publisher outcome can require
another refill-specific classifier, marker, supersession, or compatibility branch, or the
ambiguity can move into a stale seventh interpreter instead of being compressed.

Recommended default: keep PR #99 draft and return this docs-only revision for independent
re-review before any Issue #50 runtime repair, retry, exclusion, B/C submission, or
`.pytest_cache` workaround.

Founder decision required: no
