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
from unittest import mock

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

from omnicli import model_catalog, settings  # noqa: E402
from omnicli.commands import models_cmd  # noqa: E402
from omnicli.picker import CANCEL, ask_number, choose_many, choose_one  # noqa: E402

AGY_OUT = "Fetching available models...\ngemini-3.8-flash-high\tGemini 3.8 Flash (High)\nclaude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)\n"
CODEX_OUT = json.dumps({"models": [
    {"slug": "gpt-6-luna", "display_name": "GPT-6-Luna", "visibility": "list", "default_reasoning_level": "medium",
     "supported_reasoning_levels": ["low", "medium", "high"]},
    {"slug": "secret-internal", "display_name": "Hidden", "visibility": "hide"},
]})
MUSE_OUT = "\n".join(json.dumps(frame) for frame in (
    {"jsonrpc": "2.0", "id": 1, "result": {"schema": "msp/v1"}},
    {"jsonrpc": "2.0", "method": "session/started", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "result": {"providerId": "meta", "profileId": None, "source": "live",
     "models": [
         {"modelId": "muse-spark-1.3", "displayLabel": "Muse Spark 1.3", "providerId": "meta",
          "isDefault": True, "variants": ["low", "medium", "high", "max"]},
         {"modelId": "muse-spark-1.3-contributor", "displayLabel": "Muse Spark 1.3 Contributor",
          "providerId": "meta", "isDefault": False, "variants": ["low", "high"]},
     ]}},
))


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
        if tool == "muse" and args == ["serve", "--no-session-log"]:
            return MUSE_OUT
        if tool == "muse" and args[:2] == ["model-profile", "show"]:
            return "model: x" if args[2] in ("muse-spark-1.3", "muse-spark-1.3-contributor") else None
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
        self.assertEqual(codex["models"][0]["effort_levels"], ["low", "medium", "high"])
        claude = model_catalog.discover("claude", sp["claude"], runner=self.runner)
        self.assertEqual([m["id"] for m in claude["models"]], ["fable", "opus", "sonnet"])
        self.assertTrue(claude["free_text"])
        muse = model_catalog.discover("muse", sp["muse"], runner=self.runner)
        self.assertEqual([m["id"] for m in muse["models"]], ["muse-spark-1.3", "muse-spark-1.3-contributor"])
        self.assertEqual(muse["source"], "muse serve model/list")
        self.assertEqual(muse["models"][0]["effort_levels"], ["low", "medium", "high", "max"])
        self.assertIn("(meta)", muse["models"][0]["name"])

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
        self.assertEqual(model_catalog.effort_levels(sp["agy"]), ["low", "medium", "high"])
        self.assertEqual(model_catalog.effort_levels(sp["agy"], "gemini-3.8-flash-high", runner=self.runner), ["high"])
        self.assertEqual(model_catalog.effort_levels(sp["muse"], "muse-spark-1.3", runner=self.runner),
                         ["low", "medium", "high", "max"])
        self.assertEqual(model_catalog.effort_levels(sp["muse"], "muse-spark-1.3-contributor", runner=self.runner),
                         ["low", "high"])
        self.assertEqual(model_catalog.effort_levels({"effort_levels": ["x"]}), [])  # no flag -> no levels

    def test_saved_effort_is_checked_against_cached_model_catalog(self):
        from omnicli import agents
        sp = self.specs()
        model_catalog._save_cache({"codex": {"at": model_catalog.time.time(), "data": {"models": [
            {"id": "gpt-6-luna", "name": "GPT-6-Luna", "effort_levels": ["low", "medium"]}]}}})
        safe = agents.with_options(sp["codex"], model="gpt-6-luna", effort="medium")
        stale = agents.with_options(sp["codex"], model="gpt-6-luna", effort="high")
        self.assertEqual(safe.get("_effort"), "medium")
        self.assertNotIn("_effort", stale)

    def test_unverified_codex_model_uses_cli_default_effort(self):
        from omnicli import agents
        spec = self.specs()["codex"]
        spec["id"] = "codex"
        self.assertNotIn("_effort", agents.with_options(spec, model="unlisted-model", effort="high"))

    def test_muse_ignores_legacy_cache_and_refreshes_live_catalog(self):
        sp = self.specs()
        model_catalog._save_cache({"muse": {"at": 9999999999, "data": {
            "models": [{"id": "old-model", "name": "Old cached model"}],
            "source": "Muse settings",
        }}})
        live = {"models": [{"modelId": "new-model", "displayLabel": "New model",
                            "providerId": "meta", "variants": ["low", "high"]}]}
        with mock.patch.object(model_catalog, "_muse_current", return_value=None), \
                mock.patch.object(model_catalog, "_muse_catalog", return_value=live):
            info = model_catalog.discover("muse", sp["muse"])
        self.assertEqual([row["id"] for row in info["models"]], ["new-model"])
        self.assertEqual(info["source"], "muse serve model/list")

    def test_muse_does_not_cache_failed_live_catalog_read(self):
        sp = self.specs()
        with mock.patch.object(model_catalog, "_muse_current", return_value=None), \
                mock.patch.object(model_catalog, "_muse_catalog", return_value=None):
            info = model_catalog.discover("muse", sp["muse"], refresh=True)
        self.assertEqual(info["models"], [])
        self.assertNotIn("muse", model_catalog._load_cache())


