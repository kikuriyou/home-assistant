# Project instructions

## Purpose

- Build a minimal, maintainable agent harness.
- Clarify requirements and acceptance criteria before implementation.

## Working rules

- Prefer existing tools and platform features; write custom code only when they cannot meet the requirements.
- Apply YAGNI. Do not add speculative roles, providers, abstractions, or configuration.
- Keep changes small, focused, and consistent with the current task.
- Do not simply agree with the user; prioritize candid, evidence-based responses and proposals that lead to meaningful improvements.
- Do not read `tasks/archived/` unless the user explicitly asks.

## Delegation

- Use subagents only for independent work or a distinct review.
- Give each subagent a clear scope, inputs, expected output, and acceptance criteria.
- Avoid concurrent edits to the same files.

## Verification

- Run the smallest relevant formatter, lint, type-check, and test commands available.
- Before completion, check requirement coverage, scope creep, and unresolved Critical or High findings.
- Report what was verified and what could not be run.
