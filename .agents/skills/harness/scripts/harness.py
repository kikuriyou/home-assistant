#!/usr/bin/env python3
"""Deterministic state and evidence helper for the harness skill."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any, Iterator, Sequence


SCHEMA_VERSION = 1
PHASES = (
    "spec",
    "spec_review",
    "plan",
    "plan_review",
    "baseline_verification",
    "implementation",
    "deterministic_verification",
    "acceptance_verification",
    "implementation_review",
    "completed",
)
STATUSES = {
    "running",
    "awaiting_input",
    "awaiting_approval",
    "completed",
    "failed",
    "blocked",
    "aborted",
}
TERMINAL = {"completed", "failed", "blocked", "aborted"}
ARTIFACT_PHASE = {"spec": "spec", "plan": "plan", "implementation_plan": "plan"}
REVIEW_STAGES = {"spec_review", "plan_review", "implementation_review"}
REVIEW_FIELDS = {
    "id",
    "severity",
    "summary",
    "basis",
    "evidence",
    "current_risk",
    "minimal_fix",
}
FORWARD = {
    "spec": {"spec_review"},
    "spec_review": {"plan"},
    "plan": {"plan_review"},
    "plan_review": {"baseline_verification", "implementation"},
    "baseline_verification": {"implementation"},
    "implementation": {"deterministic_verification"},
    "deterministic_verification": {"acceptance_verification"},
    "acceptance_verification": {"implementation_review"},
    "implementation_review": set(),
    "completed": set(),
}
CORRECTIONS = {
    "spec_review": ("spec", "planner", "spec_review"),
    "plan_review": ("plan", "planner", "plan_review"),
    "deterministic_verification": (
        "implementation",
        "worker",
        "deterministic_verification",
    ),
    "acceptance_verification": (
        "implementation",
        "worker",
        "deterministic_verification",
    ),
    "implementation_review": (
        "implementation",
        "worker",
        "deterministic_verification",
    ),
}
AC_RE = re.compile(r"\bAC-\d{2}\b")
PLAN_COLUMNS = (
    "ac",
    "task",
    "write scope",
    "dependencies",
    "test level",
    "command",
    "environment",
    "evidence",
)


class HarnessError(RuntimeError):
    """A user-actionable deterministic harness error."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str | None:
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        return digest_bytes(os.readlink(path).encode())
    return digest_bytes(path.read_bytes())


def secure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    secure_dir(path.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        path.chmod(mode)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def atomic_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"expected JSON object: {path}")
    return value


