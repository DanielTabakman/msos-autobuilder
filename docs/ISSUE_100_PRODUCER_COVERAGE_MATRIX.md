# Issue 100 Producer Coverage Matrix

Scope: PR #104 producer evidence only. This does not implement the lifecycle recorder,
refill canonical consumption, migration, clean-generation authorization, release/install work,
Issue #50 runtime changes, or Issue #101 reduction.

| Producer | Outcome | Phase | Current source binding | Status |
| --- | --- | --- | --- | --- |
| `dispatch.prepared` | prepared | `dispatch_prepared` | host-root `state/refill-generation.json` SHA-256 | implemented |
| `dispatch.submitted` | submitted | `dispatch_submitted` | `source_ref` repository/ref/commit/path plus job YAML SHA-256 | implemented |
| `host.execution` | imported | `host_awaiting_import` | `source_ref` repository/ref/commit/path plus job YAML SHA-256 | implemented for Git feed import |
| `host.execution` | pending | `host_pending` | host-root `queue/pending/<job>.yaml` SHA-256 | implemented for Git feed import |
| `host.execution` | running | `host_running` | host-root `queue/running/<job>.yaml` SHA-256 | implemented |
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
| `revision.disposition` | not-applicable after passed validation | `validation_recorded` | gate report | Issue #101 dependency; current service only processes failed gates |
| `publication_review.disposition` | drafted | `publication_review_recorded` | host-root `publication-report.json` SHA-256 | implemented |
| `publication_review.disposition` | awaiting-review | `publication_review_recorded` | publication report | Issue #101 dependency; current publisher creates draft PRs atomically |
| `publication_review.disposition` | rejected | `publication_review_recorded` | review/ledger receipt | Issue #101 dependency; no current automated rejection writer exists |
| `publication_review.disposition` | terminal-no-publication | `publication_review_recorded` | terminal receipt | Issue #101 dependency; no current automated terminal-no-publication writer exists |
| `publication_review.disposition` | blocked/missing/conflict | `blocked` | service error marker | Issue #101 dependency for reduction semantics |
| `publication_review.disposition` | not-applicable after failed validation/revision path | `validation_recorded` / `revision_recorded` | gate or revision receipt | Issue #101 dependency; current publisher only processes passed gate reports |

Producer evidence failures after committed primary side effects follow one shared policy:
preserve the primary operation, write `state/producer-evidence-errors/<producer>/...json`,
surface the evidence gap there, and do not reclassify or retry the primary action.
