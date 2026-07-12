# MSOS Autobuilder

Open-source build-factory infrastructure for Market Structure OS (MSOS).

## Current status

This repository is in **extraction/persistent-host** mode. It can operate on disposable isolated MSOS clones and run an approval-gated local Codex host, while production publication remains disabled.

It may:

- load and validate a product contract;
- plan and run isolated parallel build lanes;
- enforce path ownership and reject overlap;
- route work by capabilities, concurrency, and relative cost;
- keep leases and runtime state outside product Git;
- run fixed local worker processes inside disposable clones;
- connect to an authenticated Codex CLI for local lanes;
- run as a persistent Windows logon task;
- import immutable approved jobs from a read-only Git manifest feed;
- archive reports and disposable workspace patches;
- collect bounded evidence while preserving a clean MSOS source mirror.

It must not yet:

- commit or push to MSOS;
- open or merge pull requests;
- hold GitHub product-write credentials;
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

### One-shot Windows Codex shadow host

The earlier foreground witness remains available:

```powershell
.\scripts\bootstrap_windows_codex_host_auto.ps1 -RunShadow
```

See [`docs/WINDOWS_CODEX_HOST_V1.md`](docs/WINDOWS_CODEX_HOST_V1.md) for the one-shot host layout and evidence.

## Repository shape

```text
src/msos_autobuilder/
  backends/           worker-provider interfaces and local/Codex backends
  codex_shadow.py     host config, preflight, manifest loading, and shadow execution
  persistent_host.py approval queue, Git feed, heartbeat, recovery, and archives
  contracts.py        product contract loading and validation
  lanes.py            lane ownership and concurrency checks
  leases.py           runtime leases outside product Git
  models.py           task, lane, lease, and capability models
  scheduler.py        parallel scheduling and heartbeat renewal
config/                public synthetic examples and deterministic rules
scripts/               operator bootstrap and Windows service tools
tests/                 factory-only tests
```

The parent extraction chapter is tracked in `DanielTabakman/Probability-prediction-engine#5348`.
