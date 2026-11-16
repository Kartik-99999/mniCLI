"""
Tests for the `omnicli team` flags: --model/--effort, -c/--continue, --max-calls/--max-minutes,
--json, -q/--quiet, --add-dir and --name. Real AI CLIs are replaced by tests/fake_agent.py.
"""

import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Never let tests touch the real ~/.omnicli (ledger, run registry, health cache, config).
os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

from omnicli import agents as ag  # noqa: E402
from omnicli import cli, conductor, runs, settings  # noqa: E402
from omnicli.conductor import Conductor, TeamConfig, parse_agent_values, resolve_add_dirs  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_agent.py"))


def fake_spec(name: str, mode: str, **extra):
    spec = {"id": name, "name": f"Fake {mode}", "bin": sys.executable, "strengths": "testing",
            "readonly": [FAKE, mode, "{prompt_file}"], "edit": [FAKE, mode, "{prompt_file}"]}
    spec.update(extra)
    return spec


OPTION_ARGS = {"model_args": ["--fake-model", "{model}"], "effort_args": ["--fake-effort={effort}"],
               "add_dir_args": ["--fake-add-dir", "{add_dir}"]}


class HomeIsolated(unittest.TestCase):
    """Fresh OMNICLI_HOME + project dir per test, so the run registry and settings start empty."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="omni-flags-"))
        self._old_home = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = str(self.tmp / "home")
        os.environ["OMNICLI_FAKE_STATE"] = str(self.tmp / "fake")
        self.proj = self.tmp / "proj"

    def tearDown(self):
        os.environ["OMNICLI_HOME"] = self._old_home or ""
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, modes, lead="alpha", verbose=False, extra_spec=None, **cfg_kw):
        specs = {n: fake_spec(n, m, **(extra_spec or {})) for n, m in modes.items()}
        health = {n: {"installed": True, "ok": True, "reason": "ok"} for n in modes}
        cfg_kw.setdefault("goal", "build the fake project")
        cfg_kw.setdefault("project_dir", self.proj)
        cfg = TeamConfig(lead=lead, auto_approve=True, **cfg_kw)
        return Conductor(cfg, specs=specs, health=health, verbose=verbose)

    def factory(self, modes, captured=None, verbose=False):
        """Stand-in for conductor.Conductor inside handle_team: a real Conductor on fake agents."""
        real = conductor.Conductor

        def make(cfg):
            if captured is not None:
                captured.append(cfg)
            specs = {n: fake_spec(n, m) for n, m in modes.items()}
            health = {n: {"installed": True, "ok": True, "reason": "ok"} for n in modes}
            return real(cfg, specs=specs, health=health, verbose=verbose)
        return make

    def cli(self, argv, modes=None, captured=None):
        """Run `omnicli <argv>` with fake agents; returns (exit code, stdout)."""
        out = io.StringIO()
        with mock.patch.object(conductor, "Conductor", self.factory(modes or {"alpha": "good"}, captured)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["--no-banner"] + argv)
        return rc, out.getvalue()


# --------------------------------------------------------------------------- build_command

class TestBuildCommandOptions(unittest.TestCase):
    def spec(self, name, **vals):
        s = dict(ag.BUILTIN_AGENTS[name], id=name, bin=sys.executable)
        s.update(vals)
        return s

    def test_claude_appends_model_effort_and_add_dirs_for_workers(self):
        s = self.spec("claude", _model="opus", _effort="high", _add_dirs=["/data/a", "/data/b"])
        cmd = ag.build_command(s, "edit", "do it", "/p")
        self.assertEqual(cmd[1:3], ["-p", "do it"])
        self.assertEqual(cmd[-8:], ["--model", "opus", "--effort", "high",
                                    "--add-dir", "/data/a", "--add-dir", "/data/b"])
        lead = ag.build_command(s, "readonly", "plan it", "/p")
        self.assertEqual(lead[1:], ["-p", "plan it", "--model", "opus", "--effort", "high"])  # no add-dir for the lead

    def test_codex_options_go_before_the_positional_prompt(self):
        s = self.spec("codex", _model="gpt-5", _effort="high", _add_dirs=["/data"])
        cmd = ag.build_command(s, "edit", "PROMPT", "/p")
        self.assertEqual(cmd[-1], "PROMPT")
        self.assertIn("-m", cmd)
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5")
        self.assertIn('model_reasoning_effort="high"', cmd)
        self.assertEqual(cmd[cmd.index("--add-dir") + 1], "/data")
        self.assertLess(cmd.index("-C"), cmd.index("-m"))  # still after `exec ... -C /p`

    def test_agy_and_muse(self):
        agy = ag.build_command(self.spec("agy", _model="m1", _effort="max", _add_dirs=["/x"]), "edit", "P", "/p")
        self.assertEqual(agy[-6:], ["--model", "m1", "--effort", "max", "--add-dir", "/x"])
        muse = ag.build_command(self.spec("muse", _model="m2", _effort="low", _add_dirs=["/x"]), "edit", "P", "/p")
        self.assertEqual(muse[-5:], ["--model", "m2", "--reasoning-effort", "low", "P"])
        self.assertNotIn("--add-dir", muse)  # muse exec has no --add-dir equivalent

    def test_unsupported_adapter_gets_nothing(self):
        s = {"id": "plain", "bin": sys.executable, "edit": ["-p", "{prompt}", "--approval-mode", "auto_edit"],
             "_model": "g", "_effort": "high", "_add_dirs": ["/x"]}  # an adapter with no option flags
        self.assertEqual(ag.build_command(s, "edit", "P", "/p")[1:], ["-p", "P", "--approval-mode", "auto_edit"])
        bare = {"bin": sys.executable, "edit": ["-p", "{prompt}", "--cwd={dir}"]}
        self.assertEqual(ag.build_command(bare, "edit", "do it", "/tmp/x")[1:], ["-p", "do it", "--cwd=/tmp/x"])

    def test_explicit_kwargs_override_spec_values(self):
        s = self.spec("claude", _model="opus")
        cmd = ag.build_command(s, "readonly", "P", "/p", model="sonnet", effort="low")
        self.assertEqual(cmd[-4:], ["--model", "sonnet", "--effort", "low"])

    def test_only_verified_adapters_declare_options(self):
        b = ag.BUILTIN_AGENTS
        self.assertEqual({n for n in b if b[n].get("model_args")}, {"claude", "codex", "agy", "muse"})
        self.assertEqual({n for n in b if b[n].get("effort_args")}, {"claude", "codex", "agy", "muse"})
        self.assertEqual({n for n in b if b[n].get("add_dir_args")}, {"claude", "codex", "agy"})
        for name, spec in b.items():
            blob = " ".join(spec.get("model_args", []) + spec.get("effort_args", []) + spec.get("add_dir_args", []))
            for flag in ("dangerously", "bypassPermissions", "--yolo"):
                self.assertNotIn(flag, blob, name)


# --------------------------------------------------------------------------- parsing helpers

class TestParsing(unittest.TestCase):
    def test_agent_values(self):
        self.assertEqual(parse_agent_values(["claude=opus", " codex = gpt-5 ", "agy=a=b"]),
                         {"claude": "opus", "codex": "gpt-5", "agy": "a=b"})
        self.assertEqual(parse_agent_values(None), {})
        self.assertEqual(parse_agent_values(["claude=opus", "claude=sonnet"]), {"claude": "sonnet"})  # last wins
        for bad in ("opus", "=opus", "claude=", "claude =", "my bot=x"):
            with self.assertRaises(ValueError) as cm:
                parse_agent_values([bad], "--model", "MODEL")
            self.assertIn("--model expects AGENT=MODEL", str(cm.exception))

    def test_add_dirs(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "sub").mkdir()
            got = resolve_add_dirs([str(d / "sub"), str(d / "sub") + "/", str(d / "sub" / ".." / "sub")])
            self.assertEqual(got, [str((d / "sub").resolve())])
            self.assertTrue(Path(got[0]).is_absolute())
            for bad in ("~", "/", str(Path.home().parent), str(d / "missing")):
                with self.assertRaises(ValueError):
                    resolve_add_dirs([bad])
        finally:
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- conductor options

class TestConductorAgentOptions(HomeIsolated):
    def test_spec_copies_carry_options_and_unsupported_values_warn(self):
        specs = {"alpha": fake_spec("alpha", "good", **OPTION_ARGS, effort_levels=["low", "high"]),
                 "beta": fake_spec("beta", "good")}
        before = copy.deepcopy(specs)
        builtin_before = copy.deepcopy(ag.BUILTIN_AGENTS)
        cfg = TeamConfig(goal="g", project_dir=self.proj, models={"alpha": "m1", "beta": "m2", "ghost": "x"},
                         effort={"alpha": "turbo"}, add_dirs=[str(self.tmp)])
        c = Conductor(cfg, specs=specs, health={}, verbose=False)
        self.assertEqual(c.specs["alpha"]["_model"], "m1")
        self.assertNotIn("_effort", c.specs["alpha"])  # 'turbo' is not an accepted level
        self.assertNotIn("_model", c.specs["beta"])   # beta has no model flag
        self.assertEqual(c.specs["alpha"]["_add_dirs"], [str(self.tmp.resolve())])
        self.assertNotIn("_add_dirs", c.specs["beta"])
        warns = " | ".join(c._option_warnings)
        self.assertIn("beta has no supported way to set a model", warns)
        self.assertIn("'ghost'", warns)
        self.assertIn("effort 'turbo' ignored for alpha", warns)
        self.assertEqual(specs, before)  # caller's specs untouched
        self.assertEqual(ag.BUILTIN_AGENTS, builtin_before)

        # the warnings print exactly once, when the team is chosen
        c.verbose = True
        c._health_override = {"alpha": {"ok": True}, "beta": {"ok": True}}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            c.select_team()
            c.select_team()
        self.assertEqual(out.getvalue().count("has no supported way"), 1)
        self.assertIn("beta cannot be given extra directories", out.getvalue())

    def test_model_effort_and_add_dir_reach_the_agent_process(self):
        extra = self.tmp / "shared"
        extra.mkdir()
        c = self.make({"alpha": "recorder"}, extra_spec=OPTION_ARGS, models={"alpha": "m1"},
                      effort={"alpha": "high"}, add_dirs=[str(extra)])
        self.assertEqual(c.run(), 0)
        calls = [json.loads(l) for l in (self.tmp / "fake" / "argv.jsonl").read_text().splitlines()]
        kinds = {x["kind"] for x in calls}
        self.assertTrue({"planning", "task assignment", "review", "final check"} <= kinds, kinds)
        for x in calls:
            self.assertEqual(x["args"][:3], ["--fake-model", "m1", "--fake-effort=high"])
            if x["kind"] == "task assignment":
                self.assertEqual(x["args"][3:], ["--fake-add-dir", str(extra.resolve())])
            else:
                self.assertNotIn("--fake-add-dir", x["args"])  # the read-only lead gets no extra dirs
        prompt = next((self.proj / ".omnicli/team/logs").glob("t1-alpha-1.prompt.md")).read_text()
        self.assertIn(f"extra directories you were given access to: {extra.resolve()}", prompt)


# --------------------------------------------------------------------------- budgets

class TestBudgets(HomeIsolated):
    def setUp(self):
        super().setUp()
        settings.set_value("verify_sandbox", "false")  # the outer test runner forbids nested sandbox-exec

    def test_max_calls_stops_then_resume_completes(self):
        # plan (1) + t1 worker (2) + t1 review (3) -> no call left for t2
        c = self.make({"alpha": "good"}, jobs=1, max_calls=3)
        self.assertEqual(c.run(), 3)
        self.assertEqual(c.calls, 3)
        self.assertEqual([t.status for t in c.tasks], ["done", "pending", "pending"])
        state = runs.load_state(self.proj)
        self.assertEqual(state["phase"], "stopped")
        self.assertEqual(state["pid"], 0)
        self.assertEqual([t["status"] for t in state["tasks"]], ["done", "pending", "pending"])
        self.assertFalse((self.proj / "b" / "b.txt").exists())
        self.assertEqual(runs.most_recent_run(unfinished_only=True)["dir"], str(self.proj.resolve()))

        c2 = self.make({"alpha": "good"}, goal="", resume=True, jobs=1)
        self.assertEqual(c2.run(), 0)
        self.assertEqual([t.status for t in c2.tasks], ["done", "done", "done"])
        self.assertEqual(runs.load_state(self.proj)["phase"], "complete")

    def test_budget_hit_before_review_accepts_on_verify_without_lead_call(self):
        c = self.make({"alpha": "good"}, jobs=1, max_calls=2)  # plan + t1 worker; review refused
        self.assertEqual(c.run(), 3)
        t1 = c.tasks[0]
        self.assertEqual(t1.status, "done")
        self.assertIn("budget reached", t1.review_note)
        self.assertEqual(c.calls, 2)

    def test_budget_never_fails_tasks(self):
        c = self.make({"alpha": "good", "beta": "good"}, jobs=2, max_calls=4)  # stops mid-round with 2 jobs
        self.assertEqual(c.run(), 3)
        self.assertNotIn("failed", [t.status for t in c.tasks])
        self.assertNotIn("skipped", [t.status for t in c.tasks])
        self.assertLessEqual(c.calls, 4)

    def test_max_minutes_stops_before_planning_then_resume_plans(self):
        c = self.make({"alpha": "good"}, max_minutes=1e-9)
        self.assertEqual(c.run(), 3)
        self.assertEqual((c.calls, c.tasks), (0, []))
        self.assertIn("minutes", c.budget_reason)
        self.assertEqual(runs.load_state(self.proj)["phase"], "stopped")
        c2 = self.make({"alpha": "good"}, goal="", resume=True)
        self.assertEqual(c2.run(), 0)
        self.assertEqual(c2.goal, "build the fake project")

    def test_budget_message_names_resume_command(self):
        c = self.make({"alpha": "good"}, verbose=True, max_calls=1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(c.run(), 3)
        self.assertIn("Budget reached (1/1 AI calls); progress saved", out.getvalue())
        self.assertIn(f"omnicli resume {self.proj.resolve()}", out.getvalue())

    def test_settings_fallback_for_budgets(self):
        settings.set_value("max_calls", "7")
        captured = []
        self.cli(["team", "g", "-d", str(self.proj), "--plan-only"], captured=captured)
        self.assertEqual(captured[0].max_calls, 7)
        self.cli(["team", "g", "-d", str(self.proj), "--plan-only", "--max-calls", "0"], captured=captured)
        self.assertEqual(captured[1].max_calls, 0)
        rc, out = self.cli(["team", "g", "-d", str(self.proj), "--max-calls", "-1"])
        self.assertEqual(rc, 1)
        self.assertIn("must be 0", out)


# --------------------------------------------------------------------------- cli flags

class TestCliFlags(HomeIsolated):
    def test_models_merge_over_settings_and_cli_wins(self):
        settings.set_value("models.claude", "opus")
        settings.set_value("models.codex", "gpt-5")
        settings.set_value("effort.agy", "high")
        captured = []
        rc, _ = self.cli(["team", "g", "-d", str(self.proj), "--plan-only", "--model", "claude=sonnet",
                          "--model", "muse=m", "--effort", "claude=max"], captured=captured)
        self.assertEqual(rc, 0)
        cfg = captured[0]
        self.assertEqual(cfg.models, {"claude": "sonnet", "codex": "gpt-5", "muse": "m"})
        self.assertEqual(cfg.effort, {"agy": "high", "claude": "max"})

    def test_bad_agent_value_is_a_friendly_error(self):
        captured = []
        rc, out = self.cli(["team", "g", "-d", str(self.proj), "--model", "opus"], captured=captured)
        self.assertEqual(rc, 1)
        self.assertIn("--model expects AGENT=MODEL", out)
        self.assertEqual(captured, [])  # nothing started
        rc, out = self.cli(["team", "g", "-d", str(self.proj), "--effort", "claude="])
        self.assertEqual(rc, 1)
        self.assertIn("--effort expects AGENT=LEVEL", out)

    def test_add_dir_refuses_home_and_resolves_paths(self):
        rc, out = self.cli(["team", "g", "-d", str(self.proj), "--add-dir", "~"])
        self.assertEqual(rc, 1)
        self.assertIn("refusing", out)
        rc, _ = self.cli(["team", "g", "-d", str(self.proj), "--add-dir", "/"])
        self.assertEqual(rc, 1)
        (self.tmp / "lib").mkdir()
        captured = []
        old = os.getcwd()
        os.chdir(self.tmp)
        try:
            rc, _ = self.cli(["team", "g", "-d", str(self.proj), "--plan-only", "--add-dir", "lib"], captured=captured)
        finally:
            os.chdir(old)
        self.assertEqual(rc, 0)
        self.assertEqual(captured[0].add_dirs, [str((self.tmp / "lib").resolve())])

    def test_json_last_line_full_run(self):
        rc, out = self.cli(["team", "build it", "-d", str(self.proj), "-y", "--json"])
        self.assertEqual(rc, 0)
        last = out.strip().splitlines()[-1]
        data = json.loads(last)
        self.assertEqual(set(data), {"dir", "phase", "complete", "exit_code", "tasks", "report", "calls", "minutes"})
        self.assertEqual(data["dir"], str(self.proj.resolve()))
        self.assertEqual((data["phase"], data["complete"], data["exit_code"]), ("complete", True, 0))
        self.assertEqual(data["tasks"], {"done": 3, "failed": 0, "skipped": 0, "pending": 0, "total": 3})
        self.assertTrue(Path(data["report"]).exists())
        self.assertEqual(data["calls"], 8)  # plan + 3 x (task + review) + final
        self.assertIsInstance(data["minutes"], float)

    def test_json_plan_only_and_budget_stop(self):
        rc, out = self.cli(["team", "build it", "-d", str(self.proj), "--plan-only", "--json"])
        data = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((rc, data["phase"], data["complete"], data["report"], data["calls"]), (0, "planned", False, None, 1))
        self.assertEqual(data["tasks"]["pending"], 3)
        rc, out = self.cli(["team", "--resume", "-d", str(self.proj), "-y", "--json", "--max-calls", "2"])
        data = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((rc, data["phase"], data["exit_code"], data["calls"]), (3, "stopped", 3, 2))

    def test_json_on_early_error(self):
        rc, out = self.cli(["team", "-c", "--json"])
        self.assertEqual(rc, 1)
        data = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((data["phase"], data["exit_code"]), (None, 1))
        self.assertIn("No unfinished team run", data["error"])

    def test_quiet_suppresses_streamed_agent_lines(self):
        for stream, expect in ((True, True), (False, False)):
            shutil.rmtree(self.proj, ignore_errors=True)
            c = self.make({"alpha": "good"}, verbose=True, jobs=1, stream_output=stream)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(c.run(), 0)
            text = out.getvalue()
            self.assertEqual("working on t1" in text, expect, text[-2000:])
            self.assertIn("Part A", text)            # milestones still print
            self.assertIn("PROJECT COMPLETE", text)  # results still print

    def test_quiet_flag_reaches_config(self):
        captured = []
        self.cli(["team", "g", "-d", str(self.proj), "--plan-only", "-q"], captured=captured)
        self.assertFalse(captured[0].stream_output)
        self.cli(["team", "g", "-d", str(self.proj), "--plan-only"], captured=captured)
        self.assertTrue(captured[1].stream_output)

    def test_continue_resumes_most_recent_unfinished_run(self):
        older = self.tmp / "older"
        self.assertEqual(self.make({"alpha": "good"}, project_dir=older, plan_only=True).run(), 0)
        self.assertEqual(self.make({"alpha": "good"}, plan_only=True).run(), 0)  # self.proj is newest
        captured = []
        rc, out = self.cli(["team", "-c", "-y", "--json"], captured=captured)
        self.assertEqual(rc, 0, out)
        self.assertTrue(captured[0].resume)
        self.assertEqual(Path(captured[0].project_dir), self.proj.resolve())
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["phase"], "complete")
        # the newest is complete now, so -c picks the older unfinished one
        captured = []
        rc, _ = self.cli(["team", "--continue", "-y"], captured=captured)
        self.assertEqual(rc, 0)
        self.assertEqual(Path(captured[0].project_dir), older.resolve())
        # nothing unfinished left
        rc, out = self.cli(["team", "-c"])
        self.assertEqual(rc, 1)
        self.assertIn("No unfinished team run", out)

    def test_continue_rejects_goal_and_live_runs(self):
        rc, out = self.cli(["team", "-c", "build something"])
        self.assertEqual(rc, 1)
        self.assertIn("takes no goal", out)
        self.assertEqual(self.make({"alpha": "good"}, plan_only=True).run(), 0)
        runs.register_run({"dir": str(self.proj), "phase": "running", "pid": os.getpid()})
        captured = []
        rc, out = self.cli(["team", "-c"], captured=captured)
        self.assertEqual(rc, 1)
        self.assertIn("still running", out)
        self.assertEqual(captured, [])

    def test_name_overrides_project_name(self):
        rc, _ = self.cli(["team", "build it", "-d", str(self.proj), "--plan-only", "--name", "My Shop"])
        self.assertEqual(rc, 0)
        self.assertEqual(runs.load_state(self.proj)["project_name"], "My Shop")
        self.assertEqual(runs.find_run(str(self.proj))["project_name"], "My Shop")
        # resuming without --name keeps it; the report uses it
        c = self.make({"alpha": "good"}, goal="", resume=True)
        self.assertEqual(c.run(), 0)
        self.assertEqual(runs.load_state(self.proj)["project_name"], "My Shop")
        self.assertIn("# OmniCLI Team Report - My Shop", c.report_path.read_text())
        # and a later --name on resume renames it
        c = self.make({"alpha": "good"}, goal="", resume=True, plan_only=True, project_name="Renamed")
        self.assertEqual(c.run(), 0)
        c.save_state()
        self.assertEqual(runs.load_state(self.proj)["project_name"], "Renamed")


if __name__ == "__main__":
    unittest.main()
