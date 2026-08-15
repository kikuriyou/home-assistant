---
name: harness
description: Run a resumable, evidence-backed software delivery workflow from requirements and specification through planning, implementation, deterministic verification, acceptance verification, and independent semantic review. Use when a user invokes $harness with a task directory or asks Codex to manage a coding task with fresh planner/worker subagents, Claude review, approvals, recovery, and AC-level evidence. Do not use for a simple answer or an isolated edit that does not need the harness workflow.
---

# Agent Harness

Treat the supplied task documents as authoritative. Do not restart settled requirements unless a platform conflict or material unresolved decision blocks delivery.

## Enter the workflow

1. Require a task directory, normally from `$harness tasks/<task-id>`.
2. Read the nearest `AGENTS.md`, `user_requests.md`, `spec.md`, `plan.md`, and `backlog.md`. Never read archived task directories unless explicitly requested.
3. Read [references/workflow.md](references/workflow.md) before starting or resuming a run.
4. Read [references/usage.md](references/usage.md) only for installation, invocation, status, recovery, or troubleshooting guidance.
5. Run the deterministic helper with Python 3.11 or newer for config, state, snapshot, review, verification, and completion checks. Do not recreate those checks in prose or ad hoc shell.

## Enforce preflight

Before modifying project files:

- Require a Git repository, the task directory, and `user_requests.md`.
- Require `.harness/config.toml`; if absent, point to [assets/config.example.toml](assets/config.example.toml) and stop without generating it.
- Record Git HEAD, tracked changes, and untracked files. Preserve all pre-existing user changes.
- Refuse to start if `.harness/runs/` artifacts are tracked or not ignored.
- Stop at `awaiting_input` when a planned write scope overlaps a starting user change.
- Delay runtime/model availability checks until the first phase that needs that runtime.
- Never install, update, reset, stash, checkout, roll back, or access production on the user's behalf.

## Delegate narrowly

- Spawn Codex planner and worker agents directly from the parent with fresh context and no parent-history fork. Pass only the task-local manifest defined in the workflow reference.
- Keep delegation at depth 1. Reject any child request to delegate again.
- Give planners and reviewers read-only access. Give workers only their declared, non-overlapping write scopes.
- Invoke Claude reviewers as new non-persistent, read-only processes. Never resume a reviewer session.
- Do not let reviewers or deterministic verifiers edit artifacts. Route valid findings to the planner or owning worker exactly as defined in the workflow reference.
- Use one reviewer assignment per stage. Do not shop for a passing reviewer.

## Stop safely

Stop and report instead of guessing when:

- fresh, non-persistent, read-only child execution cannot be guaranteed;
- existing user changes overlap the intended write scope;
- required credentials, permissions, downloads, accounts, devices, or E2E tooling are unavailable;
- a relevant baseline already fails and its boundary is unclear;
- an approved requirement conflicts with the platform.

Only mark a run `completed` after the deterministic helper confirms every completion condition and writes `result.md`. Do not implement items from the task backlog.
