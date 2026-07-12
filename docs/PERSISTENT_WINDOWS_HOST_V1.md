# Persistent Windows Host v1

The persistent host turns the proven one-shot Codex shadow runner into an always-on local Autobuilder process.

## What it does

- starts at Windows logon through Task Scheduler;
- keeps one host process alive at a time;
- polls a read-only Git manifest feed for approved jobs;
- copies approved jobs into a local atomic queue;
- launches Codex in disposable, path-scoped MSOS clones;
- records heartbeat, status, reports, and optional patches;
- recovers interrupted jobs into a failed archive instead of silently rerunning them.

## What it does not do

- commit product changes;
- push branches;
- open or merge pull requests;
- hold a GitHub product-write token;
- enable publication;
- run `dangerous-bypass`.

`publication_enabled: false` is required in the service config, host job, and embedded Codex manifest.

## Install on Windows

From the cloned `msos-autobuilder` repository:

```powershell
Set-ExecutionPolicy -Scope Process Bypass -Force
.\scripts\install_windows_persistent_host.ps1
```

The installer is idempotent. It:

1. prepares Python, the venv, Codex authentication, and the clean MSOS source mirror;
2. writes `~/.msos-autobuilder/service.yaml`;
3. initializes the queue and status files;
4. creates a hidden Task Scheduler entry named `MSOS Autobuilder Host`;
5. starts the task immediately.

## Runtime layout

```text
~/.msos-autobuilder/
  host.yaml
  service.yaml
  logs/
    persistent-host.log
  queue/
    pending/
    running/
    completed/<job-id>/
      job.yaml
      report.json
      patches/*.patch
    failed/<job-id>/
      job.yaml
      error.json
  state/
    host-status.json
    host.lock
    feed-seen.json
    feed-repo/
```

Claims use an atomic rename from `pending` to `running`. A second live host process fails closed. If the host stops during a job, the next start archives that job as interrupted rather than automatically repeating it.

## Git manifest feed

The default installer watches:

```text
repository: DanielTabakman/msos-autobuilder
branch: jobs
path: jobs/approved
```

The checkout is read-only and requires no GitHub write credential. Each immutable job ID is imported once. Reusing the same ID with different content creates a feed-conflict record rather than executing the replacement.

Example job:

```yaml
version: 1
job_id: strategy-lab-clarity-v1
approved: true
publication_enabled: false
requested_by: Daniel
submitted_at: 2026-07-12T00:00:00Z
manifest:
  version: 1
  publication_enabled: false
  lanes:
    - task_id: strategy-lab-clarity
      lane_id: msos-web-clarity
      chapter_id: STRATEGY-LAB-CLARITY-V1
      branch: autobuilder/strategy-lab-clarity-v1
      layer: msos-shell
      allowed_paths:
        - apps/msos-web/**
      forbidden_paths:
        - artifacts/**
        - docs/SOP/**
      allow_changes: true
      instruction: |
        Make one bounded Strategy Lab clarity improvement.
        Do not commit, push, publish, or open a pull request.
```

Remote feed jobs must use inline instructions. `prompt_file` is rejected.

## Useful commands

```powershell
# Current state
.\.venv\Scripts\python.exe -m msos_autobuilder host-status `
  --service-config "$HOME\.msos-autobuilder\service.yaml"

# Process one approved job in the foreground
.\.venv\Scripts\python.exe -m msos_autobuilder host-run-once `
  --service-config "$HOME\.msos-autobuilder\service.yaml"

# Request a graceful stop
.\.venv\Scripts\python.exe -m msos_autobuilder host-stop `
  --service-config "$HOME\.msos-autobuilder\service.yaml"
```

## Uninstall

```powershell
.\scripts\uninstall_windows_persistent_host.ps1
```

Runtime evidence is preserved by default. Pass `-RemoveRuntimeData` only when permanent deletion is intended.