def exact_keys(value: dict[str, Any], expected: set[str], where: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise HarnessError(f"unknown key in {where}: {sorted(unknown)[0]}")
    if missing:
        raise HarnessError(f"missing key in {where}: {sorted(missing)[0]}")


def validate_config(value: dict[str, Any]) -> dict[str, Any]:
    exact_keys(value, {"model_aliases", "assignments", "approvals", "timeouts"}, "config")
    aliases = value["model_aliases"]
    if not isinstance(aliases, dict) or not aliases:
        raise HarnessError("model_aliases must be a non-empty table")
    for name, alias in aliases.items():
        if not isinstance(alias, dict):
            raise HarnessError(f"model_aliases.{name} must be a table")
        exact_keys(alias, {"runtime", "model", "effort"}, f"model_aliases.{name}")
        if alias["runtime"] not in {"codex", "claude"}:
            raise HarnessError(f"invalid runtime for model_aliases.{name}")
        for field in ("model", "effort"):
            if not isinstance(alias[field], str) or not alias[field].strip():
                raise HarnessError(f"model_aliases.{name}.{field} must be non-empty")

    assignments = value["assignments"]
    required_assignments = {
        "planner",
        "worker",
        "worker_escalation",
        "spec_review",
        "plan_review",
        "implementation_review",
    }
    if not isinstance(assignments, dict):
        raise HarnessError("assignments must be a table")
    exact_keys(assignments, required_assignments, "assignments")
    for assignment, alias_name in assignments.items():
        if not isinstance(alias_name, str) or alias_name not in aliases:
            raise HarnessError(f"assignment {assignment} references missing model alias")
        expected_runtime = "codex" if assignment in {"planner", "worker", "worker_escalation"} else "claude"
        if aliases[alias_name]["runtime"] != expected_runtime:
            raise HarnessError(f"assignment {assignment} must use runtime {expected_runtime}")

    approvals = value["approvals"]
    if not isinstance(approvals, dict):
        raise HarnessError("approvals must be a table")
    exact_keys(approvals, {"spec", "plan", "implementation_plan"}, "approvals")
    for artifact, mode in approvals.items():
        if mode not in {"required", "skipped"}:
            raise HarnessError(f"invalid approval mode for {artifact}")

    timeouts = value["timeouts"]
    if not isinstance(timeouts, dict):
        raise HarnessError("timeouts must be a table")
    exact_keys(timeouts, {"agent_seconds", "command_seconds"}, "timeouts")
    for name, seconds in timeouts.items():
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
            raise HarnessError(f"timeouts.{name} must be a positive integer")
    return value


def load_config(path: Path) -> dict[str, Any]:
    try:
        return validate_config(tomllib.loads(path.read_text()))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise HarnessError(f"cannot load config {path}: {exc}") from exc


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", *args], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    if check and result.returncode:
        message = result.stderr.decode(errors="replace").strip()
        raise HarnessError(f"git {' '.join(args)} failed: {message}")
    return result


def find_repo(path: Path) -> Path:
    result = git(path, "rev-parse", "--show-toplevel", check=False)
    if result.returncode:
        raise HarnessError(f"not a Git repository: {path}")
    return Path(result.stdout.decode().strip()).resolve()


def inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise HarnessError(f"path is outside repository: {path}") from exc
    return resolved


def relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def excluded(path: str, excludes: Sequence[str]) -> bool:
    return any(path == item or path.startswith(item.rstrip("/") + "/") for item in excludes)


def untracked_files(root: Path, excludes: Sequence[str] = ()) -> list[dict[str, str | None]]:
    output = git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout
    paths = [part.decode(errors="surrogateescape") for part in output.split(b"\0") if part]
    return [
        {"path": path, "sha256": digest_file(root / path)}
        for path in sorted(paths)
        if not excluded(path, excludes)
    ]


def changed_files(root: Path, excludes: Sequence[str] = ()) -> dict[str, str | None]:
    tracked = git(root, "diff", "--name-only", "-z", "HEAD").stdout
    paths = {part.decode(errors="surrogateescape") for part in tracked.split(b"\0") if part}
    paths.update(item["path"] for item in untracked_files(root, excludes))
    return {
        path: digest_file(root / path)
        for path in sorted(paths)
        if not excluded(path, excludes)
    }


def source_snapshot(root: Path, excludes: Sequence[str] = ()) -> dict[str, Any]:
    head = git(root, "rev-parse", "HEAD").stdout.decode().strip()
    pathspec = ["--", ".", *[f":(exclude){item}/**" for item in excludes]]
    diff = git(root, "diff", "--binary", "HEAD", *pathspec).stdout
    untracked = untracked_files(root, excludes)
    payload = head.encode() + b"\0" + diff + b"\0" + canonical_json(untracked)
    return {
        "head": head,
        "tracked_diff": diff.decode(errors="replace"),
        "untracked": untracked,
        "source_state_hash": digest_bytes(payload),
    }


def state_source(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    return source_snapshot(root, (".harness", state["task_path"]))


def state_path(run_dir: Path) -> Path:
    return run_dir / "state.json"


def load_state(run_dir: Path) -> dict[str, Any]:
    state = read_json(state_path(run_dir))
    if state.get("schema_version") != SCHEMA_VERSION:
        raise HarnessError(f"unsupported state schema: {state.get('schema_version')}")
    return state


def save_state(run_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now()
    atomic_json(state_path(run_dir), state)


@contextlib.contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    lock_path = run_dir / ".lock"
    secure_dir(run_dir)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HarnessError(f"run is already locked: {run_dir.name}") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def ensure_runs_ignored(root: Path) -> Path:
    runs = root / ".harness" / "runs"
    secure_dir(runs)
    ignore = runs / ".gitignore"
    if not ignore.exists():
        atomic_write(ignore, b"*\n")
    elif ignore.read_bytes() != b"*\n":
        raise HarnessError(".harness/runs/.gitignore must contain exactly '*' followed by newline")
    tracked = git(root, "ls-files", "--", ".harness/runs").stdout
    if tracked.strip():
        raise HarnessError("runtime artifacts under .harness/runs are tracked")
    if git(root, "check-ignore", "-q", ".harness/runs/.gitignore", check=False).returncode:
        raise HarnessError(".harness/runs artifacts are not ignored")
    return runs


def skill_root() -> Path:
    return Path(__file__).resolve().parent.parent


def artifact_path(root: Path, state: dict[str, Any], artifact: str) -> Path:
    return root / state["task_path"] / f"{artifact}.md"


def existing_run(runs: Path, task_path: str) -> tuple[Path, dict[str, Any]] | None:
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for candidate in runs.iterdir():
        if not candidate.is_dir() or not (candidate / "state.json").exists():
            continue
        state = load_state(candidate)
        if state.get("task_path") == task_path and state.get("status") not in TERMINAL:
            candidates.append((candidate, state))
    return max(candidates, key=lambda item: item[1]["created_at"]) if candidates else None


def start_run(task: Path, *, allow_new_after_terminal: bool = False) -> tuple[Path, dict[str, Any], bool]:
    root = find_repo(task)
    task = inside(root, task)
    if not task.is_dir() or not (task / "user_requests.md").is_file():
        raise HarnessError("task directory and user_requests.md are required")
    spec = task / "spec.md"
    if not spec.is_file() or not spec.read_text().strip():
        raise HarnessError(
            "spec.md is missing or empty; offer to create it from user_requests.md "
            "and start only after the user confirms it is complete"
        )
    config_path = root / ".harness" / "config.toml"
    if not config_path.exists() and not config_path.is_symlink():
        atomic_write(config_path, (skill_root() / "assets" / "config.example.toml").read_bytes())
    config = load_config(config_path)
    runs = ensure_runs_ignored(root)
    task_rel = relative(root, task)
    unfinished = existing_run(runs, task_rel)
    if unfinished:
        return unfinished[0], unfinished[1], True
    terminal = [
        load_state(path)
        for path in runs.iterdir()
        if path.is_dir()
        and (path / "state.json").exists()
        and load_state(path).get("task_path") == task_rel
    ]
    if terminal and not allow_new_after_terminal:
        raise HarnessError("only terminal runs exist; explicitly request a new run")

    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)
    run_dir = runs / run_id
    secure_dir(run_dir)
    for child in ("snapshots", "snapshots/inputs", "snapshots/prompts", "agents", "verification"):
        secure_dir(run_dir / child)
    schema = read_json(skill_root() / "assets" / "review.schema.json")
    full_source = source_snapshot(root, (".harness/runs",))
    project_source = source_snapshot(root, (".harness", task_rel))
    atomic_write(run_dir / "snapshots" / "config.toml", config_path.read_bytes())
    atomic_json(run_dir / "snapshots" / "review.schema.json", schema)
    atomic_json(run_dir / "snapshots" / "source.json", full_source)
    input_files = [path for path in task.iterdir() if path.is_file()]
    atomic_json(
        run_dir / "snapshots" / "task-inputs.json",
        {"files": [{"path": relative(root, path), "sha256": digest_file(path)} for path in sorted(input_files)]},
    )
    approvals = {
        name: {
            "mode": mode,
            "status": "skipped" if mode == "skipped" else "pending",
            "hash": None,
        }
        for name, mode in config["approvals"].items()
    }
    created = now()
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "task_path": task_rel,
        "repo_root": str(root),
        "parent_runtime": "codex",
        "phase": "spec",
        "status": "running",
        "created_at": created,
        "updated_at": created,
        "pending_input": None,
        "discussion": {"decisions": [], "constraints": [], "remaining": []},
        "approvals": approvals,
        "artifact_hashes": {
            name: digest_file(task / f"{name}.md") for name in ARTIFACT_PHASE
        },
        "starting_changes": changed_files(root, (".harness/runs",)),
        "source_state_hash": project_source["source_state_hash"],
        "attempts": {"protocol_retry": {}, "quality_revision": {}, "worker_escalation": 0},
        "agents": [],
        "reviews": {},
        "review_history": [],
        "verification": {"baseline": [], "deterministic": [], "acceptance": []},
        "ac_evidence": {},
        "acceptance_exception": None,
        "implementation_complete": False,
        "scope_violations": [],
    }
    save_state(run_dir, state)
    return run_dir, state, False


def invalidate_from(state: dict[str, Any], artifact: str) -> None:
    state["phase"] = ARTIFACT_PHASE[artifact]
    state["status"] = "running"
    state["pending_input"] = None
    if artifact == "spec":
        for name in state["approvals"]:
            if state["approvals"][name]["mode"] == "required":
                state["approvals"][name].update(status="pending", hash=None)
        state["reviews"] = {}
    else:
        for name in ("plan", "implementation_plan"):
            if state["approvals"][name]["mode"] == "required":
                state["approvals"][name].update(status="pending", hash=None)
        for stage in ("plan_review", "implementation_review"):
            state["reviews"].pop(stage, None)
    state["verification"] = {"baseline": [], "deterministic": [], "acceptance": []}
    state["ac_evidence"] = {}
    state["acceptance_exception"] = None
    state["implementation_complete"] = False


def refresh_state(run_dir: Path) -> dict[str, Any]:
    with run_lock(run_dir):
        state = load_state(run_dir)
        if state["status"] in TERMINAL:
            return state
        root = Path(state["repo_root"])
        changed_artifact: str | None = None
        for artifact in ("spec", "plan", "implementation_plan"):
            current = digest_file(artifact_path(root, state, artifact))
            approval = state["approvals"][artifact]
            if state["artifact_hashes"].get(artifact) != current:
                changed_artifact = changed_artifact or artifact
            if approval["status"] == "approved" and approval["hash"] != current:
                approval.update(status="pending", hash=None)
            state["artifact_hashes"][artifact] = current
        if changed_artifact:
            invalidate_from(state, changed_artifact)
        current_source = state_source(root, state)["source_state_hash"]
        if current_source != state["source_state_hash"]:
            state["verification"]["deterministic"] = []
            state["verification"]["acceptance"] = []
            state["acceptance_exception"] = None
            state["reviews"].pop("implementation_review", None)
            state["source_state_hash"] = current_source
        save_state(run_dir, state)
        return state


def write_artifact(run_dir: Path, artifact: str, source: Path) -> dict[str, Any]:
    if artifact not in ARTIFACT_PHASE:
        raise HarnessError(f"unknown task artifact: {artifact}")
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        destination = artifact_path(Path(state["repo_root"]), state, artifact)
        atomic_write(destination, source.read_bytes(), 0o644)
        invalidate_from(state, artifact)
        state["artifact_hashes"][artifact] = digest_file(destination)
        save_state(run_dir, state)
        return state


def require_active(state: dict[str, Any]) -> None:
    if state["status"] in TERMINAL:
        raise HarnessError(f"terminal run cannot be changed: {state['status']}")


def transition(run_dir: Path, phase: str | None = None, status: str | None = None) -> dict[str, Any]:
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        phase = phase or state["phase"]
        status = status or "running"
        if phase not in PHASES or status not in STATUSES:
            raise HarnessError("invalid phase or status")
        current = state["phase"]
        if phase == "completed" or status == "completed":
            raise HarnessError("use completion check to enter completed")
        if phase != current:
            if phase in FORWARD[current]:
                if current == "spec_review" and state["approvals"]["spec"]["status"] == "pending":
                    raise HarnessError("spec approval is required before plan")
                if current == "plan_review":
                    for artifact in ("plan", "implementation_plan"):
                        if state["approvals"][artifact]["status"] == "pending":
                            raise HarnessError(f"{artifact} approval is required before implementation")
            elif current in CORRECTIONS and CORRECTIONS[current][0] == phase:
                attempts = state["attempts"]["quality_revision"]
                attempts[current] = attempts.get(current, 0) + 1
                if attempts[current] > 3:
                    state["status"] = "failed"
                    save_state(run_dir, state)
                    raise HarnessError(f"quality revision limit exceeded for {current}")
                if phase == "implementation":
                    state["verification"]["deterministic"] = []
                    state["verification"]["acceptance"] = []
                    state["reviews"].pop("implementation_review", None)
            else:
                raise HarnessError(f"transition not allowed: {current} -> {phase}")
        state.update(phase=phase, status=status)
        if status != "awaiting_input":
            state["pending_input"] = None
        save_state(run_dir, state)
        return state


def approve(run_dir: Path, artifact: str) -> dict[str, Any]:
    if artifact not in ARTIFACT_PHASE:
        raise HarnessError(f"unknown approval artifact: {artifact}")
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        approval = state["approvals"][artifact]
        if approval["mode"] == "skipped":
            return state
        path = artifact_path(Path(state["repo_root"]), state, artifact)
        value = digest_file(path)
        if value is None:
            raise HarnessError(f"cannot approve missing artifact: {path}")
        approval.update(status="approved", hash=value, approved_at=now())
        state["artifact_hashes"][artifact] = value
        state.update(status="running", pending_input=None)
        save_state(run_dir, state)
        return state


def abort(run_dir: Path) -> dict[str, Any]:
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        state.update(status="aborted", pending_input=None)
        save_state(run_dir, state)
        return state


def recover(run_dir: Path) -> dict[str, Any]:
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        root = Path(state["repo_root"])
        current = state_source(root, state)["source_state_hash"]
        running_agents = [agent for agent in state["agents"] if agent["status"] == "running"]
        if running_agents or current != state["source_state_hash"]:
            state.update(status="blocked", pending_input=None)
            state["recovery"] = {
                "decision": "blocked",
                "reason": "active child record or source differs from the last safe checkpoint",
                "checked_at": now(),
            }
        else:
            state.update(status="running", pending_input=None)
            state["recovery"] = {"decision": "resume", "checked_at": now()}
        save_state(run_dir, state)
        return state


def update_discussion(run_dir: Path, update: dict[str, Any]) -> dict[str, Any]:
    allowed = {"issues", "decision", "action", "issue_id", "pending_input"}
    unknown = set(update) - allowed
    if unknown:
        raise HarnessError(f"unknown discussion update key: {sorted(unknown)[0]}")
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        discussion = state["discussion"]
        discussion.setdefault("issues", [])
        discussion.setdefault("backlog_candidates", [])
        if "issues" in update:
            issues = update["issues"]
            if not isinstance(issues, list):
                raise HarnessError("discussion issues must be a list")
            identifiers: set[str] = set()
            for issue in issues:
                required = {
                    "id",
                    "category",
                    "question",
                    "recommendation",
                    "alternatives",
                    "tradeoffs",
                    "constraints",
                    "backlog_candidate",
                }
                if not isinstance(issue, dict) or set(issue) != required:
                    raise HarnessError("discussion issue has invalid fields")
                if issue["category"] not in {"must-decide", "recommended", "no-confirmation"}:
                    raise HarnessError("invalid discussion issue category")
                if issue["id"] in identifiers:
                    raise HarnessError(f"duplicate discussion issue: {issue['id']}")
                identifiers.add(issue["id"])
                issue["status"] = "open"
            discussion["issues"] = issues
            discussion["remaining"] = [issue["id"] for issue in issues if issue["category"] != "no-confirmation"]

        action = update.get("action")
        if action == "adopt_recommendations":
            for issue in discussion["issues"]:
                if issue["status"] == "open" and issue["category"] != "no-confirmation":
                    issue["status"] = "decided"
                    discussion["decisions"].append(
                        {
                            "issue_id": issue["id"],
                            "choice": issue["recommendation"],
                            "reason": "User adopted the recommendation.",
                        }
                    )
        elif action == "defer_to_backlog":
            issue_id = update.get("issue_id")
            issue = next((item for item in discussion["issues"] if item["id"] == issue_id), None)
            if not issue:
                raise HarnessError(f"unknown discussion issue: {issue_id}")
            issue["status"] = "deferred"
            discussion["backlog_candidates"].append(issue_id)
        elif action not in {None, "list_remaining"}:
            raise HarnessError(f"unknown discussion action: {action}")

        if "decision" in update:
            decision = update["decision"]
            if (
                not isinstance(decision, dict)
                or set(decision) != {"issue_id", "choice", "reason"}
                or not all(isinstance(decision[key], str) and decision[key].strip() for key in decision)
            ):
                raise HarnessError("decision requires non-empty issue_id, choice, and reason")
            issue = next(
                (item for item in discussion["issues"] if item["id"] == decision["issue_id"]),
                None,
            )
            if not issue:
                raise HarnessError(f"unknown discussion issue: {decision['issue_id']}")
            issue["status"] = "decided"
            discussion["decisions"].append(decision)
            discussion["constraints"].extend(issue["constraints"])

        discussion["remaining"] = [
            issue["id"]
            for issue in discussion["issues"]
            if issue["status"] == "open" and issue["category"] != "no-confirmation"
        ]
        pending = update.get("pending_input")
        if pending is not None:
            if not isinstance(pending, dict) or not pending.get("issue_id") or not pending.get("question"):
                raise HarnessError("pending_input requires issue_id and question")
            state.update(status="awaiting_input", pending_input=pending)
        elif action != "list_remaining":
            state.update(status="running", pending_input=None)
        save_state(run_dir, state)
        return state


def path_overlap(left: str, right: str) -> bool:
    left_parts = Path(left).parts
    right_parts = Path(right).parts
    return left_parts == right_parts[: len(left_parts)] or right_parts == left_parts[: len(right_parts)]


def valid_scope(path: str) -> bool:
    value = Path(path)
    return bool(path) and not value.is_absolute() and ".." not in value.parts and value != Path(".")


def paths_in_scope(paths: Sequence[str], scopes: Sequence[str]) -> tuple[list[str], list[str]]:
    in_scope = [path for path in paths if any(path_overlap(path, scope) for scope in scopes)]
    return in_scope, sorted(set(paths) - set(in_scope))


def prepare_agent(run_dir: Path, manifest: dict[str, Any], prompt: str) -> dict[str, Any]:
    required = {
        "role",
        "assignment",
        "objective",
        "ac_ids",
        "read_paths",
        "write_scope",
        "dependencies",
        "output",
        "depth",
        "parent_agent_id",
    }
    missing = required - set(manifest)
    if missing:
        raise HarnessError(f"agent manifest missing: {sorted(missing)[0]}")
    if manifest["depth"] != 1:
        raise HarnessError("agent depth must equal 1")
    if manifest["role"] not in {"planner", "worker", "semantic_reviewer"}:
        raise HarnessError("invalid child role")
    if manifest["role"] != "worker" and manifest["write_scope"]:
        raise HarnessError("planner and reviewer must be read-only")

    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        config = load_config(run_dir / "snapshots" / "config.toml")
        assignment = manifest["assignment"]
        if assignment not in config["assignments"]:
            raise HarnessError(f"unknown assignment: {assignment}")
        alias_name = config["assignments"][assignment]
        alias = config["model_aliases"][alias_name]
        expected_role = (
            "planner"
            if assignment == "planner"
            else "worker"
            if assignment in {"worker", "worker_escalation"}
            else "semantic_reviewer"
        )
        if manifest["role"] != expected_role:
            raise HarnessError("role does not match assignment")
        scopes = manifest["write_scope"]
        if not isinstance(scopes, list) or any(not isinstance(path, str) or not valid_scope(path) for path in scopes):
            raise HarnessError("write_scope must be a list of non-empty relative paths")
        for index, left in enumerate(scopes):
            for right in scopes[index + 1 :]:
                if path_overlap(left, right):
                    raise HarnessError(f"overlapping write scope in manifest: {left}, {right}")
        if manifest["role"] == "worker":
            starting = state["starting_changes"]
            conflicts = [path for path in starting if any(path_overlap(path, scope) for scope in scopes)]
            active_scopes = [
                scope
                for agent in state["agents"]
                if agent["status"] == "running" and agent["role"] == "worker"
                for scope in agent["write_scope"]
            ]
            conflicts.extend(
                scope for scope in scopes if any(path_overlap(scope, active) for active in active_scopes)
            )
            if conflicts:
                state.update(status="awaiting_input")
                state["pending_input"] = {
                    "kind": "write_scope_conflict",
                    "paths": sorted(set(conflicts)),
                }
                save_state(run_dir, state)
                raise HarnessError("worker write scope overlaps starting or active changes")

        invocation_id = manifest.get("invocation_id") or f"agent-{secrets.token_hex(6)}"
        agent_id = manifest.get("agent_id") or invocation_id
        root = Path(state["repo_root"])
        resolved = {
            **manifest,
            "invocation_id": invocation_id,
            "agent_id": agent_id,
            "runtime": alias["runtime"],
            "model_alias": alias_name,
            "model": alias["model"],
            "effort": alias["effort"],
            "attempt": manifest.get("attempt", 1),
            "before_changes": changed_files(root, (".harness/runs",)),
            "created_at": now(),
        }
        atomic_json(run_dir / "snapshots" / "inputs" / f"{invocation_id}.json", resolved)
        atomic_write(run_dir / "snapshots" / "prompts" / f"{invocation_id}.txt", prompt.encode())
        agent_dir = run_dir / "agents" / invocation_id
        secure_dir(agent_dir)
        atomic_json(agent_dir / "request.json", resolved)
        state["agents"].append(
            {
                key: resolved[key]
                for key in (
                    "invocation_id",
                    "agent_id",
                    "parent_agent_id",
                    "depth",
                    "role",
                    "assignment",
                    "runtime",
                    "model",
                    "effort",
                    "attempt",
                    "write_scope",
                )
            }
            | {"status": "running", "started_at": now()}
        )
        save_state(run_dir, state)
        return resolved


def finish_agent(run_dir: Path, invocation_id: str, status: str, output: str = "") -> dict[str, Any]:
    if status not in {"completed", "failed", "blocked"}:
        raise HarnessError("invalid agent completion status")
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        matches = [agent for agent in state["agents"] if agent["invocation_id"] == invocation_id]
        if len(matches) != 1 or matches[0]["status"] != "running":
            raise HarnessError("unknown or completed agent invocation")
        agent = matches[0]
        request = read_json(run_dir / "agents" / invocation_id / "request.json")
        atomic_write(run_dir / "agents" / invocation_id / "stdout.txt", output.encode())
        atomic_json(
            run_dir / "agents" / invocation_id / "completion.json",
            {"status": status, "ended_at": now()},
        )
        agent.update(status=status, ended_at=now())
        if agent["role"] == "worker":
            root = Path(state["repo_root"])
            after = changed_files(root, (".harness/runs",))
            before = request["before_changes"]
            touched = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
            _, outside = paths_in_scope(touched, agent["write_scope"])
            if outside:
                state["scope_violations"].append(
                    {"invocation_id": invocation_id, "paths": outside, "detected_at": now()}
                )
                state["status"] = "awaiting_input"
                state["pending_input"] = {"kind": "scope_violation", "paths": outside}
        save_state(run_dir, state)
        return state


def bind_agent_id(run_dir: Path, invocation_id: str, agent_id: str) -> dict[str, Any]:
    if not agent_id.strip():
        raise HarnessError("agent_id must be non-empty")
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        matches = [agent for agent in state["agents"] if agent["invocation_id"] == invocation_id]
        if len(matches) != 1:
            raise HarnessError(f"unknown agent invocation: {invocation_id}")
        matches[0]["agent_id"] = agent_id
        for path in (
            run_dir / "agents" / invocation_id / "request.json",
            run_dir / "snapshots" / "inputs" / f"{invocation_id}.json",
        ):
            value = read_json(path)
            value["agent_id"] = agent_id
            atomic_json(path, value)
        save_state(run_dir, state)
        return state


def validate_review(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"findings"} or not isinstance(value["findings"], list):
        raise HarnessError("review must be an object containing only a findings array")
    identifiers: set[str] = set()
    for finding in value["findings"]:
        if not isinstance(finding, dict) or set(finding) != REVIEW_FIELDS:
            raise HarnessError("review finding has invalid fields")
        if finding["severity"] not in {"critical", "high", "medium"}:
            raise HarnessError("review severity must be critical, high, or medium")
        for field in REVIEW_FIELDS - {"severity"}:
            if not isinstance(finding[field], str) or not finding[field].strip():
                raise HarnessError(f"review finding {field} must be non-empty")
        if finding["id"] in identifiers:
            raise HarnessError(f"duplicate review finding id: {finding['id']}")
        identifiers.add(finding["id"])
    return value


def review_passes(value: dict[str, Any]) -> bool:
    validate_review(value)
    return not any(finding["severity"] in {"critical", "high"} for finding in value["findings"])


def correction_route(stage: str) -> dict[str, str]:
    if stage not in CORRECTIONS:
        raise HarnessError(f"no correction route for stage: {stage}")
    phase, assignment, recheck = CORRECTIONS[stage]
    return {"assignment": assignment, "phase": phase, "recheck": recheck}


def consume_protocol_retry(state: dict[str, Any], invocation_id: str) -> int:
    retries = state["attempts"]["protocol_retry"]
    retries[invocation_id] = retries.get(invocation_id, 0) + 1
    if retries[invocation_id] > 1:
        raise HarnessError("protocol retry limit exceeded")
    return retries[invocation_id]


def consume_worker_escalation(state: dict[str, Any]) -> int:
    state["attempts"]["worker_escalation"] += 1
    if state["attempts"]["worker_escalation"] > 1:
        raise HarnessError("worker escalation limit exceeded")
    return state["attempts"]["worker_escalation"]


def escalate_worker(run_dir: Path) -> dict[str, Any]:
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        try:
            consume_worker_escalation(state)
        except HarnessError:
            state["status"] = "failed"
            save_state(run_dir, state)
            raise
        save_state(run_dir, state)
        return state


def parse_review_output(output: str) -> dict[str, Any]:
    try:
        value = json.loads(output)
        if isinstance(value, dict) and "structured_output" in value:
            value = value["structured_output"]
        elif isinstance(value, dict) and isinstance(value.get("result"), str):
            value = json.loads(value["result"])
    except json.JSONDecodeError as exc:
        raise HarnessError(f"review output is not JSON: {exc}") from exc
    return validate_review(value)


def claude_preflight(executable: str) -> dict[str, str]:
    try:
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
        help_result = subprocess.run(
            [executable, "--help"], capture_output=True, text=True, timeout=10, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise HarnessError(f"Claude CLI unavailable: {exc}") from exc
    if version.returncode or help_result.returncode:
        raise HarnessError("Claude CLI version/help preflight failed")
    required = {
        "--print",
        "--no-session-persistence",
        "--model",
        "--effort",
        "--permission-mode",
        "--tools",
        "--output-format",
        "--json-schema",
        "--settings",
    }
    missing = sorted(flag for flag in required if flag not in help_result.stdout)
    if missing:
        raise HarnessError(f"Claude CLI missing required flag: {missing[0]}")
    return {"version": version.stdout.strip(), "help_hash": digest_bytes(help_result.stdout.encode())}


def classify_failure(returncode: int | None, stderr: str, timed_out: bool = False) -> str:
    if timed_out:
        return "timeout"
    text = stderr.lower()
    if any(
        token in text
        for token in (
            "auth",
            "login",
            "credential",
            "permission denied",
            "network",
            "rate limit",
            "overloaded",
            "service unavailable",
            "connection",
            "timed out",
        )
    ):
        return "environment"
    return "quality" if returncode else "success"


def invoke_claude(
    executable: str,
    prompt: str,
    schema: dict[str, Any],
    model: str,
    effort: str,
    cwd: Path,
    timeout: int,
    *,
    add_dirs: Sequence[str] = (),
    plugin_dirs: Sequence[str] = (),
    mcp_configs: Sequence[str] = (),
) -> dict[str, Any]:
    preflight = claude_preflight(executable)
    command = [
        executable,
        "-p",
        "--no-session-persistence",
        "--model",
        model,
        "--effort",
        effort,
        "--permission-mode",
        "plan",
        "--tools",
        "Read,Glob,Grep",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema, separators=(",", ":")),
        "--settings",
        json.dumps({"disableAllHooks": True, "autoMemoryEnabled": False}, separators=(",", ":")),
    ]
    for value in add_dirs:
        command.extend(("--add-dir", value))
    for value in plugin_dirs:
        command.extend(("--plugin-dir", value))
    for value in mcp_configs:
        command.extend(("--mcp-config", value))
    attempts: list[dict[str, Any]] = []
    environment = os.environ.copy()
    for key in ("CLAUDE_CODE_DISABLE_CLAUDE_MDS", "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE"):
        environment.pop(key, None)
    environment["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    environment["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] = "1"
    for attempt in (1, 2):
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
            )
        except FileNotFoundError as exc:
            return {"classification": "environment", "error": str(exc), "attempts": attempts}
        except subprocess.TimeoutExpired as exc:
            attempts.append(
                {
                    "attempt": attempt,
                    "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "",
                    "returncode": None,
                    "duration_seconds": time.monotonic() - started,
                }
            )
            return {"classification": "timeout", "attempts": attempts, "preflight": preflight}
        record = {
            "attempt": attempt,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
            "duration_seconds": time.monotonic() - started,
        }
        attempts.append(record)
        if result.returncode:
            return {
                "classification": classify_failure(result.returncode, result.stderr + result.stdout),
                "attempts": attempts,
                "preflight": preflight,
            }
        try:
            review = parse_review_output(result.stdout)
        except HarnessError:
            if attempt == 1:
                continue
            return {"classification": "protocol", "attempts": attempts, "preflight": preflight}
        return {
            "classification": "success",
            "review": review,
            "attempts": attempts,
            "preflight": preflight,
            "command": command,
        }
    raise AssertionError("unreachable")


