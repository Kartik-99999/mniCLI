"""
Tests for `omnicli team`: the lead AI CLI plans, worker AI CLIs build, the lead reviews.
Real model CLIs are replaced by tests/fake_agent.py so the whole loop runs offline.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

# Never let tests touch the real ~/.omnicli (ledger, run registry, health cache, config).
os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

from omnicli import agents as ag
from omnicli.conductor import (
    Conductor,
    Task,
    TeamConfig,
    build_final_prompt,
    build_plan_prompt,
    build_review_prompt,
    build_worker_prompt,
    enforce_completion_gate,
    extract_summary,
    missing_dependency,
    normalize_tasks,
    paths_overlap,
    protected_scope_conflicts,
    verify_passed,
    uncommitted_paths,
)

FAKE = str(Path(__file__).with_name("fake_agent.py"))


def fake_spec(name: str, mode: str):
    return {"id": name, "name": f"Fake {mode}", "bin": sys.executable, "strengths": "testing",
            "readonly": [FAKE, mode, "{prompt_file}"], "edit": [FAKE, mode, "{prompt_file}"]}


def git_log(d: Path):
    out = subprocess.run(["git", "log", "--format=%s"], cwd=d, capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if l]


class TestHelpers(unittest.TestCase):
    def test_extract_json_variants(self):
        self.assertEqual(ag.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(ag.extract_json('Sure!\n```json\n{"tasks": [1]}\n```\nbye', "tasks"), {"tasks": [1]})
        self.assertEqual(ag.extract_json('noise {not json} then {"verdict": "approve"}', "verdict"), {"verdict": "approve"})
        self.assertEqual(
            ag.extract_json('Example: {"verdict": "revise"} Final: {"verdict": "approve", "feedback": ""}', "verdict"),
            {"verdict": "approve", "feedback": ""},
        )
        self.assertIsNone(ag.extract_json("no json here"))
        self.assertIsNone(ag.extract_json('{"other": 1}', "tasks"))

    def test_ping_uses_low_effort_when_supported(self):
        spec = dict(ag.BUILTIN_AGENTS["claude"], id="claude")
        with mock.patch.object(ag, "run_agent", return_value=ag.AgentRun("claude", 0, "PONG", "", 0.2)) as call:
            ok, _reason = ag._ping(spec, "/tmp", 30)
        self.assertTrue(ok)
        self.assertEqual(call.call_args.args[0].get("_effort"), "low")
        # Fixed-tier agy without an explicit model should not force --effort low
        agy_spec = dict(ag.BUILTIN_AGENTS["agy"], id="agy")
        with mock.patch.object(ag, "run_agent", return_value=ag.AgentRun("agy", 0, "PONG", "", 0.2)) as call:
            ok, _reason = ag._ping(agy_spec, "/tmp", 30)
        self.assertTrue(ok)
        self.assertIsNone(call.call_args.args[0].get("_effort"))

    def test_parse_quota_reset_formats(self):
        now = 1795000000.0
        self.assertIsNotNone(ag.parse_quota_reset("resets at 23:59", now=now))
        rel = ag.parse_quota_reset("limit reached, try again in 12 minutes", now=now)
        self.assertEqual(rel, now + 720)

    def test_paths_overlap(self):
        self.assertTrue(paths_overlap(["src"], ["src/app.py"]))
        self.assertTrue(paths_overlap(["src/app.py"], ["src"]))
        self.assertFalse(paths_overlap(["frontend"], ["backend"]))
        self.assertFalse(paths_overlap(["src/a"], ["src/b"]))
        self.assertTrue(paths_overlap([""], ["anything"]))  # "" == whole project

    def test_normalize_tasks_repairs_bad_plans(self):
        raw = [
            {"id": "x", "title": "A", "assignee": "nobody", "depends_on": ["y"], "owns": ["./src/", "/etc", "../up", "lib/*.py"]},
            {"id": "y", "title": "B", "assignee": "codex", "depends_on": ["x", "ghost"]},  # cycle with x + unknown dep
            {"id": "x", "title": "dup id", "assignee": "claude"},
            "garbage",
        ]
        tasks = normalize_tasks(raw, ["claude", "codex"])
        self.assertEqual([t.id for t in tasks], ["x", "y", "x-2"])
        self.assertIn(tasks[0].assignee, ["claude", "codex"])
        self.assertEqual(tasks[0].owns, ["src", "lib"])  # absolute / parent paths dropped, glob trimmed
        self.assertNotIn("ghost", tasks[1].depends_on)
        # the x <-> y cycle must be broken so everything can be scheduled
        self.assertFalse("y" in tasks[0].depends_on and "x" in tasks[1].depends_on)

    def test_normalize_tasks_accepts_human_assignment(self):
        task = normalize_tasks([{"id": "t1", "title": "Review UX", "assignee": "me"}],
                               ["codex", "human"])[0]
        self.assertEqual(task.assignee, "human")

    def test_extract_summary(self):
        self.assertEqual(extract_summary("lots of work\n=== SUMMARY ===\n- did it"), "- did it")
        self.assertIn("last line", extract_summary("a\nb\nlast line"))

    def test_missing_dependency_vs_real_failure(self):
        with tempfile.TemporaryDirectory() as d:
            proj = Path(d)
            (proj / "wordstats").mkdir()
            env = missing_dependency(1, "/usr/bin/python3: No module named pytest", proj)
            self.assertIn("pytest", env)
            # the project's own package missing is the worker's fault, not the machine's
            self.assertEqual(missing_dependency(1, "No module named 'wordstats.cli'", proj), "")
            self.assertIn("jest", missing_dependency(127, "/bin/sh: line 1: jest: command not found", proj))
            self.assertEqual(missing_dependency(127, "/bin/sh: ./run.sh: not found", proj), "")
            self.assertEqual(missing_dependency(1, "AssertionError: 3 != 4", proj), "")
            self.assertIn("sandbox", missing_dependency(
                71, "sandbox-exec: sandbox_apply: Operation not permitted", proj))
        self.assertTrue(verify_passed(None))
        self.assertTrue(verify_passed((1, "x", "python module 'pytest' is not installed")))
        self.assertFalse(verify_passed((1, "AssertionError", "")))

    def test_final_prompt_carries_rerun_checks(self):
        tasks = [Task(id="t1", title="A", assignee="a", instructions="", verify="python3 -m unittest", status="done")]
        p = build_final_prompt("g", Path("."), tasks, ["a"], {"a": {"name": "A"}}, 1, False, 3,
                               [("python3 -m unittest", (0, "OK", "")), ("npm test", (1, "x\n1 failing", ""))])
        self.assertIn("`python3 -m unittest` -> PASS", p)
        self.assertIn("`npm test` -> FAIL (exit 1): x | 1 failing", p)
        self.assertIn("cannot run commands yourself", p)
        self.assertIn("every capability and workflow", p)
        self.assertIn("placeholder controls", p)
        self.assertIn("report complete=false", p)

    def test_plan_and_task_review_prompts_require_real_user_visible_behavior(self):
        plan = build_plan_prompt("Build an editor", Path("."), {"a": {"name": "A"}}, ["a"], [], 3)
        self.assertIn("concrete, user-visible behaviors", plan)
        self.assertIn("toy demo", plan)
        review = build_review_prompt(Task("t1", "Build editor", "a", "Implement editor", acceptance="edit and save"),
                                     "Build an editor", "a", "Done", "diff", (0, "PASS", ""), 0, 1)
        self.assertIn("passing tests are not enough", review)
        self.assertIn("inert controls", review)
        self.assertIn("user-visible behavior", review)

    def test_completion_gate_rejects_unfinished_tasks_and_failed_checks(self):
        task = Task("t1", "Build editor", "a", "Implement editor", status="failed")
        result = enforce_completion_gate(
            {"complete": True, "summary": "Looks done", "remaining_issues": []},
            [task], [("npm test", (1, "1 failing", ""))])
        self.assertFalse(result["complete"])
        self.assertTrue(any("t1 (failed)" in issue for issue in result["remaining_issues"]))
        self.assertTrue(any("npm test" in issue for issue in result["remaining_issues"]))

    def test_completion_gate_allows_missing_environment_check_but_not_a_test_failure(self):
        task = Task("t1", "Build editor", "a", "Implement editor", status="done")
        result = enforce_completion_gate({"complete": True, "remaining_issues": []}, [task],
                                         [("pytest", (127, "", "pytest is not installed"))])
        self.assertTrue(result["complete"])
        result = enforce_completion_gate({"complete": True, "remaining_issues": []}, [task],
                                         [("npm test", (1, "failed", ""))])
        self.assertFalse(result["complete"])

    def test_plan_prompt_lists_missing_tools(self):
        p = build_plan_prompt("g", Path("."), {"a": {"name": "A"}}, ["a"], ["python3"], 5, ["python3 -m pytest"])
        self.assertIn("NOT installed: python3 -m pytest", p)
        self.assertIn("unittest.TestCase", p)

    def test_planning_protects_concurrent_changes_and_shows_agent_models(self):
        p = build_plan_prompt("g", Path("."), {"claude": {"name": "Claude", "_model": "opus",
                                                               "_effort": "high"}}, ["claude"], [], 3,
                              protected_paths=["omnicli/commands/office.py"])
        self.assertIn("CHANGED FILES OWNED BY THE USER OR OTHER ACTIVE AGENTS", p)
        self.assertIn("omnicli/commands/office.py", p)
        self.assertIn("worker model: opus", p)
        self.assertIn("worker effort: high", p)
        self.assertIn("visual quality as part of completion", p)
        worker = build_worker_prompt(Task("t1", "edit models", "claude", "update model discovery"), "g",
                                     Path("."), "Claude Code", "codex", [], model="opus", effort="high",
                                     protected_paths=["omnicli/commands/office.py"])
        self.assertIn("Runtime selection: model opus; reasoning effort high.", worker)
        self.assertIn("PROTECTED FILES already being edited", worker)
        self.assertIn("cohesive, finished screen", worker)
        self.assertIn("omnicli/commands/office.py", worker)

    def test_uncommitted_paths_include_untracked_work_but_skip_omnicli_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "office part.py").write_text("working", encoding="utf-8")
            (root / ".omnicli").mkdir()
            (root / ".omnicli" / "state.json").write_text("{}", encoding="utf-8")
            self.assertEqual(uncommitted_paths(root), ["office part.py"])

    def test_protected_scope_conflicts_reject_overlapping_and_whole_project_tasks(self):
        tasks = [Task("t1", "Office", "agy", "edit", owns=["src"]),
                 Task("t2", "Docs", "muse", "document", owns=["docs/README.md"]),
                 Task("t3", "Integration", "codex", "integrate", owns=[])]
        conflicts = protected_scope_conflicts(tasks, ["src/office.tsx", "office/assets/logo.svg"])
        self.assertEqual(len(conflicts), 2)
        self.assertIn("t1 owns protected path(s): src/office.tsx", conflicts)
        self.assertTrue(any("t3 has whole-project scope" in issue for issue in conflicts))

    def test_changed_files_stages_only_task_owned_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
            (root / "owned").mkdir()
            (root / "other").mkdir()
            (root / "owned" / "task.txt").write_text("before", encoding="utf-8")
            (root / "other" / "active.txt").write_text("before", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "baseline"], cwd=root, check=True)
            (root / "owned" / "task.txt").write_text("task edit", encoding="utf-8")
            (root / "other" / "active.txt").write_text("concurrent edit", encoding="utf-8")
            conductor = Conductor(TeamConfig(goal="test", project_dir=root), specs={}, verbose=False)
            self.assertEqual(conductor._changed_files(["owned"]), ["owned/task.txt"])
            staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=root,
                                    capture_output=True, text=True, check=True).stdout.splitlines()
            self.assertEqual(staged, ["owned/task.txt"])
            # Missing sibling path in `owns` must not cause git add to abort staging existing paths
            (root / "owned" / "new_file.txt").write_text("new", encoding="utf-8")
            self.assertEqual(
                conductor._changed_files(["owned/new_file.txt", "owned/not_created.txt"]),
                ["owned/new_file.txt"],
            )

    def test_build_command_substitutes(self):
        spec = {"bin": sys.executable, "edit": ["-p", "{prompt}", "--cwd={dir}"]}
        cmd = ag.build_command(spec, "edit", "do it", "/tmp/x")
        self.assertEqual(cmd[1:], ["-p", "do it", "--cwd=/tmp/x"])

    def test_silent_run_is_not_ok(self):
        r = ag.AgentRun("agy", 0, "  \n", "jetski: ... so it was auto-denied.", 1.0)
        self.assertFalse(r.ok)
        self.assertIn("auto-denied", r.failure_reason())
        diagnostic = ag.AgentRun("agy", 0, "", "read_url(example.com) was auto-denied", 1.0)
        self.assertIn("read_url(example.com)", diagnostic.failure_reason())
        self.assertTrue(ag.AgentRun("x", 0, "done", "", 1.0).ok)

    def test_agy_worker_gets_command_allow_list(self):
        edit = ag.BUILTIN_AGENTS["agy"]["edit"]
        settings = json.loads(edit[edit.index("--settings") + 1])
        self.assertEqual(edit[edit.index("--mode") + 1], "auto")
        self.assertIn("--sandbox", edit)
        self.assertIn("command(python3)", settings["permissions"]["allow"])
        self.assertIn("command(vite)", settings["permissions"]["allow"])
        self.assertIn("command(vitest)", settings["permissions"]["allow"])
        for allowed in ("command(rg)", "command(git status)", "command(git diff)", "command(git log)",
                        "command(git show)", "command(sed -n)"):
            self.assertIn(allowed, settings["permissions"]["allow"])
        self.assertNotIn("command(rm)", settings["permissions"]["allow"])

    def test_builtin_adapters_never_bypass_permissions(self):
        for name, spec in ag.BUILTIN_AGENTS.items():
            blob = " ".join(spec["edit"] + spec["readonly"])
            for flag in ("dangerously", "bypassPermissions", "--yolo"):
                self.assertNotIn(flag, blob, f"{name} adapter uses {flag}")

    def test_load_agent_specs_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "agents.json"
            cfg.write_text(json.dumps({"agents": {"claude": {"strengths": "custom"},
                                                  "mybot": {"bin": "mybot", "edit": ["{prompt}"]}},
                                       "disabled": ["agy"]}))
            specs = ag.load_agent_specs(cfg)
            self.assertEqual(specs["claude"]["strengths"], "custom")
            self.assertEqual(specs["claude"]["bin"], "claude")
            self.assertIn("mybot", specs)
            self.assertNotIn("agy", specs)


class TestConductorEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "proj"
        os.environ["OMNICLI_FAKE_STATE"] = str(self.tmp / "state")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, modes, lead=None, **cfg_kw):
        specs = {n: fake_spec(n, m) for n, m in modes.items()}
        health = {n: {"installed": True, "ok": True, "reason": "ok"} for n in modes}
        cfg = TeamConfig(goal="build the fake project", project_dir=self.proj, lead=lead, auto_approve=True, **cfg_kw)
        return Conductor(cfg, specs=specs, health=health, verbose=False)

    def test_plan_retries_when_scope_overlaps_active_untracked_work(self):
        self.proj.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=self.proj, check=True)
        (self.proj / "office.tsx").write_text("Claude is editing this", encoding="utf-8")
        c = self.make({"alpha": "good"}, lead="alpha")
        c.workers = ["alpha"]
        replies = [
            {"project_name": "demo", "workflows": [{"kind": "inspection", "scenario": "Read the result.", "check_task": ""}], "tasks": [{"id": "t1", "title": "Build", "assignee": "alpha",
                                                    "owns": ["."]}]},
            {"project_name": "demo", "workflows": [{"kind": "inspection", "scenario": "Read the result.", "check_task": ""}], "tasks": [{"id": "t1", "title": "Build", "assignee": "alpha",
                                                    "owns": ["src"]}]},
        ]
        prompts = []

        def reply(_kind, prompt, _key, **_kwargs):
            prompts.append(prompt)
            return replies.pop(0)

        with mock.patch("omnicli.conductor.tool_report", return_value=([], [])), \
                mock.patch("omnicli.conductor.ledger.skill_recommendations", return_value={}), \
                mock.patch.object(c, "ask_lead", side_effect=reply):
            self.assertTrue(c.plan())
        self.assertEqual(len(prompts), 2)
        self.assertIn("office.tsx", prompts[0])
        self.assertIn("PLAN REJECTED FOR FILE OWNERSHIP CONFLICTS", prompts[1])
        self.assertEqual(c.tasks[0].owns, ["src"])
        self.assertEqual(c.protected_paths, ["office.tsx"])

    def test_protected_changes_stay_in_main_checkout_during_worktree_checkpoint(self):
        self.proj.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=self.proj, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.proj, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.proj, check=True)
        (self.proj / "src").mkdir()
        (self.proj / "src" / "app.txt").write_text("base", encoding="utf-8")
        (self.proj / "office.tsx").write_text("base office", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.proj, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=self.proj, check=True)
        (self.proj / "office.tsx").write_text("Claude's active edit", encoding="utf-8")
        (self.proj / "office-extra.js").write_text("untracked active edit", encoding="utf-8")

        c = self.make({"alpha": "good"}, lead="alpha", use_worktrees=True)
        c.protected_paths = uncommitted_paths(self.proj)
        c.setup_git()
        self.assertTrue(c.git)
        self.assertEqual((self.proj / "office.tsx").read_text(encoding="utf-8"), "Claude's active edit")
        self.assertEqual(subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=self.proj,
                                        capture_output=True, text=True, check=True).stdout, "")

        task = Task("t1", "Build app", "alpha", "edit app", owns=["src"])
        workspace = c._task_workspace(task)
        self.assertNotEqual(workspace, self.proj)
        (workspace / "src" / "app.txt").write_text("worker edit", encoding="utf-8")
        task.agent_used = "alpha"
        task.commit = c._checkpoint_workspace(task, workspace, ["src"])
        c._remove_task_workspace(task)

        self.assertTrue(task.commit)
        self.assertEqual((self.proj / "src" / "app.txt").read_text(encoding="utf-8"), "worker edit")
        self.assertEqual((self.proj / "office.tsx").read_text(encoding="utf-8"), "Claude's active edit")
        self.assertEqual((self.proj / "office-extra.js").read_text(encoding="utf-8"), "untracked active edit")
        committed = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=self.proj,
                                   capture_output=True, text=True, check=True).stdout.splitlines()
        self.assertEqual(committed, ["src/app.txt"])

    def _repo_with_src(self):
        self.proj.mkdir(parents=True)
        for cmd in (["git", "init", "-q"], ["git", "config", "user.name", "Test"],
                    ["git", "config", "user.email", "test@example.invalid"]):
            subprocess.run(cmd, cwd=self.proj, check=True)
        (self.proj / "src").mkdir()
        (self.proj / "src" / "app.txt").write_text("base", encoding="utf-8")
        (self.proj / "README.md").write_text("original readme", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.proj, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=self.proj, check=True)

    def test_out_of_scope_changes_ignore_own_scope_other_tasks_and_build_output(self):
        self._repo_with_src()
        c = self.make({"alpha": "good"}, lead="alpha")
        c.setup_git()
        task = Task("t1", "Build app", "alpha", "edit app", owns=["src"])
        other = Task("t2", "Docs", "alpha", "docs", owns=["docs"], status="running")
        c.tasks = [task, other]
        before = c._dirty_fingerprint()
        (self.proj / "src" / "app.txt").write_text("in scope", encoding="utf-8")
        (self.proj / "docs").mkdir()
        (self.proj / "docs" / "guide.md").write_text("other task's file", encoding="utf-8")
        (self.proj / "__pycache__").mkdir()
        (self.proj / "__pycache__" / "x.cpython-39.pyc").write_bytes(b"cache")
        (self.proj / "README.md").write_text("sneaky edit", encoding="utf-8")
        (self.proj / "notes.txt").write_text("new stray file", encoding="utf-8")
        self.assertEqual(c._out_of_scope_changes(task, before), ["README.md", "notes.txt"])
        self.assertEqual(c._out_of_scope_changes(task, None), [])

    def _run_with_worker_edits(self, attempts, retries):
        """Run one scoped task in the shared checkout; attempts[i] = files the worker writes on try i."""
        self._repo_with_src()
        c = self.make({"alpha": "good"}, lead="alpha", jobs=1, retries=retries)
        c.workers = ["alpha"]
        c.setup_git()
        task = Task("t1", "Build app", "alpha", "edit app", owns=["src"])
        c.tasks = [task]
        calls = []

        def worker(spec, prompt, mode, cwd, **kwargs):
            edits = attempts[min(len(calls), len(attempts) - 1)]
            calls.append(prompt)
            for rel, text in edits.items():
                (Path(cwd) / rel).write_text(text, encoding="utf-8")
            return ag.AgentRun("alpha", 0, "SUMMARY: did it", "", 0.1)

        approve = {"verdict": "approve", "feedback": "", "summary": "looks good"}
        with mock.patch.object(ag, "run_agent", side_effect=worker), \
                mock.patch.object(c, "ask_lead", return_value=approve) as lead:
            c._run_task(task)
        return c, task, calls, lead

    def test_out_of_scope_edit_cannot_be_approved_until_undone(self):
        c, task, calls, lead = self._run_with_worker_edits(
            [{"src/app.txt": "feature", "README.md": "sneaky edit"},
             {"src/app.txt": "feature", "README.md": "original readme"}], retries=1)
        self.assertEqual(task.status, "done")
        self.assertEqual(len(calls), 2)
        self.assertIn("README.md", calls[1])                       # the revision prompt names the stray file
        self.assertIn("OUTSIDE this task's scope", lead.call_args_list[0].args[1])   # the lead was told
        self.assertEqual(task.attempts[0]["result"], "revise")

    def test_out_of_scope_edit_left_behind_fails_task_and_is_never_committed(self):
        c, task, calls, _lead = self._run_with_worker_edits(
            [{"src/app.txt": "feature", "README.md": "sneaky edit"}], retries=0)
        self.assertEqual(task.status, "failed")
        self.assertIn("README.md", task.review_note)
        self.assertIn("NOT reviewed or checkpointed", task.review_note)
        self.assertTrue(task.commit)                                # in-scope work was checkpointed...
        in_checkpoint = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=self.proj,
                                       capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(in_checkpoint, ["src/app.txt"])            # ...without the stray edit
        self.assertEqual((self.proj / "README.md").read_text(encoding="utf-8"), "sneaky edit")  # not reverted
        self.assertEqual(subprocess.run(["git", "show", "HEAD:README.md"], cwd=self.proj, capture_output=True,
                                        text=True, check=True).stdout, "original readme")

    def test_plan_editor_updates_task_fields(self):
        c = self.make({"alpha": "good", "beta": "good"}, lead="alpha")
        c.tasks = [Task("t1", "First", "alpha", "first"), Task("t2", "Second", "beta", "second")]
        messages = c.apply_plan_edits('t1 title "Revised first"\nt2 after t1\nt2 owns src/api\n'
                                      't2 verify "python3 -m unittest"')
        self.assertEqual(c.tasks[0].title, "Revised first")
        self.assertEqual(c.tasks[1].depends_on, ["t1"])
        self.assertEqual(c.tasks[1].owns, ["src/api"])
        self.assertEqual(c.tasks[1].verify, "python3 -m unittest")
        self.assertEqual(len([m for m in messages if m.startswith("updated")]), 4)
        cycle = c.apply_plan_edits("t1 after t2")
        self.assertTrue(any("cycle" in m for m in cycle))
        self.assertEqual(c.tasks[0].depends_on, [])

    def test_task_preview_approval_choices(self):
        c = self.make({"alpha": "good"}, lead="alpha")
        c.input_fn = lambda _prompt: "y"
        self.assertEqual(c.approve_task_preview(Task("t1", "One", "alpha", "do it"), "alpha", ["a.py"], "diff"),
                         ("approve", ""))
        c.input_fn = lambda _prompt: "n"
        self.assertEqual(c.approve_task_preview(Task("t1", "One", "alpha", "do it"), "alpha", [], ""),
                         ("reject", "user rejected the proposed changes"))

    def test_cross_vendor_review_skips_builder_provider(self):
        c = self.make({"alpha": "good", "beta": "good"}, lead="alpha")
        c.lead = "alpha"
        c.healthy = ["alpha", "beta"]
        c.specs["alpha"]["vendor"] = "anthropic"
        c.specs["beta"]["vendor"] = "openai"
        fake_run = ag.AgentRun("beta", 0, '{"verdict":"approve"}', "", 0.1)
        with mock.patch.object(ag, "run_agent", return_value=fake_run) as call:
            result = c.ask_lead("review", "prompt", "verdict", exclude_vendor="anthropic")
        self.assertEqual(result["verdict"], "approve")
        self.assertEqual(call.call_args.args[0]["id"], "beta")

    def test_human_worker_is_offered_to_the_lead(self):
        c = self.make({"alpha": "good"}, lead="alpha", human_worker=True)
        self.assertTrue(c.select_team())
        c.tasks = normalize_tasks([{"id": "t1", "title": "Product review", "assignee": "me"}],
                                 c.workers + ["human"])
        self.assertEqual(c.tasks[0].assignee, "human")

    def test_red_team_uses_a_different_vendor_and_records_test_pass(self):
        c = self.make({"alpha": "good", "beta": "good"}, lead="alpha", red_team=True)
        c.healthy = ["alpha", "beta"]
        c.workers = ["alpha", "beta"]
        c.specs["alpha"]["vendor"] = "anthropic"
        c.specs["beta"]["vendor"] = "openai"
        task = Task("t1", "Add API", "alpha", "Build the endpoint")
        result = ag.AgentRun("beta", 0, "Added tests", "", 0.1)
        with mock.patch.object(ag, "run_agent", return_value=result) as call:
            c._run_red_team(task, "alpha", self.proj)
        self.assertEqual(call.call_args.args[0]["id"], "beta")
        self.assertIn("Only add or change test files", call.call_args.args[1])
        self.assertEqual(task.attempts[0]["role"], "red_team")

    def test_full_run_parallel_with_checkpoints(self):
        c = self.make({"alpha": "good", "beta": "good"}, lead="alpha", jobs=2)
        rc = c.run()
        self.assertEqual(rc, 0)
        self.assertEqual([t.status for t in c.tasks], ["done", "done", "done"])
        self.assertEqual({c.tasks[0].agent_used, c.tasks[1].agent_used}, {"alpha", "beta"})
        for f in ("a/a.txt", "b/b.txt", "README.md"):
            self.assertTrue((self.proj / f).exists(), f)
        log = git_log(self.proj)
        self.assertEqual(len([l for l in log if l.startswith(("t1:", "t2:", "t3:"))]), 3, log)
        # each parallel task's checkpoint holds only its own files
        t1_files = subprocess.run(["git", "show", "--name-only", "--format=", c.tasks[0].commit],
                                  cwd=self.proj, capture_output=True, text=True).stdout.split()
        self.assertEqual(t1_files, ["a/a.txt"])
        self.assertTrue(c.report_path.exists())
        self.assertIn("COMPLETE", c.report_path.read_text())
        # orchestration logs must not be committed into the project
        self.assertNotIn(".omnicli", subprocess.run(["git", "ls-files"], cwd=self.proj,
                                                     capture_output=True, text=True).stdout)

    def test_worktree_run_merges_approved_changes_to_project(self):
        c = self.make({"alpha": "good"}, lead="alpha", use_worktrees=True, jobs=1)
        run_agent = ag.run_agent
        reviewed = []

        def inspect(spec, prompt, mode, cwd, **kwargs):
            if kwargs.get("meta", {}).get("kind") == "review":
                workspace = Path(cwd)
                self.assertNotEqual(workspace, self.proj)
                self.assertEqual(mode, "readonly")
                self.assertIn("diff --git", prompt)
                reviewed.append(workspace.name)
                if workspace.name == "t1":
                    self.assertTrue((workspace / "a/a.txt").is_file())
                    self.assertFalse((self.proj / "a/a.txt").exists())
            return run_agent(spec, prompt, mode, cwd, **kwargs)

        with mock.patch.object(ag, "run_agent", side_effect=inspect):
            rc = c.run()
        self.assertCountEqual(reviewed, ["t1", "t2", "t3"])
        self.assertEqual(rc, 0)
        self.assertTrue((self.proj / "a/a.txt").is_file())
        self.assertTrue((self.proj / "README.md").is_file())
        self.assertTrue(all(not t.workspace for t in c.tasks))
        worktrees = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=self.proj,
                                   capture_output=True, text=True).stdout
        self.assertEqual(worktrees.count("worktree "), 1, worktrees)

    def test_collect_workspace_changes_includes_code_diff(self):
        self._repo_with_src()
        c = self.make({"alpha": "good"}, lead="alpha", use_worktrees=True, jobs=1)
        c.setup_git()
        task = Task("t1", "Edit", "alpha", "edit", owns=["src"])
        ws = c._task_workspace(task)
        try:
            (ws / "src" / "app.txt").write_text("updated body\n", encoding="utf-8")
            files, diff, patch = c._collect_workspace_changes(ws, ["src"])
            self.assertEqual(files, ["src/app.txt"])
            self.assertIn("+updated body", diff)
            self.assertTrue(patch)
        finally:
            c._remove_task_workspace(task)

    def test_broken_worker_is_replaced(self):
        c = self.make({"alpha": "good", "beta": "broken"}, lead="alpha", jobs=1)
        rc = c.run()
        self.assertEqual(rc, 0)
        t2 = c.tasks[1]
        self.assertEqual(t2.status, "done")
        self.assertEqual(t2.agent_used, "alpha")
        self.assertTrue(t2.attempts[0]["result"].startswith("agent error"))

    def test_silent_exit_zero_worker_is_replaced(self):
        c = self.make({"alpha": "good", "beta": "silent"}, lead="alpha", jobs=1)
        self.assertEqual(c.run(), 0)
        t2 = c.tasks[1]
        self.assertEqual(t2.status, "done")
        self.assertEqual(t2.agent_used, "alpha")
        self.assertIn("auto-denied", t2.attempts[0]["result"])
        self.assertEqual(len([a for a in t2.attempts if a["result"] == "revise"]), 0)  # no revision wasted on it

    def test_logged_out_lead_hands_over(self):
        c = self.make({"alpha": "authfail", "beta": "good"}, lead="alpha", jobs=1)
        rc = c.run()
        self.assertEqual(rc, 0)
        self.assertEqual(c.lead, "beta")
        self.assertIn("alpha", c.down)
        self.assertTrue(all(t.agent_used == "beta" for t in c.tasks))

    def test_lead_requests_revision(self):
        c = self.make({"alpha": "picky", "beta": "good"}, lead="alpha", jobs=1)
        rc = c.run()
        self.assertEqual(rc, 0)
        first = c.tasks[0]
        self.assertEqual(len(first.attempts), 2)
        self.assertEqual(first.attempts[0]["result"], "revise")
        self.assertIn("revised", (self.proj / "a/a.txt").read_text())

    def test_failed_check_reaches_worker_before_an_optimistic_approval(self):
        c = self.make({"alpha": "good"}, lead="alpha", jobs=1, use_worktrees=True, max_rounds=1)
        plan = c.plan

        def plan_behavior_check():
            self.assertTrue(plan())
            c.tasks[0].verify = "grep -q revised a/a.txt"
            return True

        with mock.patch.object(c, "plan", side_effect=plan_behavior_check):
            self.assertEqual(c.run(), 0)
        first = c.tasks[0]
        self.assertEqual([a["result"] for a in first.attempts], ["revise", "approved"])
        self.assertIn("grep -q revised a/a.txt", first.attempts[0]["feedback"])
        self.assertIn("revised", (self.proj / "a/a.txt").read_text())

    def test_rejected_worktree_stays_available_without_merging(self):
        c = self.make({"alpha": "picky"}, lead="alpha", jobs=1, use_worktrees=True, retries=0, max_rounds=1)
        self.assertEqual(c.run(), 2)
        first = c.tasks[0]
        self.assertEqual((first.status, first.commit), ("failed", ""))
        self.assertTrue((Path(first.workspace) / "a/a.txt").is_file())
        self.assertFalse((self.proj / "a/a.txt").exists())

    def test_last_review_revision_does_not_count_as_complete_when_verify_passes(self):
        c = self.make({"alpha": "good"}, lead="alpha", jobs=1, retries=0)
        original_ask_lead = c.ask_lead

        def revise_task_review(kind, prompt, expected_key=None, **kwargs):
            if kind == "review":
                return {"verdict": "revise", "feedback": "Finish the missing user workflow.",
                        "summary": "The workflow is still missing."}
            return original_ask_lead(kind, prompt, expected_key, **kwargs)

        with mock.patch.object(c, "ask_lead", side_effect=revise_task_review):
            rc = c.run()
        self.assertEqual(rc, 2)
        self.assertTrue(any(task.status == "failed" for task in c.tasks))
        self.assertFalse(c.final["complete"])
        self.assertTrue(any("Tasks did not finish successfully" in issue
                            for issue in c.final["remaining_issues"]))

    def test_followup_round(self):
        c = self.make({"alpha": "followup", "beta": "good"}, lead="alpha", jobs=1, max_rounds=2)
        rc = c.run()
        self.assertEqual(rc, 0)
        self.assertEqual(c.round, 2)
        self.assertEqual(len(c.tasks), 4)
        self.assertEqual(c.tasks[-1].round, 2)
        self.assertEqual(c.tasks[-1].status, "done")

    def test_plan_only_then_resume(self):
        c = self.make({"alpha": "good"}, lead="alpha", plan_only=True)
        self.assertEqual(c.run(), 0)
        self.assertTrue(all(t.status == "pending" for t in c.tasks))
        self.assertFalse((self.proj / "README.md").exists())

        c2 = self.make({"alpha": "good"}, resume=True)
        c2.cfg.goal = ""
        self.assertEqual(c2.run(), 0)
        self.assertEqual(c2.goal, "build the fake project")
        self.assertTrue(all(t.status == "done" for t in c2.tasks))

    def test_failed_dependency_skips_dependents(self):
        c = self.make({"alpha": "broken"}, lead=None, jobs=1, review=False, max_rounds=1)
        # lead is broken too, so planning fails cleanly instead of hanging
        self.assertEqual(c.run(), 1)
        tasks = normalize_tasks([{"id": "a", "title": "A"}, {"id": "b", "title": "B", "depends_on": ["a"]}], ["alpha"])
        c.tasks = tasks
        tasks[0].status = "failed"
        c._skip_blocked()
        self.assertEqual(tasks[1].status, "skipped")


if __name__ == "__main__":
    unittest.main()
