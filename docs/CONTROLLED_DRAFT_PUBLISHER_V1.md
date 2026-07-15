# Controlled Draft Publisher V1

## Purpose

The controlled publisher is the only Autobuilder component allowed to write to the product repository. Its authority is deliberately limited to one configured branch, one commit, and one **draft** pull request for a passed candidate.

It has no authority to write product `main`, force-push, mark a PR ready, add an automerge marker, or merge.

## Required evidence

A configured job is publishable only when the `results` branch contains:

- a completed relayed `report.json` with `publication_enabled: false`;
- `result-integrity.json` proving the corrected canonical `report.json` hash and the preserved
  original `source-report.json` hash are distinct evidence roles;
- complete reconstructed patches with matching SHA-256 values and changed-path lists;
- a `gate-report.json` with `status: passed`;
- for state-aware gate reports, `state: candidate_passed`;
- every gate check passed;
- no policy blockers or errors;
- `product_write_performed: false`;
- `workspace_removed: true`;
- a `source_report_sha256` matching the relayed report bytes.

Any mutation, missing field, failed check, unvalidated candidate state, missing integrity,
path overlap, source-report-only evidence, or hash mismatch fails closed.

## Product-main drift protection

Before writing anything, the publisher:

1. fetches current product `main`;
2. proves the candidate source HEAD is an ancestor of current `main`;
3. compares all product changes since the source HEAD with the candidate changed paths;
4. rejects any overlap;
5. applies the complete candidate patch to current `main`;
6. verifies the resulting changed paths exactly match the evidence;
7. runs fixed publication-time checks;
8. verifies the checks created neither extra product changes nor commits;
9. fetches `main` again and refuses publication if it moved during the run.

The first witness source is one docs-only commit behind product `main`, so it is expected to pass the ancestry and non-overlap checks.

## Single-writer contract

The Windows installer:

- stops the managed Autobuilder tasks during cutover;
- stops and disables matching scheduled tasks whose command targets the product repository and an Autobuilder/operator/supervisor;
- stops matching live legacy product-writer processes;
- clears `PPE_GIT_AUTONOMOUS_WRITES` and `PPE_ALLOW_LEGACY_GIT_PUBLISH` from the process and user environment;
- claims `%USERPROFILE%\.msos-autobuilder\state\product-writer-owner.json` for `controlled-draft-publisher-v1`;
- refuses to continue when another owner is recorded;
- uses an operating-system file lock so two controlled publisher processes cannot write concurrently.

The publisher uses the existing Git Credential Manager credential transiently. The credential is not written to configuration, logs, results, or the local ledger.

## Repeat and partial-failure safety

The product commit is deterministic for a fixed product base and gate report. Before pushing, the publisher checks the remote branch:

- absent: push once without force;
- present at the expected commit: continue safely after an interrupted prior run;
- present at another commit: fail closed.

The same rule applies to the PR. There may be exactly one open draft PR for the configured branch, and its head commit and base branch must match. A local immutable ledger verifies the branch and PR on every later cycle.

After success, `publication-report.json` is committed to the `results` branch with the product base, branch, commit, PR URL, hashes, paths, and publication-time check results.

## First witness

Job: `ppe-frozen-evaluation-contract-v1-revision-1`

Expected product branch:

```text
autobuilder/ppe-frozen-evaluation-contract-v1-revision-1
```

Publication-time checks:

- focused frozen-evaluation record/store/review tests;
- snapshot-ID mismatch rejection;
- preservation of `ppe_frozen_eval_v1` as the canonical write version.

Expected outcome: one draft PR titled `PPE: harden frozen evaluation contract` and no revision-2 job.

## Install and start the machine

From PowerShell in the Autobuilder repository:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$HOME\msos-autobuilder\scripts\install_windows_controlled_publisher.ps1"
```

The foreground witness must succeed before the publisher task is registered. The installer then starts:

- `MSOS Autobuilder Host`;
- `MSOS Autobuilder Result Relay`;
- `MSOS Autobuilder Candidate Gate`;
- `MSOS Autobuilder Revision Loop`;
- `MSOS Autobuilder Controlled Publisher`.

## Rollback

Rollback does not merge or delete product work automatically.

1. Stop and disable `MSOS Autobuilder Controlled Publisher`.
2. Keep the two legacy publication environment variables unset.
3. Close the draft product PR and delete its `autobuilder/...` branch only after review confirms that is desired.
4. Remove `product-writer-owner.json` only after every publisher process is stopped.
5. Re-enable a prior writer only through a separate explicit cutover with its own one-writer proof.

The builder, relay, candidate gate, and revision loop can continue operating with the publisher disabled; they remain publication-free.
