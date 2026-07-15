# Candidate Integration Gate v1

The candidate gate converts a completed, relayed Autobuilder result into a tested,
disposable integration candidate without granting product publication authority.

## Flow

1. Read one immutable result from the dedicated `results` branch.
2. Verify the relay-corrected canonical `report.json`, complete patch markers, changed paths,
   `result-integrity.json`, and SHA-256 hashes. `source-report.json` is preserved as original
   worker evidence only and is noncanonical for downstream patch identity.
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

## Generic build-next discovery

Founder `build-next-*` jobs are discovered without a per-job installed plan when their
immutable `job.yaml` contains a valid `candidate_validation` contract. The contract binds
the job ID, work item, native slice, target repository, exact source commit, allowed changed
paths, bootstrap commands, required checks, timeouts, and zero publication/merge/main-write
authority. Missing, malformed, mutated, stale, or unsafe contracts produce an explicit
`unvalidated` gate report and cannot publish.

The PPE adapter preset bootstraps from the candidate repository itself with fixed argv
(`python -m pip install -e .`) and then runs fixed repository validation (`python -m pytest -q`).
Autobuilder does not hardcode PPE dependency names, USO tests, job IDs, or path-to-test maps.

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
