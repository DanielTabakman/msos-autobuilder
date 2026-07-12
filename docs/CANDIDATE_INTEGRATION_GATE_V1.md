# Candidate Integration Gate v1

The candidate gate converts a completed, relayed Autobuilder result into a tested,
disposable integration candidate without granting product publication authority.

## Flow

1. Read one immutable result from the dedicated `results` branch.
2. Verify the relayed report, complete patch markers, changed paths, and SHA-256 hashes.
3. Clone the managed product source at the recorded full source commit.
4. Remove the candidate clone's product remote.
5. Apply every patch with `git apply --check --binary` and `git apply --binary`.
6. Verify the resulting changed paths exactly match the union declared by the lanes.
7. Run fixed argv checks with `shell=False`, bounded output, and timeouts.
8. Verify no product commit was created.
9. Remove the disposable candidate workspace.
10. Commit only `gate-report.json` to the non-product `results` branch.

## Safety boundary

The gate does not:

- commit product code;
- retain a product remote in the candidate clone;
- push a product branch;
- open or merge a pull request;
- target `main` or `master` for gate evidence;
- forward GitHub tokens or arbitrary environment variables into product checks.

A repeat-safe local ledger binds each processed job ID to the SHA-256 of its relayed
`report.json`. Changing a result after processing fails closed.

## Windows installation

After the implementation is merged, run from PowerShell in the Autobuilder repository:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File `
  "$HOME\msos-autobuilder\scripts\install_windows_candidate_gate.ps1"
```

The installer runs the existing result once in the foreground and then registers the
`MSOS Autobuilder Candidate Gate` logon task. Logs are written to:

```text
%USERPROFILE%\.msos-autobuilder\logs\candidate-gate.log
```

## First witness

`mcd-boundary-and-frozen-contract-v1` runs:

- the focused Strategy Lab witness;
- frozen-evaluation contract and record tests;
- an executable snapshot-ID integrity witness.

It also carries an explicit policy block for the unresolved frozen-evaluation write-schema
migration. A failed gate report is expected until both snapshot identity enforcement and
schema compatibility are resolved.
