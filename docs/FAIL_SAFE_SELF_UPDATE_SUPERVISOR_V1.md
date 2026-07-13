# Fail-Safe Self-Update Supervisor V1

## Purpose

Issue #32 adds a Windows updater that is operationally separate from the Autobuilder release it manages. The stable supervisor consumes one explicitly approved exact-commit manifest, stages and tests that commit in a new version directory, switches one active-release pointer, verifies all five managed services, and restores the previous release automatically when the new release does not become healthy.

This chapter does not add product authority, product-main writes, merge authority, autonomous planning, or supervisor self-replacement.

## Installed boundary

The one-time installer creates a separate root:

```text
%USERPROFILE%\.msos-autobuilder-supervisor\
  bootstrap\                 stable scripts, probe, supervisor, and evidence relay
  bootstrap-venv\            stable Python environment
  versions\<commit>\         immutable managed Autobuilder releases
  state\active-release.json  atomic routing pointer
  state\previous-release.json
  state\service-witnesses\   machine-readable post-start witnesses
  reports\                   immutable update and bootstrap reports
  notifications\             immutable founder-attention outbox
  inbox\                     downloaded approved manifest
  logs\                      updater logs
```

The stable bootstrap is copied outside `versions`. A managed release may contain newer supervisor source for review, but the running update transaction cannot replace the bootstrap executing that transaction. Any future bootstrap update therefore requires a separate two-stage handoff; there is no same-transaction supervisor self-update path.

## Managed services

The installer re-registers exactly these five existing tasks behind one stable runner:

1. `MSOS Autobuilder Host`
2. `MSOS Autobuilder Result Relay`
3. `MSOS Autobuilder Candidate Gate`
4. `MSOS Autobuilder Revision Loop`
5. `MSOS Autobuilder Controlled Publisher`

Each runner resolves `state/active-release.json`, verifies the selected release marker, runs the external stable health probe with the selected release's Python, writes a release-bound service witness, and then invokes the service. The probe requires every managed module to import from inside the selected exact release.

The task-control helper accepts only the explicit configured task-name list. It cannot discover, register, unregister, or modify unrelated scheduled tasks.

## Approved manifest contract

An update manifest is valid only when all of the following hold:

- `version` is `1`;
- `approved` is exactly `true`;
- `repository` and `repo_url` match the stable local supervisor configuration;
- `repo_url` contains no embedded credentials;
- `commit` is one exact 40-character lowercase Git SHA;
- every required GitHub status/check context is successful, neutral, or skipped on that exact commit;
- `expected_files` contains safe relative paths and SHA-256 hashes, including `pyproject.toml` and the managed supervisor source anchor;
- `manifest_sha256` matches the canonical JSON representation of the manifest excluding the hash field itself;
- `supervisor_update` is exactly `false`.

`config/update_manifest.example.yaml` is structurally valid but intentionally contains placeholder commit and file hashes. An approved manifest must be created from the reviewed merge commit, with actual file hashes and the recomputed canonical manifest hash, on the protected update-manifest branch.

## Staging sequence

Before any live task stops, the external supervisor:

1. creates an incomplete exact-commit directory at `versions/<commit>`;
2. initializes Git and fetches only the approved exact commit;
3. checks out detached `FETCH_HEAD` and verifies `HEAD` equals the requested SHA;
4. verifies expected-file SHA-256 hashes;
5. creates a release-local virtual environment at its permanent path;
6. installs `.[dev]` into that environment;
7. runs Ruff;
8. runs the full pytest suite;
9. runs the external stable managed-entry-point health probe;
10. on Windows, parses every PowerShell script with the PowerShell AST parser;
11. re-verifies required GitHub status/check contexts immediately before eligibility;
12. atomically writes `release.json` as the completion marker inside `versions/<commit>`.

The version environment is built at its final exact-commit path so virtualenv and editable-install paths never point at a renamed staging directory. Until the completion marker exists, the directory is incomplete and cannot be activated. Any failure before step 12 removes it. The managed tasks and active pointer remain untouched.

## Cutover and rollback

After staging passes:

1. the current active pointer is preserved as `previous-release.json`;
2. all five explicit managed tasks stop;
3. `active-release.json` is atomically replaced with the staged release pointer;
4. all five tasks restart;
5. the supervisor requires every task to be `Running` and every service witness to be fresh, `running`, bound to the requested commit, and continuously healthy through the configured stability window;
6. if health passes, the exact commit is recorded as successful in the repeat-safe ledger;
7. if health fails, all five tasks stop, the previous pointer is restored, the tasks restart, and the previous commit must produce a fresh complete health witness;
8. a release that required rollback is ledger-blocked from repeated automatic cutovers.

The stable manual rollback entry point is:

```powershell
& "$env:USERPROFILE\.msos-autobuilder-supervisor\bootstrap\rollback_windows_self_update.ps1"
```

It restores the recorded previous release through the same task-control, pointer, restart, and health-witness boundary.

## Evidence and founder notification

Every attempt receives a unique immutable JSON report. Reports contain:

- requested release and manifest hash;
- previous and staged release identity;
- completed check argv, result, bounded output, and duration;
- cutover result;
- post-update health evidence;
- rollback evidence;
- terminal outcome and errors.

The ledger binds each cut-over exact commit to one manifest hash, terminal outcome, immutable report ID, and report SHA-256. A separate immutable notification record marks outcomes requiring founder attention. Tokens are read only from the configured environment variable for GitHub API requests and are never written to manifests, argv, reports, fixtures, or notifications.

The stable bootstrap includes a separate evidence relay. It scans the durable local notification outbox, verifies that each notification points to a report inside the immutable reports root, binds both source files by SHA-256, and pushes them without force to:

```text
results/<machine-id>/self-updates/<attempt-id>/
  update-report.json
  notification.json
  relay.json
```

The relay can target only a configured non-default branch. It retries non-fast-forward races with other results writers, refuses path replacement, and records the resulting Git commit in a local repeat-safe relay ledger. It runs before manifest deduplication and after every update attempt, so a temporary network or Git failure cannot make a completed update invisible.

A scheduled workflow stored on protected `main` checks out the `results` branch as data, scans relayed notification records, and comments issue #32 using the repository-scoped GitHub Actions token. Comment markers are derived from the requested commit or manifest hash plus terminal outcome, so repeated polling cannot create duplicate founder notifications. Executable notifier code is loaded from `main`, never from the host-writable results branch.

The polling wrapper records the downloaded manifest file hash only after both the update transaction and evidence relay succeed. Unchanged successful manifests therefore stop producing evidence, while transient update or relay failures remain retryable.

## Initial bootstrap and acceptance witnesses

The initial installer is the only unavoidable local bootstrap because no external supervisor exists before it runs. It creates the stable environment, clones the current exact commit into the first versioned release, runs installation/Ruff/pytest/PowerShell parser checks, installs the stable wrappers, starts all five services, writes an initial bootstrap report and notification, and attempts the first automatic results-branch relay. If that first Git push is temporarily unavailable, the installed updater retries the durable local evidence automatically.

Issue #32 should close only after GitHub CI/review plus these real Windows witnesses exist:

1. initial external-supervisor bootstrap report relayed to GitHub;
2. one approved exact-commit update with a complete successful health witness and no founder-run cutover commands;
3. one deliberately broken reviewed test release that fails health and automatically restores the previous release;
4. repeat-safe evidence that the rolled-back exact commit is blocked from another automatic cutover;
5. issue #32 receives the deduplicated GitHub evidence notifications without a founder copying local files.

Issue #33 remains blocked until the rollback witness is accepted.
