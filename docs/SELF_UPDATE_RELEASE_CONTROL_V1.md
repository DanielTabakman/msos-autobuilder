# Self-Update Release Control V1

## Purpose

The fail-safe supervisor consumes only an approved exact-commit manifest from the dedicated `updates` branch. Release control generates that manifest from a small request reviewed and merged through normal GitHub pull-request controls. Founders and operators do not calculate file hashes or edit the live manifest manually.

This layer approves a release identity. It does not deploy code itself, modify the stable supervisor, write product repositories, merge pull requests, or bypass the supervisor's staging and rollback checks.

## Request contract

A release request is committed under:

```text
updates/requests/<release-id>.yaml
```

Example:

```yaml
version: 1
release_id: healthy-update-witness-v1
approved: true
repository: DanielTabakman/msos-autobuilder
repo_url: https://github.com/DanielTabakman/msos-autobuilder.git
commit: self
required_status_contexts:
  - CI
expected_files:
  - pyproject.toml
  - src/msos_autobuilder/self_update_supervisor.py
  - scripts/run_windows_managed_service.ps1
supervisor_update: false
```

The request contains paths, not hashes. It must be explicitly approved and merged to `main`. Exactly one request may be introduced or changed by a release-control merge.

`commit` supports two forms:

- `self` — the exact merge commit containing the reviewed request. This is the normal healthy-release path.
- an exact 40-character lowercase Git SHA — used for a separately reviewed witness commit that must not merge to `main`, such as the deliberately broken rollback witness.

Branches, tags, short SHAs, and moving references are rejected.

## Publication workflow

A main-branch push touching one request triggers `Publish approved update manifest`.

The workflow:

1. checks out the reviewed `main` commit with full history;
2. verifies exactly one request changed;
3. fetches repository branch heads so an explicit reviewed witness SHA is available;
4. resolves the requested exact commit;
5. reads each expected file directly from that Git commit;
6. calculates SHA-256 over the exact Git blob bytes;
7. calculates the canonical `manifest_sha256`;
8. parses the completed manifest through the same supervisor validator used on Windows;
9. writes an immutable archive and the current pointer on the dedicated `updates` branch;
10. pushes without force.

Published layout:

```text
updates/approved/latest.yaml
updates/approved/archive/<request-merge-commit>.yaml
```

The Windows supervisor polls only `latest.yaml`. The archive preserves what GitHub approved at each release-control merge.

## Authority boundary

The publisher workflow has only repository contents write permission. It cannot:

- write issues or pull requests;
- merge or mark reviews ready;
- write product repositories;
- change the running Windows installation;
- create a manifest from an unreviewed request;
- replace the stable supervisor;
- force-push the `updates` branch.

The manifest is still only an eligibility request. The external Windows supervisor independently verifies its canonical hash, repository identity, exact commit, required GitHub checks, expected files, package installation, Ruff, pytest, PowerShell parsing, managed-module imports, cutover health, and rollback.

## Witness sequence

The first real acceptance sequence is deliberately split:

1. merge this release-control infrastructure;
2. prepare but do not yet merge the healthy release request;
3. prepare and review a deliberately broken witness branch, clearly marked not for merge;
4. perform the one-time Windows supervisor bootstrap;
5. merge the healthy request so GitHub publishes its manifest and the host updates automatically;
6. after the healthy witness is accepted, merge a second request naming the exact broken witness SHA;
7. verify automatic rollback, previous-release health, evidence relay, issue notification, and replay blocking.

Issue #32 remains open until those runtime witnesses exist. Issue #33 remains blocked until the rollback witness is accepted.
