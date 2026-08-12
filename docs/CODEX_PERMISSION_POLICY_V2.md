# Codex Permission Friction V2

**Status:** Proposed control-plane policy

## Goal

Reduce founder interruption from routine Codex permission prompts while preserving bounded repository execution and explicit stops for dangerous or authority-changing actions.

## Current mechanism

- project Codex config uses `workspace-write`;
- approval policy is `on-request`;
- eligible approval requests are routed to the configured automatic reviewer;
- low-risk read-only Git/GitHub inspection commands are allow-ruled;
- repository instructions require routine authorized work to continue without asking Daniel to translate or approve ordinary prompts.

## Boundaries

This policy does not authorize:

- credentials or unrelated external filesystem access;
- ACL, administrator, or system-configuration changes;
- destructive Git or direct writes to `main`;
- PR merge unless explicitly authorized;
- installed runtime, service, Scheduled Task, release, refill, feed, publisher, or product-main mutation unless the current task explicitly names that authority;
- autonomous product chartering or unchartered work selection.

## Known PPE checkout

For the current #119 host, PPE is at:

`C:\Users\USER\probability-prediction-engine`

A separate locate task is unnecessary unless fresh evidence invalidates that path.