def record_review(run_dir: Path, stage: str, review: dict[str, Any]) -> dict[str, Any]:
    if stage not in REVIEW_STAGES:
        raise HarnessError("invalid review stage")
    review = validate_review(review)
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        source_hash = state_source(Path(state["repo_root"]), state)["source_state_hash"]
        state["source_state_hash"] = source_hash
        state["reviews"][stage] = {
            "passed": review_passes(review),
            "findings": review["findings"],
            "source_state_hash": source_hash,
            "recorded_at": now(),
        }
        if stage == "spec_review":
            state["artifact_hashes"]["spec"] = digest_file(
                artifact_path(Path(state["repo_root"]), state, "spec")
            )
        elif stage == "plan_review":
            for artifact in ("plan", "implementation_plan"):
                state["artifact_hashes"][artifact] = digest_file(
                    artifact_path(Path(state["repo_root"]), state, artifact)
                )
        if not state["reviews"][stage]["passed"]:
            state["reviews"][stage]["route"] = correction_route(stage)
        state["review_history"].append({"stage": stage, **state["reviews"][stage]})
        save_state(run_dir, state)
        return state


def claude_review(
    run_dir: Path,
    stage: str,
    manifest_path: Path,
    prompt_path: Path,
    executable: str = "claude",
) -> dict[str, Any]:
    state = load_state(run_dir)
    config = load_config(run_dir / "snapshots" / "config.toml")
    alias_name = config["assignments"][stage]
    alias = config["model_aliases"][alias_name]
    schema = read_json(run_dir / "snapshots" / "review.schema.json")
    manifest = read_json(manifest_path)
    if manifest.get("assignment") != stage or manifest.get("role") != "semantic_reviewer":
        raise HarnessError("Claude review manifest must match its review stage")
    for key in ("add_dirs", "plugin_dirs", "mcp_configs"):
        values = manifest.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            raise HarnessError(f"{key} must be a list of non-empty paths")
    prompt = prompt_path.read_text()
    resolved = prepare_agent(run_dir, manifest, prompt)
    outcome = invoke_claude(
        executable,
        prompt,
        schema,
        alias["model"],
        alias["effort"],
        Path(state["repo_root"]),
        config["timeouts"]["agent_seconds"],
        add_dirs=manifest.get("add_dirs", []),
        plugin_dirs=manifest.get("plugin_dirs", []),
        mcp_configs=manifest.get("mcp_configs", []),
    )
    invocation_id = resolved["invocation_id"]
    agent_dir = run_dir / "agents" / invocation_id
    for attempt in outcome.get("attempts", []):
        suffix = str(attempt["attempt"])
        atomic_write(agent_dir / f"stdout-{suffix}.txt", str(attempt["stdout"]).encode())
        atomic_write(agent_dir / f"stderr-{suffix}.txt", str(attempt["stderr"]).encode())
    safe_outcome = {**outcome}
    safe_outcome.pop("command", None)
    atomic_json(agent_dir / "result.json", safe_outcome)
    if len(outcome.get("attempts", [])) > 1:
        with run_lock(run_dir):
            retry_state = load_state(run_dir)
            consume_protocol_retry(retry_state, invocation_id)
            save_state(run_dir, retry_state)
    agent_status = (
        "completed"
        if outcome["classification"] == "success"
        else "blocked"
        if outcome["classification"] in {"environment", "timeout"}
        else "failed"
    )
    finish_agent(run_dir, invocation_id, agent_status)
    if outcome["classification"] == "success":
        record_review(run_dir, stage, outcome["review"])
    elif agent_status == "blocked":
        transition(run_dir, status="blocked")
    else:
        transition(run_dir, status="failed")
    return outcome


