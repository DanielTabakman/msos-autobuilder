# Factory handoff to PPE API and MSOS lanes (read-only coordination)

**Scope:** Documentation/ownership clarification only. This is not a new pipeline, job charter, automatic scheduler, new merge class, release request, or authorization to dispatch, merge, deploy, or install. The existing [Autobuilder operating manual](AUTOBUILDER_OPERATING_MANUAL_V1.md), product contract, approved jobs, exact release supervision and founder decisions remain controlling. For PPE-side execution rules see `DanielTabakman/Probability-prediction-engine/docs/SOP/THREE_TRACK_COORDINATION_HANDOFF_V1.md` (proposed in a separate review PR); until merged, rely on the existing PPE `OPERATING_RULES.md`, `REPO_LAYER_MAP_V1.md` and selected packet.

## Where one task belongs

| Concern | Owner | Receipt |
| --- | --- | --- |
| Product objective, selected frontier, API semantics and MSOS UI acceptance | **PPE canon/selected slice** | Chapter ID, exact source SHA, selected sprint and paths |
| API contract and Qatom public access decisions | **PPE API owner + Daniel/partner** | Documented partner decisions, tests, staging/production conformance and rollback evidence |
| Human UI that consumes API/display output | **PPE MSOS product slice** | Separate branch/slice, user flow witness, no duplicate math |
| Factory capacity, leases, immutable approved job, gate, revision, publisher and terminal evidence | **Autobuilder** | Local runtime facts plus immutable `jobs` / `results` receipts |
| Managed factory release/rollback | **External update supervisor** | Exact installed revision and health/rollback witness, not a merged source SHA |
| Personal prioritization | **Daniel OS** | A non-executable suggestion; its Done button grants no build/merge authority |

API and MSOS both target `DanielTabakman/Probability-prediction-engine`, not separate repositories. For manual desktop work use separate, already-vetted branches/worktrees; the factory itself uses disposable clones and the product packet's exact frozen source. Never share a dirty checkout with another agent or take over an active leased worktree. Path overlap or an unsettled API schema is a **stop/serialize** condition, not a reason to increase worker count.

## Required handoff into the factory

An existing accepted PPE chapter or selected BUILD packet must already contain: chapter and slice ID, objective, authority class, source revision, allowed and forbidden paths/layer, validation and acceptance evidence, dependency IDs and overlap constraints. `build-next` remains the only permitted dispatch adapter for the existing founder portfolio, and refill merely reconciles approved capacity; neither invents the next API or MSOS chapter. Keep at most the currently authorized capacity and single publisher. If the packet is missing, stale, terminal, overlaps a product task, or has a founder decision outstanding, return `BLOCKED`/`UNFILLED`/backpressure as appropriate instead of dispatching.

After a candidate is produced, relay and gate against the pinned source, revalidate at publication, create only the publication artifact permitted by its recorded merge class and installed capabilities, then require exact merge proof before terminal closeout. A product merge does not prove the production API deployment. A source merge in this repository does not prove an installed Windows release. Keep heartbeats, leases, locks and mutable host state outside Git.

**Snapshot to recheck, not a live-status claim (Sep 22, 2026):** generic PPE backlog continuation fix #203 is merged in factory source; verify installed managed release and current refill state before assuming it operates. PPE product order 13 was merged as product PR #5483, but control closeout PR #5485 was still open/non-mergeable on inspection and explicitly holds 14–17 uncataloged. Do not rebuild 13, override the hold, or start a new partner-acceptance job solely because its charter #5484 merged. Use PPE's current selected manifest and fresh VM/operator status when ready.

## Acceptance / stop

The coordination task is complete only when both repos point to the same ID/owner/authority boundary and a desktop status check confirms actual separate checkouts *before* a parallel BUILD starts. This document itself completes the documentation portion only; no machine was altered. Do not merge this documentation over an existing conflicting factory authority change without resolving the discrepancy through the owning issue.
