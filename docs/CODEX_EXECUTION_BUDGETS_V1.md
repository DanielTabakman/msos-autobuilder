# Codex Execution Budgets V1

**Plane:** CONTROL-PLANE  
**Status:** Proposed operating contract  
**Scope:** Codex work performed in `DanielTabakman/msos-autobuilder`  
**Purpose:** Let Codex finish routine repository work without founder babysitting while preserving hard stops around destructive, runtime, release, credential, and machine-level actions.

## 1. Operating objective

The desired behavior is:

> Codex works autonomously inside a clearly bounded repository task, explains unusual permissions in plain English, and stops before dangerous or out-of-scope actions.

The founder should not need to interpret raw PowerShell, Git, filesystem, or sandbox prompts.

This contract separates four layers:

1. **Task envelope** — what this specific task may do.
2. **Project instructions** — behavioral rules in `AGENTS.md`.
3. **Project Codex defaults** — workspace sandbox and auto-review in `.codex/config.toml`.
4. **Command rules** — low-risk inspection conveniences in `.codex/rules/default.rules`.

None of these layers expands product, merge, deployment, credential, or runtime authority.

## 2. Mandatory task envelope

Every Codex implementation request must declare this before work starts:

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

Codex must compare the envelope to the user request, linked issue or PR, and repository canon. If they conflict, Codex stops before editing.

### Required packet fields

Every implementation packet must include:

- `TASK_CLASS`
- `AUTHORIZED_PATHS`
- `LOCAL_VALIDATION`
- `CI_VALIDATION`
- `TIME_BUDGET`
- `PERMISSION_BUDGET`
- `NETWORK_POLICY`
- `TEMP_POLICY`
- `STOP_CONDITIONS`

A task may not silently upgrade its own class, validation tier, permissions, or authority.

## 3. Task classes

### 3.1 `declarative_one_file`

Examples:

- release-request YAML;
- one configuration file;
- one documentation-only correction;
- one deterministic registry or manifest input.

Default local validation:

- parse or schema validation;
- deterministic simulation where applicable;
- focused tests for the parser or publisher;
- expected-path existence checks;
- Ruff when Python tooling is involved;
- `git diff --check`;
- proof that only the authorized file changed.

Default CI validation:

- repository GitHub CI;
- required Linux and Windows checks where configured.

Prohibited by default:

- full local test suite;
- candidate dependency installation;
- test-created network access;
- installed runtime commands;
- supervisor commands;
- ACL, ownership, or permission repair.

Budget:

- expected duration: 20 minutes;
- one initial repository/session grant;
- at most one unexpected permission prompt.

### 3.2 `localized_code`

Examples:

- one module and its focused tests;
- a bounded bug fix in a small path set;
- a deterministic refactor with no runtime mutation.

Default local validation:

- tests covering touched behavior;
- relevant regression tests;
- Ruff for touched paths;
- `git diff --check`;
- targeted type or parser checks where applicable.

Full local suite:

- not automatic;
- permitted only when explicitly named in the task or when focused evidence reveals a credible cross-cutting regression risk;
- Codex must report the reason before expanding.

Budget:

- expected duration: 45 minutes unless specified otherwise;
- one initial repository/session grant;
- at most one unexpected permission prompt.

### 3.3 `cross_cutting_runtime`

Examples:

- coordinated changes across scheduler, relay, gate, revision, publisher, or refill paths;
- crash recovery or durable-state changes;
- multi-service compatibility repairs.

Default local validation:

- focused tests for each affected lifecycle;
- broad integration tests identified in the task;
- full suite only when explicitly required or reasonably necessary;
- no installed-machine mutation unless separately authorized.

Budget:

- expected duration and permission needs must be stated explicitly;
- validation expansion requires an explanation before execution;
- unexpected permission loops remain prohibited.

### 3.4 `installed_witness`

Examples:

- exact-release installation;
- managed cutover;
- Scheduled Task or service witness;
- rollback witness;
- real refill A -> B witness.

Rules:

- this class must be explicitly named;
- installed paths, services, tasks, commands, expected release identity, rollback boundary, evidence paths, and stop conditions must be exact;
- routine repository authorization never implies installed-witness authority;
- founder or reviewed control-plane authorization remains required where canon says so.

