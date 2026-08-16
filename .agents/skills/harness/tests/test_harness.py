from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import textwrap
import tempfile
import unittest


SKILL_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = SKILL_ROOT / "scripts/harness.py"
CONFIG_PATH = SKILL_ROOT / "assets/config.example.toml"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures/smoke-repo"
SPEC = importlib.util.spec_from_file_location("harness", HARNESS_PATH)
assert SPEC and SPEC.loader
harness = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(harness)


def run(*argv: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=True)


class Repo:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        run("git", "init", "-q", cwd=self.root)
        run("git", "config", "user.email", "fixture@example.test", cwd=self.root)
        run("git", "config", "user.name", "Fixture", cwd=self.root)
        (self.root / "tasks/example").mkdir(parents=True)
        (self.root / ".harness").mkdir()
        shutil.copyfile(CONFIG_PATH, self.root / ".harness/config.toml")
        (self.root / "AGENTS.md").write_text("# Rules\n\nUse unittest.\n")
        (self.root / "app.py").write_text("def value():\n    return 1\n")
        (self.root / "tasks/example/user_requests.md").write_text("Change the value.\n")
        (self.root / "tasks/example/spec.md").write_text("# Spec\n\n- AC-01: value changes.\n")
        command = shlex.join([sys.executable, "-c", "raise SystemExit(0)"])
        (self.root / "tasks/example/plan.md").write_text(
            f"""# Plan

| AC | Task | Write scope | Dependencies | Test level | Command | Environment | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AC-01 | edit and verify | app.py | none | unit and acceptance | {command} | fixture | command log |
"""
        )
        (self.root / "tasks/example/backlog.md").write_text("# Backlog\n")
        run("git", "add", ".", cwd=self.root)
        run("git", "commit", "-qm", "fixture", cwd=self.root)
        self.task = self.root / "tasks/example"

    def close(self) -> None:
        self.temp.cleanup()

    def start(self):
        return harness.start_run(self.task)


class HarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repo()

    def tearDown(self) -> None:
        self.repo.close()

    def test_config_validation(self) -> None:
        valid = harness.load_config(self.repo.root / ".harness/config.toml")
        self.assertEqual("gpt-5.6-sol", valid["model_aliases"]["planner"]["model"])
        cases = []
        unknown = copy.deepcopy(valid)
        unknown["extra"] = {}
        cases.append(unknown)
        runtime = copy.deepcopy(valid)
        runtime["model_aliases"]["planner"]["runtime"] = "other"
        cases.append(runtime)
        assignment = copy.deepcopy(valid)
        assignment["assignments"]["worker"] = "missing"
        cases.append(assignment)
        empty = copy.deepcopy(valid)
        empty["model_aliases"]["planner"]["model"] = ""
        cases.append(empty)
        approval = copy.deepcopy(valid)
        approval["approvals"]["spec"] = "sometimes"
        cases.append(approval)
        timeout = copy.deepcopy(valid)
        timeout["timeouts"]["agent_seconds"] = 0
        cases.append(timeout)
        wrong_provider = copy.deepcopy(valid)
        wrong_provider["assignments"]["spec_review"] = "planner"
        cases.append(wrong_provider)
        for invalid in cases:
            with self.subTest(invalid=invalid), self.assertRaises(harness.HarnessError):
                harness.validate_config(invalid)

    def test_start_resume_snapshots_ignore_and_permissions(self) -> None:
        run_dir, state, resumed = self.repo.start()
        self.assertFalse(resumed)
        self.assertEqual("spec", state["phase"])
        self.assertEqual("codex", state["parent_runtime"])
        self.assertEqual("*\n", (self.repo.root / ".harness/runs/.gitignore").read_text())
        self.assertEqual("", run("git", "status", "--short", cwd=self.repo.root).stdout)
        self.assertTrue((run_dir / "snapshots/config.toml").is_file())
        self.assertTrue((run_dir / "snapshots/review.schema.json").is_file())
        self.assertTrue((run_dir / "snapshots/source.json").is_file())
        self.assertEqual(0o700, stat.S_IMODE(run_dir.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE((run_dir / "state.json").stat().st_mode))
        resumed_dir, resumed_state, resumed = self.repo.start()
        self.assertTrue(resumed)
        self.assertEqual(run_dir, resumed_dir)
        self.assertEqual(state["run_id"], resumed_state["run_id"])

    def test_start_bootstraps_missing_config_without_overwriting(self) -> None:
        shutil.rmtree(self.repo.root / ".harness")
        config = self.repo.root / ".harness/config.toml"
        run_dir, _, _ = self.repo.start()
        self.assertEqual(CONFIG_PATH.read_bytes(), config.read_bytes())
        self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))

        custom = config.read_text().replace("agent_seconds = 1800", "agent_seconds = 123")
        config.write_text(custom)
        resumed_dir, _, resumed = self.repo.start()
        self.assertTrue(resumed)
        self.assertEqual(run_dir, resumed_dir)
        self.assertEqual(custom, config.read_text())

    def test_transition_approval_and_terminal_protection(self) -> None:
        run_dir, _, _ = self.repo.start()
        harness.transition(run_dir, "spec_review")
        with self.assertRaises(harness.HarnessError):
            harness.transition(run_dir, "plan")
        harness.approve(run_dir, "spec")
        state = harness.transition(run_dir, "plan")
        self.assertEqual("skipped", state["approvals"]["plan"]["status"])
        with self.assertRaises(harness.HarnessError):
            harness.transition(run_dir, "implementation")
        state = harness.abort(run_dir)
        self.assertEqual("aborted", state["status"])
        with self.assertRaises(harness.HarnessError):
            harness.transition(run_dir, status="running")

    def test_artifact_and_source_changes_invalidate_evidence(self) -> None:
        run_dir, _, _ = self.repo.start()
        harness.approve(run_dir, "spec")
        state = harness.load_state(run_dir)
        state["verification"]["deterministic"] = [{"success": True}]
        state["verification"]["acceptance"] = [{"success": True}]
        state["reviews"]["implementation_review"] = {"passed": True}
        harness.save_state(run_dir, state)
        with (self.repo.task / "spec.md").open("a") as stream:
            stream.write("\nChanged.\n")
        state = harness.refresh_state(run_dir)
        self.assertEqual("pending", state["approvals"]["spec"]["status"])
        self.assertEqual("spec", state["phase"])
        self.assertEqual([], state["verification"]["deterministic"])
        self.assertNotIn("implementation_review", state["reviews"])

        state["verification"]["deterministic"] = [{"success": True}]
        state["verification"]["acceptance"] = [{"success": True}]
        state["reviews"]["implementation_review"] = {"passed": True}
        harness.save_state(run_dir, state)
        (self.repo.root / "app.py").write_text("def value():\n    return 2\n")
        state = harness.refresh_state(run_dir)
        self.assertEqual([], state["verification"]["deterministic"])
        self.assertEqual([], state["verification"]["acceptance"])
        self.assertNotIn("implementation_review", state["reviews"])

    def test_lock_abort_and_recovery(self) -> None:
        run_dir, _, _ = self.repo.start()
        with harness.run_lock(run_dir), self.assertRaises(harness.HarnessError):
            harness.transition(run_dir, status="awaiting_input")
        self.assertEqual("resume", harness.recover(run_dir)["recovery"]["decision"])
        (self.repo.root / "app.py").write_text("def value():\n    return 2\n")
        state = harness.recover(run_dir)
        self.assertEqual("blocked", state["status"])

    def test_starting_changes_are_recorded_without_modification(self) -> None:
        self.repo.close()
        self.repo = Repo()
        changed = "def value():\n    return 7\n"
        (self.repo.root / "app.py").write_text(changed)
        before = run("git", "status", "--short", cwd=self.repo.root).stdout
        _, state, _ = self.repo.start()
        after = run("git", "status", "--short", cwd=self.repo.root).stdout
        self.assertEqual(before, after)
        self.assertEqual(changed, (self.repo.root / "app.py").read_text())
        self.assertIn("app.py", state["starting_changes"])

    def test_atomic_write_leaves_no_temporary_file(self) -> None:
        path = self.repo.root / "atomic.json"
        harness.atomic_json(path, {"value": 1})
        harness.atomic_json(path, {"value": 2})
        self.assertEqual({"value": 2}, json.loads(path.read_text()))
        self.assertEqual([], list(self.repo.root.glob(".atomic.json.*")))

    def test_discussion_pause_resume_and_shortcuts(self) -> None:
        run_dir, _, _ = self.repo.start()
        issues = [
            {
                "id": "storage",
                "category": "must-decide",
                "question": "Where is state stored?",
                "recommendation": "Local files",
                "alternatives": ["Database"],
                "tradeoffs": "Local files are simpler.",
                "constraints": ["Owner-only permissions"],
                "backlog_candidate": False,
            },
            {
                "id": "dashboard",
                "category": "recommended",
                "question": "Add a dashboard?",
                "recommendation": "No dashboard",
                "alternatives": ["Build one"],
                "tradeoffs": "A dashboard adds maintenance.",
                "constraints": [],
                "backlog_candidate": True,
            },
            {
                "id": "name",
                "category": "no-confirmation",
                "question": "Choose an internal name.",
                "recommendation": "harness",
                "alternatives": [],
                "tradeoffs": "None",
                "constraints": [],
                "backlog_candidate": False,
            },
        ]
        pending = {"issue_id": "storage", "question": "Where is state stored?"}
        state = harness.update_discussion(run_dir, {"issues": issues, "pending_input": pending})
        self.assertEqual("awaiting_input", state["status"])
        self.assertEqual(pending, harness.load_state(run_dir)["pending_input"])
        listed = harness.update_discussion(run_dir, {"action": "list_remaining"})
        self.assertEqual(["storage", "dashboard"], listed["discussion"]["remaining"])
        decided = harness.update_discussion(
            run_dir,
            {
                "decision": {
                    "issue_id": "storage",
                    "choice": "Local files",
                    "reason": "Minimum moving parts.",
                }
            },
        )
        self.assertEqual(["dashboard"], decided["discussion"]["remaining"])
        adopted = harness.update_discussion(run_dir, {"action": "adopt_recommendations"})
        self.assertEqual([], adopted["discussion"]["remaining"])
        self.assertEqual("running", adopted["status"])

    def test_fresh_planner_manifest_and_revision_route(self) -> None:
        run_dir, _, _ = self.repo.start()
        manifest = {
            "role": "planner",
            "assignment": "planner",
            "objective": "Create the specification discussion map.",
            "ac_ids": ["AC-01"],
            "read_paths": ["AGENTS.md", "tasks/example/user_requests.md"],
            "write_scope": [],
            "dependencies": [],
            "output": "Discussion JSON only.",
            "depth": 1,
            "parent_agent_id": "parent",
        }
        resolved = harness.prepare_agent(run_dir, manifest, "Task-local prompt")
        self.assertEqual("codex", resolved["runtime"])
        self.assertEqual("gpt-5.6-sol", resolved["model"])
        self.assertTrue((run_dir / f"snapshots/inputs/{resolved['invocation_id']}.json").is_file())
        self.assertEqual(
            "Task-local prompt",
            (run_dir / f"snapshots/prompts/{resolved['invocation_id']}.txt").read_text(),
        )
        state = harness.bind_agent_id(run_dir, resolved["invocation_id"], "native-agent-123")
        self.assertEqual("native-agent-123", state["agents"][0]["agent_id"])
        request = json.loads(
            (run_dir / f"agents/{resolved['invocation_id']}/request.json").read_text()
        )
        self.assertEqual("native-agent-123", request["agent_id"])
        harness.finish_agent(run_dir, resolved["invocation_id"], "completed")
        self.assertEqual(
            {"assignment": "planner", "phase": "spec", "recheck": "spec_review"},
            harness.correction_route("spec_review"),
        )
        for _ in range(3):
            harness.transition(run_dir, "spec_review")
            harness.transition(run_dir, "spec")
        harness.transition(run_dir, "spec_review")
        with self.assertRaises(harness.HarnessError):
            harness.transition(run_dir, "spec")
        self.assertEqual("failed", harness.load_state(run_dir)["status"])

    def test_plan_mapping_requires_every_ac_and_field(self) -> None:
        spec = "# Spec\n\n- AC-01: new path.\n- AC-02: preserved path.\n"
        plan = """# Plan

| AC | Task | Write scope | Dependencies | Test level | Command | Environment | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AC-01 | edit | app.py | none | unit | python -m unittest | local | test log |
| AC-02 | verify | none | AC-01 | integration | python test_app.py | fixture | test log |
"""
        result = harness.validate_plan_mapping(spec, plan)
        self.assertEqual([], result["missing"])
        self.assertEqual(2, len(result["rows"]))
        missing = harness.validate_plan_mapping(spec, plan.replace("AC-02", "AC-03"))
        self.assertEqual(["AC-02"], missing["missing"])
        bad = plan.replace("| test log |", "| |", 1)
        with self.assertRaises(harness.HarnessError):
            harness.validate_plan_mapping(spec, bad)

    def test_delta_spec_uses_same_ac_mapping(self) -> None:
        delta_spec = """# Delta specification

## Current behavior
Returns one.

## Desired change
Return two. AC-01

## Preserved behavior
Public function name remains. AC-02
"""
        plan = """| AC | Task | Write scope | Dependencies | Test level | Command | Environment | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AC-01 | change value | app.py | none | unit | python test_app.py | fixture | output |
| AC-02 | regression | none | AC-01 | integration | python test_app.py | fixture | output |
"""
        self.assertEqual([], harness.validate_plan_mapping(delta_spec, plan)["missing"])

    def fake_claude(self, mode: str) -> Path:
        path = self.repo.root / f"fake-claude-{mode}"
        path.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import json
                import os
                from pathlib import Path
                import sys
                import time

                mode = {mode!r}
                if "--version" in sys.argv:
                    print("fake 1.0")
                    raise SystemExit
                if "--help" in sys.argv:
                    print("--print --no-session-persistence --model --effort --permission-mode "
                          "--tools --output-format --json-schema --settings")
                    raise SystemExit
                if os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") != "1":
                    print("auto memory is enabled", file=sys.stderr)
                    raise SystemExit(4)
                if os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") != "1":
                    print("nonessential traffic is enabled", file=sys.stderr)
                    raise SystemExit(4)
                if mode == "timeout":
                    time.sleep(1)
                if mode == "auth":
                    print("authentication required", file=sys.stderr)
                    raise SystemExit(2)
                if mode == "api_timeout":
                    print(json.dumps({{"is_error": True, "terminal_reason": "api_error",
                                      "result": "Request timed out"}}))
                    raise SystemExit(1)
                if mode == "quality":
                    print("provider failure", file=sys.stderr)
                    raise SystemExit(3)
                if mode == "invalid":
                    print("not json")
                    raise SystemExit
                if mode == "invalid_then_success":
                    counter = Path(__file__).with_suffix(".count")
                    count = int(counter.read_text()) if counter.exists() else 0
                    counter.write_text(str(count + 1))
                    if count == 0:
                        print("not json")
                        raise SystemExit
                print(json.dumps({{"structured_output": {{"findings": []}}}}))
                """
            )
        )
        path.chmod(0o755)
        return path

    @staticmethod
    def finding(severity: str = "medium", **changes):
        value = {
            "id": "F-1",
            "severity": severity,
            "summary": "Current issue",
            "basis": "AC-01",
            "evidence": "app.py:1",
            "current_risk": "The current requirement can fail.",
            "minimal_fix": "Change one line.",
        }
        value.update(changes)
        return value

    def test_review_schema_severity_and_routes(self) -> None:
        self.assertTrue(harness.review_passes({"findings": []}))
        self.assertTrue(harness.review_passes({"findings": [self.finding()]}))
        self.assertFalse(harness.review_passes({"findings": [self.finding("high")]}))
        self.assertFalse(harness.review_passes({"findings": [self.finding("critical")]}))
        with self.assertRaises(harness.HarnessError):
            harness.validate_review({"findings": [self.finding("low")]})
        with self.assertRaises(harness.HarnessError):
            harness.validate_review({"findings": [self.finding(evidence="")]})
        with self.assertRaises(harness.HarnessError):
            harness.validate_review({"findings": [self.finding(), self.finding()]})
        self.assertEqual("planner", harness.correction_route("plan_review")["assignment"])
        self.assertEqual("worker", harness.correction_route("implementation_review")["assignment"])
        self.assertEqual(
            "deterministic_verification",
            harness.correction_route("acceptance_verification")["recheck"],
        )

    def test_fake_claude_success_retry_timeout_and_auth(self) -> None:
        schema = json.loads((SKILL_ROOT / "assets/review.schema.json").read_text())
        success = harness.invoke_claude(
            str(self.fake_claude("success")),
            "review",
            schema,
            "fable",
            "xhigh",
            self.repo.root,
            1,
        )
        self.assertEqual("success", success["classification"])
        self.assertIn("--no-session-persistence", success["command"])
        self.assertNotIn("--bare", success["command"])
        self.assertEqual("plan", success["command"][success["command"].index("--permission-mode") + 1])
        self.assertEqual("Read,Glob,Grep", success["command"][success["command"].index("--tools") + 1])
        cli_schema = json.loads(success["command"][success["command"].index("--json-schema") + 1])
        self.assertNotIn("$schema", cli_schema)

        retried = harness.invoke_claude(
            str(self.fake_claude("invalid_then_success")),
            "review",
            schema,
            "fable",
            "xhigh",
            self.repo.root,
            1,
        )
        self.assertEqual("success", retried["classification"])
        self.assertEqual(2, len(retried["attempts"]))
        protocol = harness.invoke_claude(
            str(self.fake_claude("invalid")),
            "review",
            schema,
            "fable",
            "xhigh",
            self.repo.root,
            1,
        )
        self.assertEqual("protocol", protocol["classification"])
        self.assertEqual(2, len(protocol["attempts"]))
        auth = harness.invoke_claude(
            str(self.fake_claude("auth")),
            "review",
            schema,
            "fable",
            "xhigh",
            self.repo.root,
            1,
        )
        self.assertEqual("environment", auth["classification"])
        self.assertEqual(1, len(auth["attempts"]))
        api_timeout = harness.invoke_claude(
            str(self.fake_claude("api_timeout")),
            "review",
            schema,
            "fable",
            "xhigh",
            self.repo.root,
            1,
        )
        self.assertEqual("environment", api_timeout["classification"])
        self.assertEqual(1, len(api_timeout["attempts"]))
        timeout = harness.invoke_claude(
            str(self.fake_claude("timeout")),
            "review",
            schema,
            "fable",
            "xhigh",
            self.repo.root,
            0.05,
        )
        self.assertEqual("timeout", timeout["classification"])
        self.assertEqual(1, len(timeout["attempts"]))

    def test_claude_review_records_fresh_agent_metadata(self) -> None:
        run_dir, _, _ = self.repo.start()
        manifest_path = self.repo.task / "review-manifest.json"
        prompt_path = self.repo.task / "review-prompt.txt"
        manifest_path.write_text(
            json.dumps(
                {
                    "role": "semantic_reviewer",
                    "assignment": "spec_review",
                    "objective": "Review the specification.",
                    "ac_ids": ["AC-01"],
                    "read_paths": ["tasks/example/spec.md"],
                    "write_scope": [],
                    "dependencies": [],
                    "output": "Canonical review JSON.",
                    "depth": 1,
                    "parent_agent_id": "parent",
                }
            )
        )
        prompt_path.write_text("Review only the supplied specification.")
        outcome = harness.claude_review(
            run_dir,
            "spec_review",
            manifest_path,
            prompt_path,
            str(self.fake_claude("success")),
        )
        self.assertEqual("success", outcome["classification"])
        state = harness.load_state(run_dir)
        agent = state["agents"][0]
        self.assertEqual("semantic_reviewer", agent["role"])
        self.assertEqual(1, agent["depth"])
        self.assertEqual("completed", agent["status"])
        self.assertTrue(state["reviews"]["spec_review"]["passed"])
        self.assertTrue((run_dir / f"snapshots/inputs/{agent['invocation_id']}.json").is_file())

    def test_depth_parallel_scopes_and_scope_violation(self) -> None:
        run_dir, _, _ = self.repo.start()

        def worker(scope, depth=1):
            return {
                "role": "worker",
                "assignment": "worker",
                "objective": "Implement AC-01.",
                "ac_ids": ["AC-01"],
                "read_paths": ["app.py"],
                "write_scope": scope,
                "dependencies": [],
                "output": "Diff and test evidence.",
                "depth": depth,
                "parent_agent_id": "parent",
            }

        with self.assertRaises(harness.HarnessError):
            harness.prepare_agent(run_dir, worker(["app.py"], depth=2), "work")
        first = harness.prepare_agent(run_dir, worker(["app.py"]), "work one")
        second = harness.prepare_agent(run_dir, worker(["test_app.py"]), "work two")
        with self.assertRaises(harness.HarnessError):
            harness.prepare_agent(run_dir, worker(["app.py"]), "overlap")
        harness.finish_agent(run_dir, first["invocation_id"], "completed")
        harness.finish_agent(run_dir, second["invocation_id"], "completed")

        third = harness.prepare_agent(run_dir, worker(["app.py"]), "work three")
        (self.repo.root / "outside.py").write_text("outside = True\n")
        state = harness.finish_agent(run_dir, third["invocation_id"], "completed")
        self.assertEqual("awaiting_input", state["status"])
        self.assertEqual(["outside.py"], state["scope_violations"][0]["paths"])

    def test_retry_revision_and_escalation_limits(self) -> None:
        state = {
            "attempts": {
                "protocol_retry": {},
                "quality_revision": {},
                "worker_escalation": 0,
            }
        }
        self.assertEqual(1, harness.consume_protocol_retry(state, "review"))
        with self.assertRaises(harness.HarnessError):
            harness.consume_protocol_retry(state, "review")
        self.assertEqual(1, harness.consume_worker_escalation(state))
        with self.assertRaises(harness.HarnessError):
            harness.consume_worker_escalation(state)
        run_dir, _, _ = self.repo.start()
        self.assertEqual(1, harness.escalate_worker(run_dir)["attempts"]["worker_escalation"])
        with self.assertRaises(harness.HarnessError):
            harness.escalate_worker(run_dir)
        self.assertEqual("failed", harness.load_state(run_dir)["status"])

    @staticmethod
    def passing_command():
        return [sys.executable, "-c", "raise SystemExit(0)"]

    def prepare_completion(self, *, acceptance: bool = True, medium: bool = False):
        run_dir, _, _ = self.repo.start()
        harness.approve(run_dir, "spec")
        harness.record_review(run_dir, "spec_review", {"findings": []})
        harness.record_review(run_dir, "plan_review", {"findings": []})
        harness.mark_implementation_complete(run_dir)
        harness.run_verification(
            run_dir,
            "deterministic",
            self.passing_command(),
            selection_source="plan",
            ac_ids=["AC-01"],
        )
        if acceptance:
            harness.run_verification(
                run_dir,
                "acceptance",
                self.passing_command(),
                selection_source="plan",
                ac_ids=["AC-01"],
            )
        findings = [self.finding()] if medium else []
        harness.record_review(run_dir, "implementation_review", {"findings": findings})
        return run_dir

    def test_verification_failures_route_code_and_block_environment(self) -> None:
        run_dir, _, _ = self.repo.start()
        evidence = harness.run_verification(
            run_dir,
            "deterministic",
            [sys.executable, "-c", "raise SystemExit(1)"],
            selection_source="plan",
        )
        self.assertEqual("code", evidence["failure_category"])
        self.assertEqual("implementation", harness.load_state(run_dir)["phase"])
        stored = json.loads((run_dir / "verification" / f"{evidence['id']}.json").read_text())
        self.assertEqual("code", stored["failure_category"])
        harness.abort(run_dir)

        run_dir, _, _ = harness.start_run(self.repo.task, allow_new_after_terminal=True)
        evidence = harness.run_verification(
            run_dir,
            "deterministic",
            ["definitely-not-a-command"],
            selection_source="plan",
        )
        self.assertEqual("environment", evidence["failure_category"])
        self.assertEqual("blocked", harness.load_state(run_dir)["status"])

    def test_project_change_invalidates_all_final_evidence(self) -> None:
        run_dir = self.prepare_completion()
        (self.repo.root / "app.py").write_text("def value():\n    return 2\n")
        state = harness.refresh_state(run_dir)
        self.assertEqual([], state["verification"]["deterministic"])
        self.assertEqual([], state["verification"]["acceptance"])
        self.assertNotIn("implementation_review", state["reviews"])
        with self.assertRaises(harness.HarnessError):
            harness.complete_run(run_dir)

    def test_completion_requires_e2e_and_writes_result(self) -> None:
        missing = self.prepare_completion(acceptance=False)
        with self.assertRaisesRegex(harness.HarnessError, "acceptance verification"):
            harness.complete_run(missing)
        harness.abort(missing)

        run_dir, _, _ = harness.start_run(self.repo.task, allow_new_after_terminal=True)
        harness.approve(run_dir, "spec")
        for stage in ("spec_review", "plan_review"):
            harness.record_review(run_dir, stage, {"findings": []})
        harness.mark_implementation_complete(run_dir)
        for kind in ("deterministic", "acceptance"):
            harness.run_verification(
                run_dir,
                kind,
                self.passing_command(),
                selection_source="plan",
                ac_ids=["AC-01"],
            )
        harness.record_review(
            run_dir,
            "implementation_review",
            {"findings": [self.finding()]},
        )
        state = harness.complete_run(run_dir)
        self.assertEqual("completed", state["status"])
        self.assertTrue(any((run_dir / "verification/e2e").glob("acceptance-*.json")))
        result = (self.repo.task / "result.md").read_text()
        self.assertIn("Unresolved Critical/High: none", result)
        self.assertIn("Medium F-1", result)
        with self.assertRaises(harness.HarnessError):
            harness.mark_implementation_complete(run_dir)

    def test_explicit_acceptance_exception_can_replace_e2e_only(self) -> None:
        run_dir = self.prepare_completion(acceptance=False)
        harness.approve_acceptance_exception(run_dir, "Fixture has no safe external boundary.")
        state = harness.complete_run(run_dir)
        self.assertEqual("completed", state["status"])
        self.assertIn("Acceptance exception", (self.repo.task / "result.md").read_text())

    def test_skipped_plan_approval_still_invalidates_changed_plan(self) -> None:
        run_dir, _, _ = self.repo.start()
        harness.transition(run_dir, "spec_review")
        harness.record_review(run_dir, "spec_review", {"findings": []})
        harness.approve(run_dir, "spec")
        harness.transition(run_dir, "plan")
        harness.transition(run_dir, "plan_review")
        harness.record_review(run_dir, "plan_review", {"findings": []})
        with (self.repo.task / "plan.md").open("a") as stream:
            stream.write("\nChanged after review.\n")
        state = harness.refresh_state(run_dir)
        self.assertEqual("plan", state["phase"])
        self.assertNotIn("plan_review", state["reviews"])

    def test_parent_writes_planner_artifact_atomically(self) -> None:
        run_dir, _, _ = self.repo.start()
        proposal = self.repo.task / "proposal.md"
        proposal.write_text("# Revised spec\n\n- AC-01: revised.\n")
        state = harness.write_artifact(run_dir, "spec", proposal)
        self.assertEqual("spec", state["phase"])
        self.assertEqual(proposal.read_text(), (self.repo.task / "spec.md").read_text())
        self.assertEqual(
            harness.digest_file(self.repo.task / "spec.md"),
            state["artifact_hashes"]["spec"],
        )

    def test_fixture_model_less_workflow(self) -> None:
        self.repo.close()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "repo"
        shutil.copytree(FIXTURE_PATH, root)
        run("git", "init", "-q", cwd=root)
        run("git", "config", "user.email", "fixture@example.test", cwd=root)
        run("git", "config", "user.name", "Fixture", cwd=root)
        (root / ".harness").mkdir()
        shutil.copyfile(CONFIG_PATH, root / ".harness/config.toml")
        task = root / "tasks/example"
        task.joinpath("spec.md").write_text(
            """# Specification

