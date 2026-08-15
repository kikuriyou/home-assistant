# Harness workflow

## Contents

1. [Responsibilities](#responsibilities)
2. [Run entry and preflight](#run-entry-and-preflight)
3. [Artifacts and manifests](#artifacts-and-manifests)
4. [Phases and statuses](#phases-and-statuses)
5. [Specification and planning](#specification-and-planning)
6. [Child execution](#child-execution)
7. [Review and routing](#review-and-routing)
8. [Verification and scope safety](#verification-and-scope-safety)
9. [Recovery and invalidation](#recovery-and-invalidation)
10. [Completion](#completion)

## Responsibilities

Use only these four harness roles:

| Role | Responsibility | Writes project files |
| --- | --- | --- |
| planner | Clarify requirements, return specification/plan content, map ACs to evidence | Never directly; parent writes returned task documents atomically |
| worker | Implement one task inside an exclusive write scope and add its smallest relevant tests | Declared scope only |
| deterministic verifier | Run selected commands and record exact evidence | Run artifacts only |
| semantic reviewer | Find current requirement, contract, scope, and verification defects | Never |

Keep orchestration in the parent Codex; do not configure an orchestrator role. Keep deterministic verification in the parent and helper script; do not assign it to an LLM.

## Run entry and preflight

For `$harness tasks/<task-id>`:

1. Resolve the task path inside the Git repository and require `user_requests.md`.
2. Load and strictly validate `.harness/config.toml`.
3. Inspect non-terminal runs for the same task. Resume the newest safe run; present pending input or approval when applicable. Ask before starting after a terminal run.
4. Create `.harness/runs/.gitignore` with exactly `*` if missing. Refuse tracked or unignored run artifacts.
5. Snapshot the effective config, review schema, task inputs, project rules, Git HEAD, tracked diff, and untracked manifest.
6. Compare starting changes with every planned write scope. Move to `awaiting_input` on overlap without editing, reset, stash, checkout, or rollback.
7. Acquire the run file lock before mutation and hold it for the parent operation.

Create run directories and files with owner-only permissions. Never log the full environment or inspect credentials without a task-specific need.

## Artifacts and manifests

Store each run under `.harness/runs/<run-id>/`:

```text
state.json
snapshots/
  config.toml
  review.schema.json
  source.json
  inputs/<invocation-id>.json
  prompts/<invocation-id>.txt
agents/<invocation-id>/
  request.json
  stdout.txt
  stderr.txt
  result.json
verification/
  <verification-id>.json
  e2e/
```

Each child input manifest must contain only required paths or excerpts plus:

- invocation ID, role, assignment, runtime, model, effort, attempt;
- agent ID, parent agent ID, and depth;
- objective, target ACs, read paths, write scope, dependencies;
- required output format and the exact evidence to return.

Record `depth = 1` and reject larger values. Do not persist conversation transcripts or chain-of-thought.

## Phases and statuses

Advance normally in this order:

```text
spec -> spec_review -> plan -> plan_review -> baseline_verification
-> implementation -> deterministic_verification -> acceptance_verification
-> implementation_review -> completed
```

Skip `baseline_verification` only for a genuinely new system or when no relevant baseline exists. Track `implementation_plan.md` as an approval artifact, not a permanent phase.

Allow only these statuses: `running`, `awaiting_input`, `awaiting_approval`, `completed`, `failed`, `blocked`, `aborted`.

- Use `awaiting_input` for a material user decision or ambiguous external change.
- Use `awaiting_approval` only for an artifact configured as `required`.
- Use `failed` after allowed quality correction is exhausted.
- Use `blocked` for environment, authentication, permission, infrastructure, unknown diff, or relevant baseline failure.
- Use `aborted` only after processing an explicit user stop request.
- Treat all terminal statuses as immutable.

## Specification and planning

Spawn a fresh planner with the user request, relevant rules, necessary current code/spec/tests, and required output only.

For specification dialogue, require:

- a provisional discussion map grouped as `must-decide`, `recommended`, and `no-confirmation`;
- one material question at a time by default;
- recommendation, alternatives, trade-offs, known constraints, backlog candidates, and progress;
- support for “adopt recommendations”, “list remaining”, category priority, pause, and backlog deferral.

Persist only decisions, reasons, constraints, remaining items, and the current pending input. Do not copy the full conversation. For an existing system, require current behavior, desired delta, preserved behavior, evidence, compatibility constraints, new ACs, and regression ACs. Identify the smallest relevant baseline before implementation.

When no material question remains, produce a spec with purpose, non-goals, behavior, errors, interruption/recovery, constraints, testable ACs, and backlog boundary.

Map every AC in the plan to:

- an implementation or verification task;
- an exclusive write scope and dependencies;
- test level, exact command, environment, and expected evidence.

Place this machine-checked table in every final plan:

~~~text
| AC | Task | Write scope | Dependencies | Test level | Command | Environment | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AC-01 | ... | ... | none | unit | ... | local | ... |
~~~

Populate every cell. Use none when a write scope or dependency is genuinely absent.

Order shared contracts first and integration last. Serialize overlapping scopes. Create `implementation_plan.md` only when multiple substantial gates need additional sequencing.

Apply each configured approval independently. Hash the approved artifact; a later content change invalidates its approval and all dependent evidence.

## Child execution

### Codex planner and worker

Use the parent's native subagent tool directly. Set `fork_turns` to `none`, choose the resolved assignment model and effort, and place only the saved task-local manifest in the task message. Do not invoke `codex exec` from the Python helper for the MVP.

Start workers concurrently only when their write scopes do not overlap. Record each native agent ID. After completion, compare the Git changes with that worker's scope before accepting its output.

### Claude semantic reviewer

At the first Claude stage, inspect `claude --version` and `claude --help`. Block if the installed CLI cannot provide all of:

- print/non-interactive execution;
- `--no-session-persistence`;
- selected model and effort;
- structured output using the canonical schema;
- read-only tools and permissions;
- auto-memory disabled while CLAUDE.md and explicitly supplied skills, plugins, and MCP remain usable.

Launch a new process with argument arrays, not a shell. Pass the prompt on stdin. Set CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 and CLAUDE_CODE_SKIP_PROMPT_HISTORY=1 without recording the inherited environment. Use print mode, no-session-persistence, the selected model/effort, plan permission mode, only Read/Glob/Grep tools, JSON output/schema, and an inline settings value that disables hooks and auto-memory. Add explicit extra directories, plugins, or MCP configs only when the invocation manifest requires them. Do not use bare mode, because it also disables CLAUDE.md discovery and keychain/OAuth authentication. Never pass secrets in arguments or environment snapshots.

Apply the configured timeout. Record sanitized arguments, stdin prompt path, stdout, stderr, exit code, duration, and classification. Do not retry authentication, permission, timeout, CLI absence, or infrastructure failures as quality failures.

## Review and routing

Validate reviewer output against [../assets/review.schema.json](../assets/review.schema.json). Also require unique finding IDs and non-empty basis, evidence, current risk, and minimal fix.

Reject `low`, preferences, generic future extensibility, and claims without current evidence. Fail a review only when at least one valid `critical` or `high` finding remains. Report `medium` findings while allowing the stage to pass.

Retry a protocol-invalid reviewer response once with identical inputs. Do not resume its session.

Route corrections exactly as follows:

| Detection stage | Corrector | Return phase | Required fresh recheck |
| --- | --- | --- | --- |
| `spec_review` | planner assignment | `spec` | same `spec_review` assignment |
| `plan_review` | planner assignment | `plan` | same `plan_review` assignment |
| `deterministic_verification` | worker owning failed scope | `implementation` | deterministic verification onward |
| `acceptance_verification` | worker owning failed scope | `implementation` | deterministic and acceptance verification onward |
| `implementation_review` | worker owning finding scope | `implementation` | both verifications, then same implementation review assignment |

Never ask the reviewer or verifier to fix artifacts. If a finding proves an upstream artifact wrong, return to the earliest affected phase and invalidate downstream evidence. Ask for user input before correction when a new material decision is required.

Permit at most three quality correction cycles per review stage and one worker escalation for an unresolved implementation problem. After correction, use a separate fresh reviewer invocation to confirm old findings and perform the independent stage review. Do not change reviewer assignments to seek a pass.

## Verification and scope safety

Select existing verification commands in this order:

1. project `AGENTS.md` rules;
2. CI configuration;
3. existing scripts or task runner;
4. approved plan.

Run the smallest relevant formatter check, lint, type check, and unit/integration tests. A required command that cannot run is `blocked`, never passed.

Record a relevant baseline before changing an existing system. Block on a related existing failure or an untrustworthy starting state. Record an unrelated known failure only after the user decides how to handle it.

For user-visible behavior, run at least one automated acceptance path at the project's highest useful boundary. Use an existing mechanism first. If a tool, download, account, device, or environment decision is needed, ask before adding or using it. Never substitute an unrecorded manual check for E2E. Require explicit approval for an unavoidable automation exception.

Bind deterministic, acceptance, and implementation-review evidence to a `source_state_hash` made from Git HEAD, tracked diff, and untracked manifest. Any project-file change invalidates all three. Distinguish code/test failures, which return to the owning worker, from environment/flaky failures, which become `blocked`.

## Recovery and invalidation

Write state and generated documents atomically in their destination directory using a temporary file, flush, `fsync`, and `os.replace`.

On a stale `running` run, compare the saved checkpoint, child status, artifact hashes, and current Git state. Resume only from a complete safe checkpoint. Treat unowned partial worker changes or an ambiguous external edit as `blocked`; never repair or roll them back automatically.

Invalidate from the earliest changed dependency:

- changed spec input: spec approval and every downstream artifact;
- changed plan: plan approval and implementation/verification/review evidence;
- changed implementation plan: its approval and downstream implementation evidence;
- changed project files: deterministic, acceptance, and implementation-review evidence.

## Completion

Before `completed`, require all of:

- settled spec, passed spec review, and required spec approval;
- passed plan review and configured plan/implementation-plan approvals;
- all planned tasks complete and no unresolved scope violation;
- every required deterministic command passed;
- applicable AC-level acceptance evidence exists or has an explicit exception approval;
- no unresolved Critical or High finding;
- all final evidence hashes equal the current source state;
- starting user changes remain preserved.

Generate `result.md` atomically with the summary, changed files, AC-by-AC implementation and evidence, commands and results, resolved Critical/High findings, remaining Medium findings, known baseline failures, approved exceptions, unperformed items, and backlog reference. Then transition to `completed`.