## 4. Permission model

### 4.1 Routine repository permission — recommend `Always allow`

A session-scoped or repository-scoped approval is appropriate when the prompt clearly concerns:

- reading or writing within the named repository;
- editing explicitly authorized files;
- running repository-local Python, Pytest, or Ruff commands within the declared validation profile;
- nondestructive Git inspection;
- staging and committing authorized paths to the assigned non-main branch;
- fetching, pushing, or creating the explicitly requested draft PR.

Codex must still obey the task envelope. Repository permission is not permission to edit every tracked file.

### 4.2 Elevated but bounded permission — recommend `Allow once`

Use one-time authorization for:

- a specific necessary Git or GitHub network operation;
- a package installation explicitly required by the task;
- a specifically named external repository read;
- a normal operating-system temporary directory;
- deleting a known task-created temporary directory.

### 4.3 Dangerous or out-of-scope permission — recommend `Deny`

Deny and stop for:

- unrestricted filesystem or unsandboxed full access;
- parent runtime or supervisor directories outside the assigned task;
- credentials, SSH keys, browser profiles, or environment secrets;
- Windows services or Scheduled Tasks outside an explicit installed-witness task;
- ACL, ownership, administrator permission, or system-configuration changes;
- `git reset --hard`;
- `git clean -fd`;
- force pushes;
- branch deletion;
- direct writes to `main`;
- merge actions not explicitly authorized;
- direct `updates`-branch edits;
- release installation, activation, or rollback not explicitly authorized;
- refill enablement, job-feed mutation, or installed witness execution not explicitly authorized.

When the prompt is unclear, the safe default is deny and require Codex to explain.

## 5. Permission preview

Before triggering an action likely to produce a permission dialog, Codex must first print:

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

The explanation must be understandable without shell expertise.

Codex must not trigger a permission prompt it cannot explain clearly.

## 6. Permission interruption budget

One initial repository/session authorization is expected.

After that:

- batch related commands when practical;
- do not ask file by file;
- do not retry the same blocked action through Python, PowerShell, shell wrappers, or alternative tools;
- after the first unexpected permission request, explain it before proceeding;
- after a second unexpected permission request for the same underlying need, stop.

Required stop report:

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

A task that repeatedly interrupts the founder has failed its execution budget even if the code eventually works.

## 7. Validation policy

### 7.1 Focused-first rule

Run the smallest validation set that proves the changed contract.

Validation should expand only when:

- the task explicitly requires it;
- focused tests expose a cross-cutting failure;
- changed code affects a shared lifecycle whose broader tests are identified;
- fresh evidence creates a material compatibility concern.

Before expanding, Codex reports:

```text
VALIDATION EXPANSION
Current task class:
Current validation:
Additional validation proposed:
Evidence requiring expansion:
Expected duration:
Expected permissions/network:
Recommended default:
```

### 7.2 Local versus CI ownership

Local validation owns fast deterministic feedback.

GitHub CI owns:

- the full clean-checkout environment;
- cross-platform Linux and Windows checks;
- broad regression confirmation for small declarative PRs;
- authoritative required status contexts for release control.

A green local full suite does not replace CI. For `declarative_one_file`, CI is the normal broad-suite owner.

### 7.3 One-file release request profile

For a one-file release request, run only:

- request parser;
- exact manifest simulation;
- expected-file existence check;
- supervisor manifest parser;
- focused release-control tests;
- Ruff;
- `git diff --check`;
- one-file diff proof.

Do not run locally by default:

- full Pytest;
- candidate dependency installation;
- network-dependent runtime tests;
- installed service commands;
- refill commands;
- supervisor commands;
- ACL or filesystem-permission repair.

## 8. Network policy

Routine network use is limited to explicitly necessary Git and GitHub operations.

Tests must not silently expand into external package installation or open-ended internet access.

When unexpected network access appears:

1. stop the expanded command;
2. identify the exact host or dependency need;
3. decide whether the task actually requires it;
4. prefer GitHub CI for broad clean-environment validation;
5. request one-time access only when the acceptance contract genuinely depends on local network execution.

Open-ended network authorization is not a normal repository permission.

## 9. Temporary files and cleanup

Use the operating-system temporary directory when possible.

