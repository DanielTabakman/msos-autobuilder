# Codex operating contract

GitHub is the source of truth. For coordination, handoffs, disagreement, ownership, or role boundaries, follow PPE `docs/SOP/CHATGPT_GITHUB_CODEX_CONTROL_PLANE_V1.md`.

## Permission behavior

This repository is configured for bounded `workspace-write` execution with `on-request` approval and automatic approval review.

For actions already authorized by the current GitHub task:

- proceed with routine repository reads/writes/tests and nondestructive Git/GitHub work;
- when an authorized action needs sandbox escalation, request it normally so the configured approval reviewer can decide;
- do not stop Daniel merely to translate or approve a routine permission prompt;
- batch related commands when practical and do not retry the same blocked need through alternate wrappers;
- escalate to Daniel only when the requested permission would change real authority/scope, conflicts with canon, or cannot be safely resolved by the approval reviewer.

## Hard stops

Without explicit authority in the current task, do not:

- access credentials, secrets, SSH keys, browser profiles, or unrelated external paths;
- change ACLs, ownership, administrator permissions, or system configuration;
- use destructive Git operations, force-push, delete branches, or write directly to `main`;
- merge a PR unless explicitly authorized;
- mutate installed runtime state, services, Scheduled Tasks, release pointers, refill/feed/publication authority, or product-main state;
- widen product scope, acceptance criteria, or select unchartered work.

## Installed runtime work

An explicitly authorized `installed_witness` or runtime task may name exact runtime/supervisor paths, exact Scheduled Tasks, commands, releases, and evidence paths. Treat only those named resources/actions as in scope. Preserve one writer on shared mutable state and stop at the task's stated verdict boundary.

## Known PPE checkout on the #119 host

PPE is located at:

`C:\Users\USER\probability-prediction-engine`

Do not run a separate locate task for PPE unless fresh evidence shows this path is invalid.
