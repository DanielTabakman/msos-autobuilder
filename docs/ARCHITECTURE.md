# Bootstrap architecture

## Separation boundary

The product repository owns product code, product tests, deployment configuration, product canon, and a versioned Autobuilder project contract.

This repository owns orchestration concepts, lane scheduling, worker interfaces, leases, runtime state, publication policy, and factory tests.

Autobuilder must not import MSOS or PPE business modules. Product-specific facts enter through the contract.

## Parallel lane rule

A lane is the smallest independently owned build stream. It receives a unique lane ID, chapter ID, branch, isolated workspace, layer, allowed paths, forbidden paths, validation commands, lease, and worker assignment.

Two lanes may run concurrently only when their ownership roots are disjoint. Ambiguous wildcard ownership fails closed. An explicit integration lane is required for overlapping changes.

## Worker routing

Backends advertise capabilities, maximum concurrency, timeout, and relative cost class. Chapter semantics stay provider-neutral. The first backend is plan-only and performs no command execution or writes.

## Publication

Publication is disabled during bootstrap. Later, execution workers may be parallel while a separate publisher enforces exactly one writer, one branch per lane, and at most one open PR per chapter.
