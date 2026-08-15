# Project instructions

## Purpose

- Build a minimal, maintainable home assistant.
- Clarify requirements and acceptance criteria before implementation.

## Working rules

- Prefer existing tools and platform features; write custom code only when they cannot meet the requirements.
- Apply YAGNI. Do not add speculative roles, providers, abstractions, or configuration.
- Keep changes small, focused, and consistent with the current task.
- Do not simply agree with the user; prioritize candid, evidence-based responses and proposals that lead to meaningful improvements.
- When the user gives development or collaboration feedback that should apply to future tasks, update this file in the same task unless the user asks otherwise.
- Record only concise, reusable project-wide guidance here; keep task-specific requirements in the task documentation.
- Do not put personal data, secrets, or environment-specific details—including local or cloud deployment identifiers, endpoints, and configuration—in `.agents/`, `AGENTS.md`, or `.gitignore`; use generic placeholders and keep real values in ignored local configuration or an external secret manager.
- Treat every task directory except the explicitly designated current task as read-only. Create a new task directory for follow-up work, and modify an older task only when the user explicitly asks.
- Do not read `tasks/archived/` unless the user explicitly asks.

## Delegation

- Use subagents only for independent work or a distinct review.
- Give each subagent a clear scope, inputs, expected output, and acceptance criteria.
- Avoid concurrent edits to the same files.

## Verification

- Run the smallest relevant formatter, lint, type-check, and test commands available.
- Before completion, check requirement coverage, scope creep, and unresolved Critical or High findings.
- Report what was verified and what could not be run.
