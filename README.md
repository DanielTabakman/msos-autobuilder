# MSOS Autobuilder

Open-source build-factory infrastructure for Market Structure OS (MSOS).

## Current status

This repository is in **controlled draft-publication** mode. It can run approved Codex jobs in disposable product clones, relay complete patches, gate candidates, generate bounded revisions, and publish configured passing candidates as review-only draft product pull requests.

For the current operating model, founder boundary, and idea-to-PR flow, see [`docs/AUTOBUILDER_OPERATING_MANUAL_V1.md`](docs/AUTOBUILDER_OPERATING_MANUAL_V1.md). For the current chapter closeout and exact next Codex objective, see [`docs/CURRENT_STATE_AND_CODEX_NEXT_2026-07-13.md`](docs/CURRENT_STATE_AND_CODEX_NEXT_2026-07-13.md).

It may:

- load and validate a product contract;
- plan and run isolated parallel build lanes;
- enforce path ownership and reject overlap;
- route work by capabilities, concurrency, and relative cost;
- keep leases and runtime state outside product Git;
- run fixed local worker processes inside disposable clones;
- connect to an authenticated Codex CLI for local lanes;
- run as persistent Windows logon tasks;
- import immutable approved jobs from a read-only Git manifest feed;
- archive reports and disposable workspace patches;
- relay complete review artifacts to a dedicated results branch;
- apply relayed patches in disposable candidate clones and run deterministic checks;
- turn failed candidate reports into bounded correction jobs;
- create one configured product branch, one commit, and one **draft** pull request after a passing gate and a second publication-time validation.

It must not:

- write the product `main` branch;
- force-push product branches;
- mark draft pull requests ready for review;
- add an automerge marker;
- merge product pull requests;
- use GitHub for leases, heartbeats, or mutable runtime state;
- run concurrently as a second production publisher.

## Safety invariants

1. Parallel workers are allowed; overlapping path ownership is not.
2. Multiple builders are allowed; only one production publisher may be enabled.
3. Runtime queue, lease, worker, and operator state stays outside product Git repositories.
4. Product behavior is described through a versioned project contract, not imports from MSOS/PPE business modules.
5. Public fixtures and examples contain synthetic values only.
6. Codex uses `workspace-write` by default; dangerous sandbox bypass requires explicit operator opt-in.
7. Worker commits are forbidden before the single-publisher phase.
8. Persistent jobs require `approved: true` and `publication_enabled: false`.
9. Job IDs are immutable; content replacement fails closed.
10. Candidate gates remove product remotes before checks and publish evidence only to the results branch.
11. The controlled publisher accepts only passed immutable evidence, revalidates on current product `main`, pushes without force, creates a draft PR, and has no merge or `main` authority.
12. Managed host updates use an external stable supervisor, exact commits, versioned releases, one atomic active pointer, health witnesses, and automatic rollback.

## Bootstrap

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
```

### Persistent Windows host

From PowerShell in this repository:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\scripts\install_windows_persistent_host.ps1
```

The installer prepares the existing Codex host, writes the persistent service config, registers a hidden Windows logon task, and starts it immediately.

See [`docs/PERSISTENT_WINDOWS_HOST_V1.md`](docs/PERSISTENT_WINDOWS_HOST_V1.md) for the queue, approved Git feed, evidence layout, and uninstall process.

### Review-only result relay

```powershell
.\scripts\install_windows_results_relay.ps1
```

The relay reconstructs complete patches, including newly created files, and sends immutable review evidence to the dedicated `results` branch.

### Disposable candidate integration gate

```powershell
.\scripts\install_windows_candidate_gate.ps1
```

The gate applies relayed patches to a fresh clone pinned to the recorded source commit, runs fixed validation commands, removes the clone, and writes a structured gate report to the `results` branch. Codex and gate publication remain disabled.

See [`docs/CANDIDATE_INTEGRATION_GATE_V1.md`](docs/CANDIDATE_INTEGRATION_GATE_V1.md) for the gate contract and first witness.

### Automatic revision pipeline

```powershell
.\scripts\install_windows_revision_pipeline.ps1
```

The revision pipeline turns configured failed gate reports into bounded approved correction jobs and gates the resulting revision candidates automatically.

### Controlled draft product publisher

```powershell
.\scripts\install_windows_controlled_publisher.ps1
```

The cutover installer disables matching legacy in-product writer processes/tasks, clears the legacy write environment variables, verifies a single writer-owner marker, publishes the configured passing witness as one draft product PR, installs the persistent publisher task, and restarts the full Autobuilder task set. Merge and product-`main` writes remain disabled.

See [`docs/CONTROLLED_DRAFT_PUBLISHER_V1.md`](docs/CONTROLLED_DRAFT_PUBLISHER_V1.md) for evidence requirements, drift protection, rollback, and the first witness.

### Fail-safe host self-update supervisor

The one-time bootstrap installs the update supervisor outside every managed Autobuilder release:

```powershell
.\scripts\install_windows_self_update_supervisor.ps1
```

Future updates consume an explicitly approved exact-commit manifest, install and test into a new version directory, switch one atomic active-release pointer, require fresh health witnesses from all five services, and automatically restore the previous release when health fails. The managed release cannot replace the supervisor executing the same transaction.

See [`docs/FAIL_SAFE_SELF_UPDATE_SUPERVISOR_V1.md`](docs/FAIL_SAFE_SELF_UPDATE_SUPERVISOR_V1.md) for the manifest contract, version layout, rollback boundary, evidence, and required real Windows acceptance witnesses.

### One-shot Windows Codex shadow host

The earlier foreground witness remains available:

```powershell
.\scripts\bootstrap_windows_codex_host_auto.ps1 -RunShadow
```

See [`docs/WINDOWS_CODEX_HOST_V1.md`](docs/WINDOWS_CODEX_HOST_V1.md) for the one-shot host layout and evidence.

## Repository shape

```text
src/msos_autobuilder/
  backends/                 worker-provider interfaces and local/Codex backends
  candidate_gate.py         disposable patch integration and validation
  controlled_publisher.py   passed-gate verification and draft-only product publication
  codex_shadow.py           host config, preflight, manifest loading, and shadow execution
  persistent_host.py        approval queue, Git feed, heartbeat, recovery, and archives
  results_relay.py          complete review-artifact reconstruction and relay
  revision_loop.py          failed-gate to bounded correction-job conversion
  self_update_supervisor.py exact-commit release staging, cutover, health, and rollback
  contracts.py              product contract loading and validation
  lanes.py                  lane ownership and concurrency checks
  leases.py                 runtime leases outside product Git
  models.py                 task, lane, lease, and capability models
  scheduler.py              parallel scheduling and heartbeat renewal
config/                      public synthetic examples and deterministic rules
scripts/                     operator bootstrap and Windows service tools
tests/                       factory-only tests
```

The parent extraction chapter is tracked in `DanielTabakman/Probability-prediction-engine#5348`.