Keep the public function and change its value.

- AC-01: value() and the CLI print 2.
"""
        )
        command = shlex.join([sys.executable, "-m", "unittest", "-v", "test_app.py"])
        task.joinpath("plan.md").write_text(
            f"""# Plan

| AC | Task | Write scope | Dependencies | Test level | Command | Environment | Evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| AC-01 | change value and test | app.py, test_app.py | none | unit and acceptance | {command} | fixture | command log |
"""
        )
        task.joinpath("backlog.md").write_text("# Backlog\n")
        run("git", "add", ".", cwd=root)
        run("git", "commit", "-qm", "fixture", cwd=root)

        run_dir, _, resumed = harness.start_run(task)
        self.assertFalse(resumed)
        harness.update_discussion(
            run_dir,
            {
                "issues": [
                    {
                        "id": "value",
                        "category": "recommended",
                        "question": "Use the requested value?",
                        "recommendation": "Return 2",
                        "alternatives": [],
                        "tradeoffs": "None",
                        "constraints": ["Preserve value()"],
                        "backlog_candidate": False,
                    }
                ]
            },
        )
        harness.update_discussion(run_dir, {"action": "adopt_recommendations"})
        harness.transition(run_dir, "spec_review")
        harness.record_review(run_dir, "spec_review", {"findings": []})
        harness.approve(run_dir, "spec")
        harness.transition(run_dir, "plan")
        harness.transition(run_dir, "plan_review")
        harness.record_review(run_dir, "plan_review", {"findings": []})
        harness.transition(run_dir, "baseline_verification")
        baseline = harness.run_verification(
            run_dir,
            "baseline",
            [sys.executable, "-m", "unittest", "-v", "test_app.py"],
            selection_source="AGENTS.md",
        )
        self.assertTrue(baseline["success"])
        harness.transition(run_dir, "implementation")
        worker = harness.prepare_agent(
            run_dir,
            {
                "role": "worker",
                "assignment": "worker",
                "objective": "Implement AC-01.",
                "ac_ids": ["AC-01"],
                "read_paths": ["app.py", "test_app.py"],
                "write_scope": ["app.py", "test_app.py"],
                "dependencies": [],
                "output": "Diff and test evidence.",
                "depth": 1,
                "parent_agent_id": "parent",
            },
            "Change only app.py and test_app.py.",
        )
        root.joinpath("app.py").write_text(
            "def value():\n    return 2\n\n\nif __name__ == \"__main__\":\n    print(value())\n"
        )
        root.joinpath("test_app.py").write_text(
            "import unittest\n\nimport app\n\n\n"
            "class AppTest(unittest.TestCase):\n"
            "    def test_value(self):\n"
            "        self.assertEqual(2, app.value())\n\n\n"
            "if __name__ == \"__main__\":\n"
            "    unittest.main()\n"
        )
        state = harness.finish_agent(run_dir, worker["invocation_id"], "completed")
        self.assertEqual([], state["scope_violations"])
        harness.mark_implementation_complete(run_dir)
        harness.transition(run_dir, "deterministic_verification")
        deterministic = harness.run_verification(
            run_dir,
            "deterministic",
            [sys.executable, "-m", "unittest", "-v", "test_app.py"],
            selection_source="AGENTS.md",
            ac_ids=["AC-01"],
        )
        self.assertTrue(deterministic["success"])
        harness.transition(run_dir, "acceptance_verification")
        acceptance = harness.run_verification(
            run_dir,
            "acceptance",
            [sys.executable, "-m", "unittest", "-v", "test_app.py"],
            selection_source="plan",
            ac_ids=["AC-01"],
        )
        self.assertTrue(acceptance["success"])
        harness.transition(run_dir, "implementation_review")
        harness.record_review(run_dir, "implementation_review", {"findings": []})
        state = harness.complete_run(run_dir)
        self.assertEqual("completed", state["status"])
        self.assertTrue(task.joinpath("result.md").is_file())
        status = run("git", "status", "--short", cwd=root).stdout
        self.assertNotIn(".harness/runs", status)
        self.assertIn("app.py", status)
        self.assertIn("test_app.py", status)


if __name__ == "__main__":
    unittest.main()
