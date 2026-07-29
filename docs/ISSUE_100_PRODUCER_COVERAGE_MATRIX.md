# Issue 100 Producer Coverage Matrix

Scope: PR #104 producer evidence only. This does not implement the lifecycle recorder,
refill canonical consumption, migration, clean-generation authorization, release/install work,
Issue #50 runtime changes, or Issue #101 reduction.

| Producer | Outcome | Phase | Current source binding | Status |
| --- | --- | --- | --- | --- |
| `dispatch.prepared` | prepared | `dispatch_prepared` | immutable host-root `state/refill-evidence/sources/dispatch-prepared/<generation>/<job>.<intent>.json` receipt | implemented |
| `dispatch.submitted` | submitted | `dispatch_submitted` | `source_ref` repository/ref/commit/path plus job YAML SHA-256 | implemented |
| `host.execution` | imported | `host_awaiting_import` | `source_ref` repository/ref/commit/path plus job YAML SHA-256 | implemented for Git feed import |
| `host.execution` | pending | `host_pending` | immutable host-root `state/host-evidence/sources/execution/pending/<job>.<job-source-sha>.json` transition receipt containing exact job source bytes/hash and observed timestamp | implemented for Git feed import |
| `host.execution` | running | `host_running` | immutable host-root `state/host-evidence/sources/execution/running/<job>.<job-source-sha>.json` transition receipt containing exact job source bytes/hash and stable running timestamp | implemented |
| `host.execution` | completed | `execution_archived` | host-root `queue/completed/<job>/report.json` SHA-256 | implemented |
| `host.execution` | failed/interrupted | `execution_archived` | host-root `queue/failed/<job>/error.json` SHA-256 | implemented |
| `relay.result` | relayed | `result_relayed` | host-root relay staging `report.json` SHA-256 | implemented |
| `relay.result` | not-applicable after terminal host failure | `execution_archived` | host-root `queue/failed/<job>/error.json` SHA-256 | implemented |
| `gate.validation` | passed | `validation_recorded` | host-root `gate-report.json` SHA-256 | implemented |
| `gate.validation` | failed | `validation_recorded` | host-root `gate-report.json` SHA-256 | implemented |
| `gate.validation` | blocked/missing/conflict | `blocked` | service error marker | Issue #101 dependency for reduction semantics; producer failure is diagnostic-only in this PR |
| `revision.disposition` | queued | `revision_recorded` | host-root `gate-report.json` SHA-256 | implemented |
| `revision.disposition` | exhausted | `revision_recorded` | gate report plus terminal reason | Issue #101 dependency; current revision service raises before committing an exhausted primary disposition |
| `revision.disposition` | blocked/missing/conflict | `blocked` | service error marker | Issue #101 dependency for reduction semantics |
| `revision.disposition` | not-applicable after passed validation | `validation_recorded` | immutable host-root `state/revision-evidence/sources/not-applicable/<machine>/<job>.<gate-sha>.json` receipt; does not write `state/revision-loop-seen.json` | implemented |
| `publication_review.disposition` | drafted | `publication_review_recorded` | host-root `publication-report.json` SHA-256 | implemented |
| `publication_review.disposition` | awaiting-review | `publication_review_recorded` | publication report | Issue #101 dependency; current publisher creates draft PRs atomically |
| `publication_review.disposition` | rejected | `publication_review_recorded` | review/ledger receipt | Issue #101 dependency; no current automated rejection writer exists |
| `publication_review.disposition` | terminal-no-publication | `publication_review_recorded` | terminal receipt | Issue #101 dependency; no current automated terminal-no-publication writer exists |
| `publication_review.disposition` | blocked/missing/conflict | `blocked` | service error marker | Issue #101 dependency for reduction semantics |
| `publication_review.disposition` | not-applicable after failed validation/revision path | `validation_recorded` / `revision_recorded` | immutable host-root `state/publisher-evidence/sources/not-applicable/<machine>/<job>.<gate-sha>.json` receipt; does not write `state/controlled-publisher-seen.json` | implemented for failed gate reports |

Shared producer-head validation now enforces contiguous committed streams from the first head:
an absent head accepts only `producer_sequence == 1`, sequence booleans are rejected as
malformed, and an orphan immutable envelope is not treated as committed until a producer head
references it. Host feed integration has regression coverage where imported sequence 1 evidence
fails, pending sequence 2 writes only orphan evidence, and no producer head is published.

Shared envelope validation is canonical-semantic, not only syntactic. The validator binds
`dispatch.prepared` selected identity and capacity-one proof to `attempt_identity`, binds
`dispatch.submitted` feed commit/path/hash to `source_ref`, and rejects outcome/finality/proof
combinations that cannot represent a valid v1 producer event for host, relay, gate, revision,
and publication review evidence.

Canonical identity extraction returns `None` only when refill lifecycle markers are genuinely
absent. If refill markers are present but malformed, producer hooks preserve the committed
primary result and write a best-effort `producer-evidence-errors` diagnostic.

Producer evidence failures after committed primary side effects follow one shared policy:
preserve the primary operation, write `state/producer-evidence-errors/<producer>/...json`,
surface the evidence gap there, and do not reclassify or retry the primary action.

Legacy compatibility before Issue #102: canonical revision and publisher `not_applicable`
closures are emitted from separate immutable producer-owned source receipts. They do not add
new semantic entries to `state/revision-loop-seen.json` or
`state/controlled-publisher-seen.json`, so current refill lineage classification,
terminal-disposition handling, and awaiting-review counts retain their legacy meanings until
canonical refill consumption is introduced.