Do not place Pytest temp or cache roots in the repository unless the task explicitly requires reproducible repository-local evidence.

Codex gets one ordinary cleanup attempt.

If Windows refuses cleanup:

- leave the path untracked and unstaged;
- verify it is absent from the PR;
- report the exact path;
- stop.

Do not:

- reset ACLs;
- take ownership;
- request administrator access;
- retry deletion through multiple wrappers;
- spend implementation time debugging disposable test debris.

## 10. Project-level Codex configuration

The repository includes:

`/.codex/config.toml`

with:

```toml
sandbox_mode = "workspace-write"
approvals_reviewer = "auto_review"
```

Intent:

- keep ordinary work inside the repository workspace;
- route escalated permission requests through Codex auto-review where supported;
- continue stopping on higher-risk or insufficiently authorized actions.

Project-local configuration is applied only when the local Codex client trusts the repository. If Codex reports that project configuration is disabled, the user must mark the repository as trusted in their local Codex settings. Trust is a local user decision and must not be committed by the repository.

Existing Codex sessions may need to be restarted after this file changes.

## 11. Command rules

The repository includes:

`/.codex/rules/default.rules`

It allows only low-risk read-only inspection prefixes such as:

- `git status`;
- `git diff`;
- `git log`;
- `git show`;
- `git rev-parse`;
- `git merge-base`;
- selected read-only `gh pr` and `gh issue` operations.

These rules are convenience controls, not the security boundary.

On Windows, PowerShell wrapping and sandbox escape behavior can still produce prompts even when a direct command prefix matches. Therefore:

- do not rely on command rules to authorize writes or deployment actions;
- use workspace sandboxing, auto-review, session approvals, and the task envelope;
- treat unexpected prompting as a client limitation to report, not a reason to widen machine access.

## 12. Founder-facing completion states

Codex should return only when one of these is true:

1. the requested draft PR or bounded deliverable exists;
2. a declared stop condition was reached;
3. a founder decision is genuinely unavoidable;
4. the task was blocked by a permission that cannot safely be approved within the envelope.

Routine progress, expected test duration, and ordinary internal decisions do not require founder interruption.

## 13. Coordination and disagreement

GitHub remains the source of truth.

Codex must not silently resolve disagreement between:

- the current task envelope;
- repository canon;
- another agent;
- CI or runtime evidence;
- founder direction;
- project-level Codex configuration.

When reviewing, continuing, or depending on another agent, return:

```text
COORDINATION STATUS
Agreement: aligned | partial | conflict | unknown
Compared: ...
Disagreement: ...
Evidence gap: ...
Ownership overlap: ...
Risk if unresolved: ...
Recommended default: ...
Founder decision required: yes | no
```

## 14. Adoption checklist

After this contract merges:

1. restart or open a new Codex session in the repository;
2. ensure the repository is marked trusted if the client warns that project config is disabled;
3. confirm the session reports workspace-write rather than unrestricted filesystem access;
4. confirm auto-review is active where the client exposes that setting;
5. run a benign read-only task and verify routine inspection does not interrupt;
6. run a bounded repository edit and grant repository/session write access when the prompt clearly names this repository;
7. confirm runtime, supervisor, service, merge, and release actions still stop for explicit authorization.

## 15. Non-goals

This contract does not:

- grant automatic merge authority;
- grant direct `main` or `updates`-branch write authority;
- grant release, deployment, rollback, or installed-runtime authority;
- grant credential access;
- guarantee that every Codex client version will suppress every Windows permission dialog;
- replace independent PR review or GitHub CI;
- replace the broader ChatGPT + GitHub + Codex control-plane contract.

## 16. External capability basis

This contract uses supported Codex concepts documented by OpenAI:

- workspace sandboxing and approval policy are separate controls;
- `approvals_reviewer = "auto_review"` routes escalated actions through a risk-based reviewer;
- project-local `.codex/config.toml` can supply layered configuration in trusted projects;
- command prefix rules can allow known benign inspection commands;
- managed requirements can constrain allowed sandbox modes, but machine- or organization-level requirements remain outside this repository's authority.

The repository intentionally commits project behavior and safe defaults, not local trust decisions, credentials, or machine-admin requirements.