class TestPicker(Base):
    def test_choose_one(self):
        opts = [("a", "A"), ("b", "B")]
        self.assertEqual(choose_one("t", opts, input_fn=scripted(["2"]), out=self.out.append), "b")
        self.out.clear()
        prompts = []
        choose_one("t", opts, current="b", input_fn=lambda p: prompts.append(p) or "",
                   out=self.out.append)
        self.assertTrue(any("✓ current" in line for line in self.out))
        self.assertFalse(any("← current" in line for line in self.out))
        self.assertIn("Type a number", prompts[0])
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

    def test_lead_model_can_differ_from_same_cli_worker_model(self):
        sp = self.specs()
        settings.set_value("models.claude", "sonnet")
        orig = model_catalog._run
        model_catalog._run = self.runner
        try:
            self.assertEqual(models_cmd.pick_model("claude", sp["claude"], scripted(["2"]), self.out.append,
                                                   setting_key="lead_models", role_label="lead", inherit_worker=True),
                             "opus")
        finally:
            model_catalog._run = orig
        self.assertEqual(settings.get("models.claude"), "sonnet")
        self.assertEqual(settings.get("lead_models.claude"), "opus")

    def test_effort_picker_uses_selected_muse_models_supported_levels(self):
        sp = self.specs()
        catalog = {"models": [{"id": "muse-spark-1.3-contributor", "effort_levels": ["low", "high"]}]}
        with mock.patch.object(model_catalog, "discover", return_value=catalog):
            choice = models_cmd.pick_effort("muse", sp["muse"], scripted(["2"]), self.out.append,
                                            save=False, model="muse-spark-1.3-contributor")
        self.assertEqual(choice, "high")
        output = "\n".join(self.out)
        self.assertIn("1) low", output)
        self.assertIn("2) high", output)

    def test_effort_picker_clears_saved_tier_not_supported_by_selected_model(self):
        sp = self.specs()
        settings.set_value("effort.muse", "medium")
        catalog = {"models": [{"id": "muse-spark-1.3-contributor", "effort_levels": ["low", "high"]}]}
        with mock.patch.object(model_catalog, "discover", return_value=catalog):
            choice = models_cmd.pick_effort("muse", sp["muse"], scripted([""]), self.out.append,
                                            save=True, model="muse-spark-1.3-contributor")
        self.assertEqual(choice, "default")
        self.assertIsNone(settings.get("effort.muse"))
        self.assertIn("isn't supported by this model", self.text())

    def test_lead_effort_picker_saves_a_separate_role_setting(self):
        sp = self.specs()
        with mock.patch.object(model_catalog, "discover", return_value={"models": [
                {"id": "gpt-6-luna", "effort_levels": ["low", "high"]}]}):
            choice = models_cmd.pick_effort("codex", sp["codex"], scripted(["2"]), self.out.append,
                                            model="gpt-6-luna", setting_key="lead_effort",
                                            role_label="lead planning and review")
        self.assertEqual(choice, "high")
        self.assertEqual(settings.get("lead_effort.codex"), "high")
        self.assertIsNone(settings.get("effort.codex"))

    def test_models_lists_every_discovered_model(self):
        from types import SimpleNamespace
        from contextlib import redirect_stdout
        import io
        from omnicli import agents
        sp = self.specs()
        rows = [{"id": "muse-model-{}".format(i), "name": "Muse model {}".format(i)} for i in range(45)]
        buf = io.StringIO()
        with mock.patch.object(agents, "load_agent_specs", return_value={"muse": sp["muse"]}), \
                mock.patch.object(model_catalog, "discover", return_value={"models": rows, "cli_default": None,
                                                                          "source": "test", "note": ""}), \
                redirect_stdout(buf):
            self.assertEqual(models_cmd.handle_models(SimpleNamespace(agent="muse", refresh=False, json=False)), 0)
        self.assertIn("muse-model-44", buf.getvalue())

    def test_full_setup_wizard(self):
        sp = self.specs()
        orig = model_catalog._run
        model_catalog._run = self.runner
        try:
            ready = ["claude", "agy", "muse"]
            answers = [
                "2",                  # lead = agy
                "1,2",                # workers = claude, agy
                "2", "",             # claude worker: model opus, effort keep
                "1", "1",             # agy worker: model and fixed high tier
                "2",                  # agy lead: different model (Claude Opus via agy)
                "3",                  # separate lead effort: high
                "3", "", "0", "30",  # jobs 3, max_tasks keep, max_calls 0, max_minutes 30
            ]
            rc = models_cmd.run_setup(sp, ready, {}, input_fn=scripted(answers), out=self.out.append)
            self.assertEqual(rc, 0)
            self.assertEqual(settings.get("lead"), "agy")
            self.assertEqual(settings.get("workers"), ["claude", "agy"])
            self.assertEqual(settings.get("models.agy"), "gemini-3.8-flash-high")
            self.assertEqual(settings.get("lead_models.agy"), "claude-opus-4-6-thinking")
            self.assertEqual(settings.get("effort.agy"), "high")
            self.assertEqual(settings.get("lead_effort.agy"), "high")
            self.assertEqual(settings.get("models.claude"), "opus")
            self.assertEqual(settings.get("jobs"), 3)
            self.assertEqual(settings.get("max_minutes"), 30.0)
            self.assertIn("Your team now", self.text())
        finally:
            model_catalog._run = orig

    def test_custom_team_saves_roster_and_configures_each_cli_model(self):
        sp = self.specs()
        with mock.patch.object(models_cmd, "pick_lead", return_value="agy"), \
                mock.patch.object(models_cmd, "pick_workers", return_value=["claude", "muse"]), \
                mock.patch.object(models_cmd, "pick_model", return_value=None) as pick_model, \
                mock.patch.object(models_cmd, "pick_effort", return_value=None):
            rc = models_cmd.run_custom_team(sp, ["claude", "agy", "muse"], {},
                                            input_fn=scripted([]), out=self.out.append)
        self.assertEqual(rc, 0)
        self.assertEqual(settings.get("lead"), "agy")
        self.assertEqual(settings.get("workers"), ["claude", "muse"])
        self.assertEqual([call.args[0] for call in pick_model.call_args_list], ["claude", "muse", "agy"])
        self.assertEqual(pick_model.call_args_list[-1].kwargs["setting_key"], "lead_models")
        self.assertIn("worker/lead model choices", self.text())

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

    def test_lead_model_command_sets_override_without_changing_worker_model(self):
        orig = model_catalog._run
        model_catalog._run = self.runner
        settings.set_value("models.claude", "sonnet")
        try:
            s = self.session(["/lead-model claude", "2"])
            s.loop()
        finally:
            model_catalog._run = orig
        self.assertEqual(settings.get("models.claude"), "sonnet")
        self.assertEqual(settings.get("lead_models.claude"), "opus")

    def test_lead_effort_command_saves_a_separate_role_setting(self):
        settings.set_value("models.claude", "sonnet")
        with mock.patch.object(model_catalog, "effort_levels", return_value=["low", "high"]):
            s = self.session(["/lead-effort claude", "2"])
            s.loop()
        self.assertEqual(settings.get("lead_effort.claude"), "high")
        self.assertIsNone(settings.get("effort.claude"))

    def test_team_summary(self):
        s = self.session(["/team"])
        s.loop()
        self.assertIn("Lead", self.text())
        self.assertIn("Workers", self.text())

    def test_team_custom_and_named_profiles_work_in_chat(self):
        from omnicli import profiles
        s = self.session([])
        with mock.patch("omnicli.commands.models_cmd.run_custom_team", return_value=0) as custom:
            s.team_command("team", ["custom"])
        custom.assert_called_once()

        settings.set_value("lead", "claude")
        settings.set_value("workers", "claude,muse")
        settings.set_value("models.claude", "opus")
        settings.set_value("lead_models.claude", "sonnet")
        s.team_command("team", ["save", "frontend"])
        settings.set_value("lead", "muse")
        settings.set_value("workers", "agy")
        settings.unset("models.claude")
        settings.unset("lead_models.claude")
        s.team_command("team", ["use", "frontend"])
        self.assertEqual(settings.get("lead"), "claude")
        self.assertEqual(settings.get("workers"), ["claude", "muse"])
        self.assertEqual(settings.get("models.claude"), "opus")
        self.assertEqual(settings.get("lead_models.claude"), "sonnet")
        self.assertEqual(s.lead, "claude")
        s.team_command("team", ["list"])
        self.assertIn("frontend", self.text())
        self.assertIn("frontend", profiles.load_all())

    def test_chat_brain_uses_saved_model(self):
        from omnicli import agents as ag
        settings.set_value("models.claude", "opus")
        spec = ag.with_saved_options(dict(ag.BUILTIN_AGENTS["claude"], id="claude"), "claude")
        self.assertIn("--model", ag.build_command(spec, "readonly", "hi", "/tmp"))
        self.assertIn("opus", ag.build_command(spec, "readonly", "hi", "/tmp"))

    def test_chat_lead_uses_role_model_override(self):
        from omnicli import agents as ag
        settings.set_value("models.claude", "sonnet")
        settings.set_value("lead_models.claude", "opus")
        spec = ag.with_saved_lead_options(dict(ag.BUILTIN_AGENTS["claude"], id="claude"), "claude")
        self.assertEqual(ag.build_command(spec, "readonly", "hi", "/tmp")[-2:], ["--model", "opus"])
        worker = ag.with_saved_options(dict(ag.BUILTIN_AGENTS["claude"], id="claude"), "claude")
        self.assertEqual(ag.build_command(worker, "edit", "hi", "/tmp")[-2:], ["--model", "sonnet"])
        settings.set_value("lead_models.claude", "default")
        default_lead = ag.with_saved_lead_options(dict(ag.BUILTIN_AGENTS["claude"], id="claude"), "claude")
        self.assertNotIn("--model", ag.build_command(default_lead, "readonly", "hi", "/tmp"))

    def test_lead_effort_is_separate_and_different_model_uses_cli_default(self):
        from omnicli import agents as ag
        settings.set_value("models.claude", "sonnet")
        settings.set_value("lead_models.claude", "opus")
        settings.set_value("effort.claude", "high")
        spec = ag.with_saved_lead_options(dict(ag.BUILTIN_AGENTS["claude"], id="claude"), "claude")
        self.assertNotIn("--effort", ag.build_command(spec, "readonly", "hi", "/tmp"))
        settings.set_value("lead_effort.claude", "max")
        spec = ag.with_saved_lead_options(dict(ag.BUILTIN_AGENTS["claude"], id="claude"), "claude")
        self.assertEqual(ag.build_command(spec, "readonly", "hi", "/tmp")[-4:],
                         ["--model", "opus", "--effort", "max"])


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
