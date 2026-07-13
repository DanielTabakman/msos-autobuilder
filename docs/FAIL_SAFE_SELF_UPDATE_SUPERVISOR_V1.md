# Fail-Safe Self-Update Supervisor V1

## Purpose

Issue #32 adds a Windows updater that is operationally separate from the Autobuilder release it manages. The stable supervisor consumes one explicitly approved exact-commit manifest, stages and tests that commit in a new version directory, switches one active-release pointer, verifies all five managed services, and restores the previous release automatically when the new release does not become healthy.

This chapter does not add product authority, product-main writes, merge authority, autonomous planning, or supervisor self-replacement.

## Installed boundary

The one-time installer creates a separate root:

```text
%USERPROFILE%\.msos-autobuilder-supervisor\
  bootstrap\                 stable scripts and supervisor module
  bootstrap-venv\            stable Python environment
  versions\<commit>\         immutable managed Autobuilder releases
  state\active-release.json  atomic routing pointer
  state\previous-release.json
  state\service-witnesses\   machine-readable post-start witnesses
  reports\                   immutable update and bootstrap reports
  notifications\             immutable founder-attention outbox
  inbox\                      downloaded approved manifest
  logs\                       updater logs
```

The stable bootstrap is copied outside `versions`. A managed release may contain newer supervisor source for review, but the running update transaction cannot replace the bootstrap executing that transaction. Any future bootstrap update therefore requires a separate two-stage handoff; there is no same-transaction supervisor self-update path.

## Managed services

The installer re-registers exactly these five existing tasks behind one stable runner:

1. `MSOS Autobuilder Host`
2. `MSOS Autobuilder Result Relay`
3. `MSOS Autobuilder Candidate Gate`
4. `MSOS Autobuilder Revision Loop`
5. `MSOS Autobuilder Controlled Publisher`

Each runner resolves `state/active-release.json`, verifies the selected release marker, runs the release smoke import, writes a release-bound service witness, and then invokes the service with the selected release's virtual environment.

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

1. creates a fresh staging directory under `versions`;
2. initializes Git and fetches only the approved exact commit;
3. checks out detached `FETCH_HEAD` and verifies `HEAD` equals the requested SHA;
4. verifies required GitHub status/check contexts;
5. verifies expected-file SHA-256 hashes;
6. creates a release-local virtual environment;
7. installs `.[dev]` into that environment;
8. runs Ruff;
9. runs the full pytest suite;
10. runs the managed-entry-point release smoke;
11. on Windows, parses every PowerShell script with the PowerShell AST parser;
12. writes `release.json` and atomically renames staging to `versions/<commit>`.

Any failure before step 12 removes the temporary staging directory. The managed tasks and active pointer remain untouched.

## Cutover and rollback

After staging passes:

1. the current active pointer is preserved as `previous-release.json`;
2. all five explicit managed tasks stop;
3. `active-release.json` is atomically replaced with the staged release pointer;
4. all five tasks restart;
5. the supervisor requires every task to be `Running` and every service witness to be fresh, `running`, and bound to the requested commit;
6. if health passes, the exact commit is recorded as successful in the repeat-safe ledger;
7. if health fails, all five tasks stop, the previous pointer is restored, the tasks restart, and the previous commit must produce a fresh complete health witness;
8. a release that required rollback is ledger-blocked from repeated automatic cutovers.

The stable manual rollback entry point is:

```powershell
& "$env:USERPROFILE\.msos-autobuilder-supervisor\bootstrap\rollback_windows_self_update.ps1"
```

It restores the recorded previous release through the same task-control, pointer, restart, and health-witness boundary.

## Evidence

Every attempt receives a unique immutable JSON report. Reports contain:

- requested release and manifest hash;
- previous and staged release identity;
- completed check argv, result, bounded output, and duration;
- cutover result;
- post-update health evidence;
- rollback evidence;
- terminal outcome and errors.

The ledger binds each cut-over exact commit to one manifest hash, terminal outcome, immutable report ID, and report SHA-256. A separate immutable notification record marks outcomes requiring founder attention. Tokens are read only from the configured environment variable for GitHub API requests and are never written to manifests, argv, reports, fixtures, or notifications.

## Initial bootstrap and acceptance witnesses

The initial installer is the only unavoidable local bootstrap because no external supervisor exists before it runs. It creates the stable environment, clones the current exact commit into the first versioned release, runs installation/Ruff/pytest/PowerShell parser checks, installs the stable wrappers, starts all five services, and writes an initial bootstrap report.

Issue #32 should close only after GitHub CI/review plus these real Windows witnesses exist:

1. initial external-supervisor bootstrap report;
2. one approved exact-commit update with a complete successful health witness and no founder-run cutover commands;
3. one deliberately broken reviewed test release that fails health and automatically restores the previous release;
4. repeat-safe evidence that the rolled-back exact commit is blocked from another automatic cutover.

Issue #33 remains blocked until the rollback witness is accepted.
