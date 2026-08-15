# Harness usage

## Requirements

Use Linux or macOS, a Git repository, and Python 3.11 or newer. The MVP was verified against Git 2.25.1, Codex CLI 0.147.0, and Claude Code 2.1.232. Later compatible versions are acceptable when their required flags remain available.

The harness never installs or updates these tools. Model availability and authentication are checked only when the corresponding stage first needs them.

With `uv`, run the helper without adding a project dependency:

```bash
uv run --no-project --python 3.11 python .agents/skills/harness/scripts/harness.py --help
```

## Install the Skill

Keep the canonical Skill in the repository. Optionally expose it as a user Skill with a symlink:

```bash
mkdir -p ~/.agents/skills
ln -s /absolute/project/.agents/skills/harness ~/.agents/skills/harness
```

Codex discovers repository Skills directly. Restart Codex only if a newly created or changed Skill does not appear.

## Configure a project

Copy [../assets/config.example.toml](../assets/config.example.toml) to `.harness/config.toml` and edit only the model aliases, assignment mappings, approvals, and timeouts required by the project:

```bash
mkdir -p .harness
cp .agents/skills/harness/assets/config.example.toml .harness/config.toml
```

The harness does not generate or overwrite config. On the first run it creates `.harness/runs/.gitignore` containing `*`, verifies that run artifacts are ignored and untracked, and stores them with owner-only permissions.

## Invoke and control a run

Use one Skill entry for all operations:

```text
$harness tasks/20260811
$harness tasks/20260811 を再開して
$harness tasks/20260811 の状態を表示して
$harness tasks/20260811 のspecを承認します
$harness tasks/20260811 を中断して
```

During specification dialogue, you can say “推奨案を採用”, “残りを一覧”, “このカテゴリを優先”, “一時停止”, or “backlogへ送る”. The parent resumes from saved decisions and pending input rather than replaying the conversation.

Run state and child/verification logs live under `.harness/runs/<run-id>/`. Task deliverables remain under the supplied task directory. Runtime logs are local and must not be committed.

## Troubleshoot

- Missing `.harness/config.toml`: copy the example explicitly; the harness will not create it.
- `blocked`: resolve the reported environment, authentication, permission, baseline, or ambiguous-diff condition, then start a new run or resume only when the saved checkpoint is safe.
- `failed`: inspect unresolved quality findings and exhausted correction counts. Do not relabel it as an environment failure.
- `awaiting_input`: answer the recorded pending question, especially before overlapping existing changes.
- `awaiting_approval`: review the named artifact and approve or request changes explicitly.
- stale `running`: inspect the reported checkpoint and Git differences. Never reset or discard partial work automatically.
- Claude preflight failure: install a compatible CLI version yourself or adjust the approved runtime configuration; the harness does not update it.