def extract_ac_ids(text: str) -> set[str]:
    return set(AC_RE.findall(text))


def check_ac_coverage(spec_text: str, plan_text: str) -> dict[str, list[str]]:
    required = extract_ac_ids(spec_text)
    planned = extract_ac_ids(plan_text)
    return {"missing": sorted(required - planned), "extra": sorted(planned - required)}


def validate_plan_mapping(spec_text: str, plan_text: str) -> dict[str, Any]:
    lines = plan_text.splitlines()
    header_index = None
    for index, line in enumerate(lines):
        cells = tuple(cell.strip().lower() for cell in line.strip().strip("|").split("|"))
        if cells == PLAN_COLUMNS:
            header_index = index
            break
    if header_index is None or header_index + 1 >= len(lines):
        raise HarnessError("plan must contain the canonical AC mapping table")
    separator = [cell.strip() for cell in lines[header_index + 1].strip().strip("|").split("|")]
    if len(separator) != len(PLAN_COLUMNS) or any(not re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
        raise HarnessError("plan AC mapping table has an invalid separator")
    rows: list[dict[str, str]] = []
    for line in lines[header_index + 2 :]:
        if not line.strip().startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != len(PLAN_COLUMNS) or any(not cell for cell in cells):
            raise HarnessError("plan AC mapping row must populate every column")
        rows.append(dict(zip(PLAN_COLUMNS, cells)))
    if not rows:
        raise HarnessError("plan AC mapping table must contain at least one row")
    mapped: set[str] = set()
    for row in rows:
        ids = extract_ac_ids(row["ac"])
        if not ids:
            raise HarnessError("plan AC mapping row has no AC ID")
        mapped.update(ids)
    required = extract_ac_ids(spec_text)
    return {
        "rows": rows,
        "missing": sorted(required - mapped),
        "extra": sorted(mapped - required),
    }


def verification_failure(returncode: int | None, stderr: str, timed_out: bool = False) -> str:
    if timed_out or returncode is None:
        return "environment"
    text = stderr.lower()
    if any(token in text for token in ("permission denied", "not found", "no such file", "auth", "network")):
        return "environment"
    return "code"


def run_verification(
    run_dir: Path,
    kind: str,
    command: Sequence[str],
    *,
    selection_source: str,
    ac_ids: Sequence[str] = (),
) -> dict[str, Any]:
    if kind not in {"baseline", "deterministic", "acceptance"}:
        raise HarnessError("invalid verification kind")
    if selection_source not in {"AGENTS.md", "ci", "script", "plan"}:
        raise HarnessError("invalid verification command source")
    if not command:
        raise HarnessError("verification command must not be empty")
    state = load_state(run_dir)
    root = Path(state["repo_root"])
    config = load_config(run_dir / "snapshots" / "config.toml")
    started = time.monotonic()
    timed_out = False
    try:
        result = subprocess.run(
            list(command),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=config["timeouts"]["command_seconds"],
            check=False,
        )
        returncode, stdout, stderr = result.returncode, result.stdout, result.stderr
    except (FileNotFoundError, PermissionError) as exc:
        returncode, stdout, stderr = None, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode, stdout, stderr = None, str(exc.stdout or ""), str(exc.stderr or "")
    snapshot = state_source(root, state)
    evidence = {
        "id": f"{kind}-{secrets.token_hex(5)}",
        "kind": kind,
        "command": list(command),
        "selection_source": selection_source,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_seconds": time.monotonic() - started,
        "source_state_hash": snapshot["source_state_hash"],
        "ac_ids": sorted(set(ac_ids)),
        "success": returncode == 0 and not timed_out,
        "recorded_at": now(),
    }
    if not evidence["success"]:
        evidence["failure_category"] = verification_failure(returncode, stderr, timed_out)
    with run_lock(run_dir):
        state = load_state(run_dir)
        evidence_dir = run_dir / "verification" / "e2e" if kind == "acceptance" else run_dir / "verification"
        secure_dir(evidence_dir)
        atomic_json(evidence_dir / f"{evidence['id']}.json", evidence)
        state["source_state_hash"] = snapshot["source_state_hash"]
        state["verification"][kind].append({key: value for key, value in evidence.items() if key not in {"stdout", "stderr"}})
        if evidence["success"]:
            for ac_id in evidence["ac_ids"]:
                state["ac_evidence"].setdefault(ac_id, []).append(evidence["id"])
        else:
            category = evidence["failure_category"]
            if kind == "baseline" or category == "environment":
                state["status"] = "blocked"
            else:
                current_phase = f"{kind}_verification"
                revisions = state["attempts"]["quality_revision"]
                revisions[current_phase] = revisions.get(current_phase, 0) + 1
                if revisions[current_phase] > 3:
                    state["status"] = "failed"
                else:
                    state.update(phase="implementation", status="running")
        save_state(run_dir, state)
    return evidence


def record_ac_evidence(run_dir: Path, ac_ids: Sequence[str], evidence: str) -> dict[str, Any]:
    if not evidence.strip():
        raise HarnessError("AC evidence must be non-empty")
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        for ac_id in ac_ids:
            if not AC_RE.fullmatch(ac_id):
                raise HarnessError(f"invalid AC ID: {ac_id}")
            state["ac_evidence"].setdefault(ac_id, []).append(evidence)
        save_state(run_dir, state)
        return state


def mark_implementation_complete(run_dir: Path) -> dict[str, Any]:
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        if state["scope_violations"]:
            raise HarnessError("scope violations remain unresolved")
        state["implementation_complete"] = True
        save_state(run_dir, state)
        return state


def approve_acceptance_exception(run_dir: Path, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise HarnessError("acceptance exception requires a reason")
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        source_hash = state_source(Path(state["repo_root"]), state)["source_state_hash"]
        state["acceptance_exception"] = {
            "reason": reason.strip(),
            "approved_by": "user",
            "approved_at": now(),
            "source_state_hash": source_hash,
        }
        save_state(run_dir, state)
        return state


def preserved_starting_changes(root: Path, state: dict[str, Any]) -> list[str]:
    current = changed_files(root, (".harness/runs",))
    return sorted(
        path for path, fingerprint in state["starting_changes"].items() if current.get(path) != fingerprint
    )


def complete_run(run_dir: Path) -> dict[str, Any]:
    refresh_state(run_dir)
    with run_lock(run_dir):
        state = load_state(run_dir)
        require_active(state)
        root = Path(state["repo_root"])
        task = root / state["task_path"]
        source_hash = state_source(root, state)["source_state_hash"]
        problems: list[str] = []
        for artifact, approval in state["approvals"].items():
            if approval["mode"] == "required" and approval["status"] != "approved":
                problems.append(f"{artifact} approval missing")
        for stage in REVIEW_STAGES:
            review = state["reviews"].get(stage)
            if not review or not review["passed"]:
                problems.append(f"{stage} not passed")
        if not state["implementation_complete"]:
            problems.append("implementation incomplete")
        for kind in ("deterministic", "acceptance"):
            evidence = state["verification"][kind]
            exception = state["acceptance_exception"] if kind == "acceptance" else None
            if not evidence and exception and exception["source_state_hash"] == source_hash:
                continue
            if not evidence or any(not item["success"] for item in evidence):
                problems.append(f"{kind} verification missing or failed")
            elif any(item["source_state_hash"] != source_hash for item in evidence):
                problems.append(f"{kind} evidence source hash is stale")
        implementation_review = state["reviews"].get("implementation_review")
        if implementation_review and implementation_review["source_state_hash"] != source_hash:
            problems.append("implementation review source hash is stale")
        if state["scope_violations"]:
            problems.append("scope violations remain")
        changed_start = preserved_starting_changes(root, state)
        if changed_start:
            problems.append(f"starting changes were modified: {', '.join(changed_start)}")
        spec_text = (task / "spec.md").read_text()
        plan_text = (task / "plan.md").read_text()
        coverage = validate_plan_mapping(spec_text, plan_text)
        if coverage["missing"]:
            problems.append(f"plan misses ACs: {', '.join(coverage['missing'])}")
        executed = {
            shlex.join(item["command"])
            for kind in ("baseline", "deterministic", "acceptance")
            for item in state["verification"][kind]
            if item["success"]
        }
        required_commands = {
            row["command"]
            for row in coverage["rows"]
            if not (
                state["acceptance_exception"]
                and row["test level"].strip().lower() in {"acceptance", "e2e"}
            )
        }
        missing_commands = sorted(required_commands - executed)
        if missing_commands:
            problems.append(f"planned commands not passed: {', '.join(missing_commands)}")
        missing_evidence = sorted(extract_ac_ids(spec_text) - set(state["ac_evidence"]))
        if missing_evidence:
            problems.append(f"AC evidence missing: {', '.join(missing_evidence)}")
        if problems:
            raise HarnessError("cannot complete: " + "; ".join(problems))

        changes = sorted(changed_files(root, (".harness/runs",)))
        result_rel = f"{state['task_path']}/result.md"
        if result_rel not in changes:
            changes.append(result_rel)
        medium = [
            finding
            for review in state["reviews"].values()
            for finding in review["findings"]
            if finding["severity"] == "medium"
        ]
        resolved_blocking = [
            finding
            for review in state["review_history"]
            for finding in review["findings"]
            if finding["severity"] in {"critical", "high"}
        ]
        lines = [
            "# Harness result",
            "",
            "## Summary",
            "",
            "All planned implementation, verification, acceptance, and semantic review gates passed.",
            "",
            "## Changed files",
            "",
            *([f"- {path}" for path in changes] or ["- None"]),
            "",
            "## AC evidence",
            "",
            *[
                f"- **{ac_id}**: {', '.join(evidence)}"
                for ac_id, evidence in sorted(state["ac_evidence"].items())
            ],
            "",
            "## Verification",
            "",
        ]
        for kind in ("baseline", "deterministic", "acceptance"):
            for item in state["verification"][kind]:
                lines.append(
                    f"- {kind}: {' '.join(shlex.quote(part) for part in item['command'])} -> {item['returncode']}"
                )
        lines.extend(
            [
                "",
                "## Review findings",
                "",
                "- Unresolved Critical/High: none",
                *(
                    [
                        f"- Resolved {item['severity'].title()} {item['id']}: {item['summary']}"
                        for item in resolved_blocking
                    ]
                    or ["- Resolved Critical/High: none"]
                ),
                *([f"- Medium {item['id']}: {item['summary']}" for item in medium] or ["- Medium: none"]),
                "",
                "## Unperformed and deferred",
                "",
                *(
                    [f"- Acceptance exception: {state['acceptance_exception']['reason']}"]
                    if state["acceptance_exception"]
                    else []
                ),
                "- See backlog.md; no backlog item was implemented by this run.",
            ]
        )
        atomic_write(task / "result.md", ("\n".join(lines) + "\n").encode(), 0o644)
        state.update(phase="completed", status="completed", pending_input=None)
        state["source_state_hash"] = source_hash
        state["completed_at"] = now()
        save_state(run_dir, state)
        return state


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-config")
    validate.add_argument("path", type=Path)
    start = commands.add_parser("start")
    start.add_argument("task", type=Path)
    start.add_argument("--new", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("run", type=Path)
    move = commands.add_parser("transition")
    move.add_argument("run", type=Path)
    move.add_argument("--phase", choices=PHASES)
    move.add_argument("--status", choices=sorted(STATUSES))
    approval = commands.add_parser("approve")
    approval.add_argument("run", type=Path)
    approval.add_argument("artifact", choices=sorted(ARTIFACT_PHASE))
    abort_parser = commands.add_parser("abort")
    abort_parser.add_argument("run", type=Path)
    recover_parser = commands.add_parser("recover")
    recover_parser.add_argument("run", type=Path)
    write = commands.add_parser("write-artifact")
    write.add_argument("run", type=Path)
    write.add_argument("artifact", choices=sorted(ARTIFACT_PHASE))
    write.add_argument("source", type=Path)
    discussion = commands.add_parser("discussion")
    discussion.add_argument("run", type=Path)
    discussion.add_argument("update", type=Path)
    prepare = commands.add_parser("prepare-agent")
    prepare.add_argument("run", type=Path)
    prepare.add_argument("manifest", type=Path)
    prepare.add_argument("prompt", type=Path)
    finish = commands.add_parser("finish-agent")
    finish.add_argument("run", type=Path)
    finish.add_argument("invocation_id")
    finish.add_argument("--status", required=True, choices=("completed", "failed", "blocked"))
    finish.add_argument("--output", default="")
    bind = commands.add_parser("bind-agent-id")
    bind.add_argument("run", type=Path)
    bind.add_argument("invocation_id")
    bind.add_argument("agent_id")
    review = commands.add_parser("record-review")
    review.add_argument("run", type=Path)
    review.add_argument("stage", choices=sorted(REVIEW_STAGES))
    review.add_argument("review", type=Path)
    claude = commands.add_parser("claude-review")
    claude.add_argument("run", type=Path)
    claude.add_argument("stage", choices=sorted(REVIEW_STAGES))
    claude.add_argument("manifest", type=Path)
    claude.add_argument("prompt", type=Path)
    claude.add_argument("--executable", default="claude")
    verify = commands.add_parser("verify")
    verify.add_argument("run", type=Path)
    verify.add_argument("kind", choices=("baseline", "deterministic", "acceptance"))
    verify.add_argument("--source", required=True, choices=("AGENTS.md", "ci", "script", "plan"))
    verify.add_argument("--ac", action="append", default=[])
    verify.add_argument("argv", nargs=argparse.REMAINDER)
    implemented = commands.add_parser("implementation-complete")
    implemented.add_argument("run", type=Path)
    exception = commands.add_parser("approve-acceptance-exception")
    exception.add_argument("run", type=Path)
    exception.add_argument("reason")
    escalation = commands.add_parser("escalate-worker")
    escalation.add_argument("run", type=Path)
    evidence = commands.add_parser("record-evidence")
    evidence.add_argument("run", type=Path)
    evidence.add_argument("evidence")
    evidence.add_argument("ac", nargs="+")
    complete = commands.add_parser("complete")
    complete.add_argument("run", type=Path)
    coverage = commands.add_parser("check-coverage")
    coverage.add_argument("spec", type=Path)
    coverage.add_argument("plan", type=Path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "validate-config":
            value = load_config(arguments.path)
        elif arguments.command == "start":
            run_dir, state, resumed = start_run(arguments.task, allow_new_after_terminal=arguments.new)
            value = {"run_dir": str(run_dir), "resumed": resumed, "state": state}
        elif arguments.command == "status":
            value = refresh_state(arguments.run)
        elif arguments.command == "transition":
            value = transition(arguments.run, arguments.phase, arguments.status)
        elif arguments.command == "approve":
            value = approve(arguments.run, arguments.artifact)
        elif arguments.command == "abort":
            value = abort(arguments.run)
        elif arguments.command == "recover":
            value = recover(arguments.run)
        elif arguments.command == "write-artifact":
            value = write_artifact(arguments.run, arguments.artifact, arguments.source)
        elif arguments.command == "discussion":
            value = update_discussion(arguments.run, read_json(arguments.update))
        elif arguments.command == "prepare-agent":
            value = prepare_agent(arguments.run, read_json(arguments.manifest), arguments.prompt.read_text())
        elif arguments.command == "finish-agent":
            value = finish_agent(arguments.run, arguments.invocation_id, arguments.status, arguments.output)
        elif arguments.command == "bind-agent-id":
            value = bind_agent_id(arguments.run, arguments.invocation_id, arguments.agent_id)
        elif arguments.command == "record-review":
            value = record_review(arguments.run, arguments.stage, read_json(arguments.review))
        elif arguments.command == "claude-review":
            value = claude_review(
                arguments.run,
                arguments.stage,
                arguments.manifest,
                arguments.prompt,
                arguments.executable,
            )
        elif arguments.command == "verify":
            command = arguments.argv[1:] if arguments.argv[:1] == ["--"] else arguments.argv
            if not command:
                raise HarnessError("verification command is required after --")
            value = run_verification(
                arguments.run,
                arguments.kind,
                command,
                selection_source=arguments.source,
                ac_ids=arguments.ac,
            )
        elif arguments.command == "implementation-complete":
            value = mark_implementation_complete(arguments.run)
        elif arguments.command == "approve-acceptance-exception":
            value = approve_acceptance_exception(arguments.run, arguments.reason)
        elif arguments.command == "escalate-worker":
            value = escalate_worker(arguments.run)
        elif arguments.command == "record-evidence":
            value = record_ac_evidence(arguments.run, arguments.ac, arguments.evidence)
        elif arguments.command == "complete":
            value = complete_run(arguments.run)
        elif arguments.command == "check-coverage":
            value = validate_plan_mapping(arguments.spec.read_text(), arguments.plan.read_text())
            if value["missing"]:
                print_json(value)
                return 1
        else:
            raise AssertionError("unhandled command")
        print_json(value)
        return 0
    except HarnessError as exc:
        print(f"harness: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
