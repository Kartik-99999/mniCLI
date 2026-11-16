"""
Tests for choosing the team: model discovery (model_catalog), menu pickers, `omnicli models`,
`omnicli setup`, chat /lead /model /workers /team, and editing who does what in a plan.
No real AI CLI runs: CLI output is injected through a fake runner.
"""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

from omnicli import model_catalog, settings  # noqa: E402
from omnicli.commands import models_cmd  # noqa: E402
from omnicli.picker import CANCEL, ask_number, choose_many, choose_one  # noqa: E402

AGY_OUT = "Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
CODEX_OUT = json.dumps({"models": [
    {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "visibility": "list", "default_reasoning_level": "medium"},
    {"slug": "secret-internal", "display_name": "Hidden", "visibility": "hide"},
]})


def scripted(answers):
    it = iter(answers)

    def _in(_p=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return _in


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="omni-home-"))
        self._old = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = str(self.home)
        self.bindir = Path(tempfile.mkdtemp(prefix="fakebins-"))
        self.out = []

    def tearDown(self):
        os.environ["OMNICLI_HOME"] = self._old or ""

    def fake_bin(self, name):
        p = self.bindir / name
        p.write_text("#!/bin/sh\necho PONG\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)

    def specs(self):
        from omnicli import agents as ag
        specs = {}
        for n in ("claude", "codex", "agy", "muse"):
            s = dict(ag.BUILTIN_AGENTS[n], id=n)
            s["bin"] = self.fake_bin(n)
            specs[n] = s
        return specs

    @staticmethod
    def runner(cmd, timeout=30):
        tool, args = os.path.basename(cmd[0]), cmd[1:]
        if tool == "agy" and args == ["models"]:
            return AGY_OUT
        if tool == "codex" and args == ["debug", "models"]:
            return CODEX_OUT
        if tool == "muse" and args[:2] == ["model-profile", "show"]:
            return "model: x" if args[2] == "muse-spark-1.3" else None
        return None

    def text(self):
        return "\n".join(map(str, self.out))


class TestCatalog(Base):
    def test_discover_each_cli(self):
        sp = self.specs()
        agy = model_catalog.discover("agy", sp["agy"], runner=self.runner)
        self.assertEqual([m["id"] for m in agy["models"]], ["gemini-3.8-flash-high", "claude-opus-4-6-thinking"])
        codex = model_catalog.discover("codex", sp["codex"], runner=self.runner)
        self.assertEqual([m["id"] for m in codex["models"]], ["gpt-6-luna"])  # hidden models left out
        self.assertIn("default effort medium", codex["models"][0]["name"])
        claude = model_catalog.discover("claude", sp["claude"], runner=self.runner)
        self.assertEqual([m["id"] for m in claude["models"]], ["fable", "opus", "sonnet"])
        self.assertTrue(claude["free_text"])

    def test_wrong_binary_never_trusted(self):
        sp = self.specs()
        sp["agy"]["bin"] = self.fake_bin("something-else")
        info = model_catalog.discover("agy", sp["agy"], runner=self.runner)
        self.assertEqual(info["models"], [])

    def test_validate(self):
        sp = self.specs()
        self.assertTrue(model_catalog.validate("muse", sp["muse"], "muse-spark-1.3", runner=self.runner))
        self.assertFalse(model_catalog.validate("muse", sp["muse"], "nope", runner=self.runner))
        self.assertTrue(model_catalog.validate("agy", sp["agy"], "gemini-3.8-flash-high", runner=self.runner))
        self.assertFalse(model_catalog.validate("agy", sp["agy"], "made-up", runner=self.runner))
        self.assertIsNone(model_catalog.validate("claude", sp["claude"], "opus", runner=self.runner))

    def test_effort_levels(self):
        sp = self.specs()
        self.assertIn("high", model_catalog.effort_levels(sp["agy"]))
        self.assertEqual(model_catalog.effort_levels({"effort_levels": ["x"]}), [])  # no flag -> no levels


class TestPicker(Base):
    def test_choose_one(self):
        opts = [("a", "A"), ("b", "B")]
        self.assertEqual(choose_one("t", opts, input_fn=scripted(["2"]), out=self.out.append), "b")
        self.assertIsNone(choose_one("t", opts, input_fn=scripted([""]), out=self.out.append))
        self.assertIs(choose_one("t", opts, input_fn=scripted(["q"]), out=self.out.append), CANCEL)
        self.assertEqual(choose_one("t", opts, input_fn=scripted(["9", "1"]), out=self.out.append), "a")
        self.assertEqual(choose_one("t", opts, allow_custom=True, input_fn=scripted(["custom-x"]),
                                    out=self.out.append), "custom-x")
        bad = lambda v: "nope" if v == "bad" else None
        self.assertEqual(choose_one("t", opts, allow_custom=True, validate=bad,
                                    input_fn=scripted(["bad", "good"]), out=self.out.append), "good")

    def test_choose_many_and_number(self):
        opts = [("a", ""), ("b", ""), ("c", "")]
        self.assertEqual(choose_many("t", opts, [], input_fn=scripted(["3,1"]), out=self.out.append), ["c", "a"])
        self.assertEqual(choose_many("t", opts, [], input_fn=scripted(["a"]), out=self.out.append), ["a", "b", "c"])
        self.assertIsNone(choose_many("t", opts, ["a"], input_fn=scripted([""]), out=self.out.append))
        self.assertEqual(ask_number("n", 2, input_fn=scripted(["x", "4"]), out=self.out.append), 4)
        self.assertIsNone(ask_number("n", 2, input_fn=scripted([""]), out=self.out.append))


class TestSetupAndPickers(Base):
    def test_pick_model_saves_and_default_clears(self):
        sp = self.specs()
        orig = model_catalog._run
        model_catalog._run = self.runner
        try:
            model_catalog.discover("agy", sp["agy"], refresh=True)
            self.assertEqual(models_cmd.pick_model("agy", sp["agy"], scripted(["2"]), self.out.append),
                             "claude-opus-4-6-thinking")
            self.assertEqual(settings.get("models.agy"), "claude-opus-4-6-thinking")
            models_cmd.pick_model("agy", sp["agy"], scripted(["3"]), self.out.append)  # "default"
            self.assertIsNone(settings.get("models.agy"))
            # a typed name the CLI doesn't know is rejected, then a listed one accepted
            self.assertEqual(models_cmd.pick_model("agy", sp["agy"], scripted(["made-up", "1"]), self.out.append),
                             "gemini-3.8-flash-high")
            self.assertIn("doesn't know a model called 'made-up'", self.text())
        finally:
            model_catalog._run = orig

    def test_full_setup_wizard(self):
        sp = self.specs()
        orig = model_catalog._run
        model_catalog._run = self.runner
        try:
            ready = ["claude", "agy", "muse"]
            answers = [
                "2",            # lead = agy
                "1,2",          # workers = claude, agy
                "1", "3",       # agy: model gemini..., effort high
                "2", "",        # claude: model opus, effort keep
                "3", "", "0", "30",  # jobs 3, max_tasks keep, max_calls 0, max_minutes 30
            ]
            rc = models_cmd.run_setup(sp, ready, {}, input_fn=scripted(answers), out=self.out.append)
            self.assertEqual(rc, 0)
            self.assertEqual(settings.get("lead"), "agy")
            self.assertEqual(settings.get("workers"), ["claude", "agy"])
            self.assertEqual(settings.get("models.agy"), "gemini-3.8-flash-high")
            self.assertEqual(settings.get("effort.agy"), "high")
            self.assertEqual(settings.get("models.claude"), "opus")
            self.assertEqual(settings.get("jobs"), 3)
            self.assertEqual(settings.get("max_minutes"), 30.0)
            self.assertIn("Your team now", self.text())
        finally:
            model_catalog._run = orig

    def test_describe_team_reflects_settings(self):
        sp = self.specs()
        settings.set_value("lead", "muse")
        settings.set_value("models.muse", "muse-spark-1.3")
        lines = "\n".join(models_cmd.describe_team(sp, ["claude", "muse"], {}))
        self.assertIn("muse", lines.splitlines()[0])
        self.assertIn("model muse-spark-1.3", lines)
        self.assertIn("Not available", lines)  # codex, agy not ready


class TestChatTeamCommands(Base):
    def session(self, inputs):
        from omnicli.commands import chat
        sp = self.specs()
        health = {n: {"installed": True, "ok": n != "codex", "reason": "not logged in"} for n in sp}
        return chat.ChatSession(project=None, input_fn=scripted(inputs), out=self.out.append, specs=sp,
                                health=health, run_cli=lambda a: 0, make_conductor=None, interactive=False)

    def test_lead_menu_switches_and_saves(self):
        s = self.session(["/lead", "2"])
        s.loop()
        self.assertEqual(settings.get("lead"), s.ready[1])
        self.assertEqual(s.lead, s.ready[1])

    def test_lead_direct_refuses_unready(self):
        s = self.session(["/lead codex"])
        s.loop()
        self.assertIn("isn't ready", self.text())
        self.assertNotEqual(settings.get("lead"), "codex")

    def test_model_for_named_agent(self):
        orig = model_catalog._run
        model_catalog._run = self.runner
        try:
            s = self.session(["/model claude", "1"])
            s.loop()
            self.assertEqual(settings.get("models.claude"), "fable")
        finally:
            model_catalog._run = orig

    def test_team_summary(self):
        s = self.session(["/team"])
        s.loop()
        self.assertIn("Lead", self.text())
        self.assertIn("Workers", self.text())

    def test_chat_brain_uses_saved_model(self):
        from omnicli import agents as ag
        settings.set_value("models.claude", "opus")
        spec = ag.with_saved_options(dict(ag.BUILTIN_AGENTS["claude"], id="claude"), "claude")
        self.assertIn("--model", ag.build_command(spec, "readonly", "hi", "/tmp"))
        self.assertIn("opus", ag.build_command(spec, "readonly", "hi", "/tmp"))


class TestPlanEditing(Base):
    def test_reassign_tasks_before_start(self):
        from omnicli.conductor import Conductor, TeamConfig, normalize_tasks
        c = Conductor(TeamConfig(goal="g", project_dir=self.home / "p"), specs={}, health={}, verbose=False)
        c.healthy, c.workers = ["claude", "agy", "muse"], ["claude", "agy"]
        c.tasks = normalize_tasks([{"id": "t1", "title": "a", "assignee": "claude"},
                                   {"id": "t2", "title": "b", "assignee": "agy"}], c.workers)
        c.tasks[0].status = "done"
        msgs = c.apply_assignments("t2=muse t1=agy t9=claude x t2=codex")
        self.assertEqual(c.tasks[1].assignee, "muse")
        self.assertIn("muse", c.workers)  # added so the task really goes to muse
        self.assertIn("t1 is already done", msgs)
        self.assertTrue(any("no task 't9'" in m for m in msgs))
        self.assertTrue(any("codex isn't ready" in m for m in msgs))

    def test_confirm_edit_then_start(self):
        from omnicli.conductor import Conductor, TeamConfig, normalize_tasks
        c = Conductor(TeamConfig(goal="g", project_dir=self.home / "q"), specs={}, health={}, verbose=False,
                      input_fn=scripted(["e", "t1=agy", "y"]))
        c.healthy, c.workers = ["claude", "agy"], ["claude"]
        c.tasks = normalize_tasks([{"id": "t1", "title": "a", "assignee": "claude"}], c.workers)
        self.assertTrue(c.confirm())
        self.assertEqual(c.tasks[0].assignee, "agy")


if __name__ == "__main__":
    unittest.main()
