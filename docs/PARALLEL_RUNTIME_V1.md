# Parallel lane runtime v1

This runtime prepares multiple independent build lanes without giving the factory product publication authority.

## Safety boundaries

- Lease and worker state live under an operator-supplied runtime root outside the product checkout.
- Each lane has a unique branch, ownership surface, and deterministic workspace path.
- Workspace and runtime roots must remain outside the product checkout and separate from each other.
- Overlapping path ownership fails before any backend executes.
- All lane leases are acquired before execution and released after completion or failure.
- Backends are selected by required capabilities and relative cost class, not by provider-specific chapter semantics.
- Publication remains disabled in this phase.

## Runtime sequence

1. Validate unique task IDs, branches, lane IDs, and disjoint ownership.
2. Select the cheapest compatible backend for each task.
3. Validate each backend's proposed workspace against the lane workspace policy.
4. Atomically acquire one runtime lease per lane.
5. Execute lanes concurrently while respecting each backend's concurrency limit.
6. Return evidence in task order and release all acquired leases.

## Current witness

The CI witness runs an MSOS web lane and a PPE core lane through a two-party synchronization barrier. The test can pass only when both lanes enter execution concurrently. A separate witness proves overlapping PPE engine lanes are rejected before backend execution.

## Deferred

- creating live product Git worktrees;
- executing coding agents or arbitrary product commands;
- remote compute credentials;
- product commits, pushes, pull requests, or merges;
- publisher cutover.
