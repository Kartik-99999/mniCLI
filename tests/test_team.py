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
    build_plan_prompt,
    extract_summary,
    missing_dependency,
    normalize_tasks,
    paths_overlap,
    verify_passed,
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
        self.assertIsNone(ag.extract_json("no json here"))
        self.assertIsNone(ag.extract_json('{"other": 1}', "tasks"))

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
        self.assertTrue(verify_passed(None))
        self.assertTrue(verify_passed((1, "x", "python module 'pytest' is not installed")))
        self.assertFalse(verify_passed((1, "AssertionError", "")))

    def test_final_prompt_carries_rerun_checks(self):
        from omnicli.conductor import build_final_prompt
        tasks = [Task(id="t1", title="A", assignee="a", instructions="", verify="python3 -m unittest", status="done")]
        p = build_final_prompt("g", Path("."), tasks, ["a"], {"a": {"name": "A"}}, 1, False, 3,
                               [("python3 -m unittest", (0, "OK", "")), ("npm test", (1, "x\n1 failing", ""))])
        self.assertIn("`python3 -m unittest` -> PASS", p)
        self.assertIn("`npm test` -> FAIL (exit 1): x | 1 failing", p)
        self.assertIn("cannot run commands yourself", p)

    def test_plan_prompt_lists_missing_tools(self):
        p = build_plan_prompt("g", Path("."), {"a": {"name": "A"}}, ["a"], ["python3"], 5, ["python3 -m pytest"])
        self.assertIn("NOT installed: python3 -m pytest", p)
        self.assertIn("unittest.TestCase", p)

    def test_build_command_substitutes(self):
        spec = {"bin": sys.executable, "edit": ["-p", "{prompt}", "--cwd={dir}"]}
        cmd = ag.build_command(spec, "edit", "do it", "/tmp/x")
        self.assertEqual(cmd[1:], ["-p", "do it", "--cwd=/tmp/x"])

    def test_silent_run_is_not_ok(self):
        r = ag.AgentRun("agy", 0, "  \n", "jetski: ... so it was auto-denied.", 1.0)
        self.assertFalse(r.ok)
        self.assertIn("auto-denied", r.failure_reason())
        self.assertTrue(ag.AgentRun("x", 0, "done", "", 1.0).ok)

    def test_agy_worker_gets_command_allow_list(self):
        edit = ag.BUILTIN_AGENTS["agy"]["edit"]
        settings = json.loads(edit[edit.index("--settings") + 1])
        self.assertIn("command(python3)", settings["permissions"]["allow"])
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
                                       "disabled": ["gemini"]}))
            specs = ag.load_agent_specs(cfg)
            self.assertEqual(specs["claude"]["strengths"], "custom")
            self.assertEqual(specs["claude"]["bin"], "claude")
            self.assertIn("mybot", specs)
            self.assertNotIn("gemini", specs)


class TestConductorEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "proj"
        os.environ["FAKE_STATE"] = str(self.tmp / "state")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, modes, lead=None, **cfg_kw):
        specs = {n: fake_spec(n, m) for n, m in modes.items()}
        health = {n: {"installed": True, "ok": True, "reason": "ok"} for n in modes}
        cfg = TeamConfig(goal="build the fake project", project_dir=self.proj, lead=lead, auto_approve=True, **cfg_kw)
        return Conductor(cfg, specs=specs, health=health, verbose=False)

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
        rc = c.run()
        self.assertEqual(rc, 0)
        self.assertTrue((self.proj / "a/a.txt").is_file())
        self.assertTrue((self.proj / "README.md").is_file())
        self.assertTrue(all(not t.workspace for t in c.tasks))
        worktrees = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=self.proj,
                                   capture_output=True, text=True).stdout
        self.assertEqual(worktrees.count("worktree "), 1, worktrees)

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
