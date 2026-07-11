# Windows Codex host v1

This phase connects `msos-autobuilder` to an authenticated Codex CLI on a Windows operator machine without enabling product publication.

## Safety boundary

The host uses three separate locations under `%USERPROFILE%\.msos-autobuilder`:

- `source\msos` — a dedicated clean mirror of MSOS `main`;
- `workspaces\<lane>` — disposable clone-per-lane workspaces;
- `runtime\` — leases and live orchestration state.

The active MSOS development checkout is not used or modified. Codex cannot commit, push, publish, or open pull requests through this phase. Changed paths are checked against the lane contract after execution.

## One-command setup

From a PowerShell window in the `msos-autobuilder` repository:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap_windows_codex_host.ps1 -RunShadow
```

The script:

1. creates `.venv` and installs the repository;
2. creates or refreshes the dedicated MSOS source mirror;
3. discovers Codex using the same path order as the previous operator;
4. verifies `codex login status`;
5. writes host and shadow configuration outside Git;
6. runs preflight;
7. with `-RunShadow`, launches two concurrent read-only Codex review lanes.

If Codex is not authenticated, run:

```powershell
codex login
```

Then rerun the bootstrap command.

## Sandbox policy

The default is `workspace-write`. Dangerous approval and sandbox bypass is never automatic. It requires the explicit bootstrap flag:

```powershell
.\scripts\bootstrap_windows_codex_host.ps1 -RunShadow -DangerousBypass
```

Use that only if the Windows Codex installation cannot start in workspace-write mode and the lane remains disposable and path-scoped.

## Evidence

The generated files live under `%USERPROFILE%\.msos-autobuilder\artifacts`:

- `codex-preflight.json`
- `codex-shadow.json`

The shadow report includes each lane's bounded Codex output and changed paths. The starter manifest requires zero product changes, so any modification fails the run.

## What comes after the first green shadow run

1. Replace the read-only review prompts with two bounded real chapter tasks.
2. Keep `allow_changes: true` only for the intended lane.
3. Run product-owned validation in each clone.
4. Add a reviewable patch bundle.
5. Extract and enable exactly one publisher after the old writer credential is revoked.
