# Codex execution contract

This repository uses bounded Codex execution.

Before editing files or running non-trivial commands, read:

`docs/CODEX_EXECUTION_BUDGETS_V1.md`

## Mandatory task envelope

At the start of every implementation task, report:

```text
TASK ENVELOPE
Task class:
Goal:
Authorized tracked paths:
Allowed temporary paths:
Local validation:
CI validation:
Expected duration:
Expected permissions:
Network policy:
Stop conditions:
```

Do not begin implementation until the envelope is coherent with the user request and repository canon.

## Scope and validation

- Edit only explicitly authorized tracked paths.
- Do not silently expand the optimization target, repository, branch, runtime, or acceptance criteria.
- Do not silently upgrade the validation tier.
- Prefer focused local validation; let GitHub CI own broad cross-platform validation unless the task explicitly requires a local full suite.
- A one-file declarative change must not trigger a local full test suite, network-dependent candidate installation, runtime commands, or permission repair unless explicitly required.

## Permission behavior

Before an action likely to trigger a permission prompt, print:

```text
PERMISSION PREVIEW
Action:
Exact command:
Files/directories affected:
Why required:
Consequence of denial:
Recommended choice: Always allow | Allow once | Deny
Risk: routine | elevated | dangerous
```

Batch related routine commands when practical.

One initial repository/session grant is expected. At most one additional unexpected permission prompt is allowed. If another unexpected permission is needed, do not retry through alternate wrappers. Preserve completed work and stop with:

```text
PERMISSION BLOCK
Requested action:
Exact command:
Exact path or resource:
Why necessary:
Why the current sandbox is insufficient:
Safe work already completed:
Consequence of denying:
Recommended permission: allow once | always allow | deny
```

## Hard boundaries

Without explicit authorization in the current task, do not:

- access parent runtime or supervisor directories;
- modify Windows services or Scheduled Tasks;
- change ACLs, ownership, administrator permissions, credentials, or system configuration;
- request unrestricted filesystem or unsandboxed full access;
- use force push, `git reset --hard`, or `git clean -fd`;
- delete branches;
- merge a pull request or write directly to `main`;
- edit the `updates` branch directly;
- install, activate, or roll back a release;
- enable refill, mutate the job feed, or run an installed witness.

## Network and cleanup

- Routine network access is limited to explicitly necessary Git and GitHub operations.
- Do not let tests silently expand into network-dependent package installation.
- Make one ordinary cleanup attempt for task-created temporary files.
- If Windows refuses cleanup, leave the path untracked and unstaged, verify it is absent from the PR, report it, and stop.
- Do not modify permissions merely to remove test debris.

## Coordination

GitHub is the source of truth. Do not silently resolve disagreement between this contract, repository canon, another agent, runtime evidence, or user direction. Return the repository's mandatory `COORDINATION STATUS` block whenever reviewing, continuing, or depending on another agent's work.
