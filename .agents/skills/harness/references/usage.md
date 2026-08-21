# Harness usage

## Requirements

Use Linux or macOS, a Git repository, and `uv`. The MVP was verified against Git 2.25.1, Codex CLI 0.147.0, and Claude Code 2.1.232. Later compatible versions are acceptable when their required flags remain available.

The harness never installs or updates these tools. Model availability and authentication are checked only when the corresponding stage first needs them.

Run every helper command through `uv`-managed Python 3.11 without adding a project dependency:

```bash
uv run --no-cache --no-python-downloads --no-project --python 3.11 python .agents/skills/harness/scripts/harness.py --help
```

## Install the Skill

Keep the canonical Skill in the repository. Optionally expose it as a user Skill with a symlink:

```bash
mkdir -p ~/.agents/skills
ln -s /absolute/project/.agents/skills/harness ~/.agents/skills/harness
```

Codex discovers repository Skills directly. Restart Codex only if a newly created or changed Skill does not appear.

## Configure a project

On first start, the helper atomically copies [../assets/config.example.toml](../assets/config.example.toml) to a missing `.harness/config.toml`. It never overwrites an existing path. Edit the generated model aliases, assignment mappings, approvals, or timeouts only when the defaults do not fit the project.

The first run also creates `.harness/runs/.gitignore` containing `*`, verifies that run artifacts are ignored and untracked, and stores them with owner-only permissions.

## Invoke and control a run

Use one Skill entry for all operations:

```text
$harness tasks/20260811
$harness tasks/20260811 を再開して
$harness tasks/20260811 の状態を表示して
$harness tasks/20260811 のspecを承認します
$harness tasks/20260811 を中断して
```

If `spec.md` is missing, empty, or still has material gaps, the harness does not start. It explains the gaps and asks whether to prepare the specification interactively from `user_requests.md`. When accepted, it asks one material question at a time, presents the completed draft for confirmation, and writes `spec.md` only after approval. Starting delivery requires a separate confirmation; no configuration or run artifacts are created before then.

After specification approval, the task artifact flow is `spec.md -> design.md -> plan.md`. The harness prepares `design.md` through the same interactive dialogue, writes it after explicit confirmation, and reviews it with the configured `plan_review` assignment before planning.

During specification or design dialogue, you can say “推奨案を採用”, “残りを一覧”, “このカテゴリを優先”, “一時停止”, or “backlogへ送る”. The parent resumes from saved decisions and pending input rather than replaying the conversation.

Run state and child/verification logs live under `.harness/runs/<run-id>/`. Task deliverables remain under the supplied task directory. Runtime logs are local and must not be committed.

## Troubleshoot

- Invalid `.harness/config.toml`: fix or remove it explicitly; the harness preserves existing paths and only bootstraps a truly missing config.
- Missing or incomplete `spec.md`: accept the offered specification dialogue or complete the file yourself, then explicitly start the delivery run.
- `blocked`: resolve the reported environment, authentication, permission, baseline, or ambiguous-diff condition, then start a new run or resume only when the saved checkpoint is safe.
- `failed`: inspect unresolved quality findings and exhausted correction counts. Do not relabel it as an environment failure.
- `awaiting_input`: answer the recorded pending question, especially before overlapping existing changes.
- `awaiting_approval`: review the named artifact and approve or request changes explicitly.
- stale `running`: inspect the reported checkpoint and Git differences. Never reset or discard partial work automatically.
- Claude preflight failure: install a compatible CLI version yourself or adjust the approved runtime configuration; the harness does not update it.
