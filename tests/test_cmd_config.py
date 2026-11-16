"""Tests for `omnicli config` and `omnicli init` (omnicli/commands/config_cmd.py)."""

import os
import tempfile

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

import argparse  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from omnicli import agents as ag  # noqa: E402
from omnicli import paths, settings  # noqa: E402
from omnicli.commands import config_cmd  # noqa: E402

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omnicli")
    subs = parser.add_subparsers(dest="subcommand")
    config_cmd.add_parser(subs)
    return parser


def run(*argv, stdin=None):
    """Parse argv like the real CLI and call the handler. Returns (exit_code, plain_output)."""
    args = build_parser().parse_args(list(argv))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        if stdin is not None:
            with mock.patch("builtins.input", side_effect=stdin):
                code = args.func(args)
        else:
            code = args.func(args)
    return code, _ANSI.sub("", out.getvalue())


class ConfigBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="omni-cfg-home-"))
        self._old = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = str(self.home)
        self._editor_env = {k: os.environ.pop(k) for k in ("VISUAL", "EDITOR") if k in os.environ}

    def tearDown(self):
        os.environ["OMNICLI_HOME"] = self._old or ""
        os.environ.update(self._editor_env)
        shutil.rmtree(self.home, ignore_errors=True)

    def cfg(self):
        return json.loads(paths.settings_path().read_text())

    def write_cfg(self, text):
        paths.settings_path().parent.mkdir(parents=True, exist_ok=True)
        paths.settings_path().write_text(text)


class TestRegistration(unittest.TestCase):
    def test_names_and_parser(self):
        self.assertEqual(config_cmd.NAMES, ["config", "init"])
        p = build_parser()
        self.assertIs(p.parse_args(["config"]).func, config_cmd.handle_config)
        self.assertIs(p.parse_args(["init"]).func, config_cmd.handle_init)
        self.assertEqual(p.parse_args(["config", "ls"]).config_action, "list")


class TestConfigList(ConfigBase):
    def test_bare_config_is_list(self):
        code, out = run("config")
        self.assertEqual(code, 0)
        for key in settings.SCHEMA:
            self.assertIn(key, out)
        self.assertIn("Source", out)
        self.assertIn("(auto)", out)
        self.assertIn(str(paths.settings_path()), out)
        self.assertIn("omnicli config set <key> <value>", out)

    def test_list_shows_user_values_and_subkeys(self):
        settings.set_value("models.claude", "opus")
        settings.set_value("workers", "claude,agy")
        settings.set_value("review", "false")
        code, out = run("config", "list")
        self.assertEqual(code, 0)
        self.assertRegex(out, r"models\.claude\s+opus\s+user")
        self.assertRegex(out, r"workers\s+claude, agy\s+user")
        self.assertRegex(out, r"review\s+false\s+user")
        self.assertRegex(out, r"effort\s+\{\}\s+default")
        self.assertRegex(out, r"jobs\s+2\s+default")
        self.assertRegex(out, r"task_timeout\s+25\s+default")

    def test_list_json(self):
        settings.set_value("jobs", "4")
        settings.set_value("models.codex", "o3")
        for argv in (("config", "list", "--json"), ("config", "--json")):
            code, out = run(*argv)
            self.assertEqual(code, 0)
            data = json.loads(out)
            self.assertEqual(data["path"], str(paths.settings_path()))
            self.assertEqual(set(data["settings"]), set(settings.SCHEMA))
            self.assertEqual(data["settings"]["jobs"]["value"], 4)
            self.assertEqual(data["settings"]["jobs"]["source"], "user")
            self.assertEqual(data["settings"]["jobs"]["default"], 2)
            self.assertEqual(data["settings"]["rounds"]["source"], "default")
            self.assertEqual(data["settings"]["models"]["value"], {"codex": "o3"})
            self.assertIsNone(data["settings"]["lead"]["value"])
            self.assertEqual(data["problems"], [])

    def test_list_warns_about_broken_file(self):
        self.write_cfg("{not json")
        code, out = run("config", "list")
        self.assertEqual(code, 0)
        self.assertIn("not valid JSON", out)
        self.write_cfg(json.dumps({"jobz": 3, "jobs": "many"}))
        code, out = run("config", "list", "--json")
        probs = json.loads(out)["problems"]
        self.assertTrue(any("jobz" in p for p in probs))
        self.assertTrue(any("'jobs'" in p for p in probs))


class TestConfigGetSetUnset(ConfigBase):
    def test_get_plain_values(self):
        self.assertEqual(run("config", "get", "jobs"), (0, "2\n"))
        self.assertEqual(run("config", "get", "lead"), (0, "\n"))
        self.assertEqual(run("config", "get", "review"), (0, "true\n"))
        self.assertEqual(run("config", "get", "task_timeout"), (0, "25\n"))
        settings.set_value("workers", "claude,agy")
        self.assertEqual(run("config", "get", "workers"), (0, "claude,agy\n"))

    def test_get_dotted_and_dict(self):
        settings.set_value("models.claude", "opus")
        self.assertEqual(run("config", "get", "models.claude"), (0, "opus\n"))
        self.assertEqual(run("config", "get", "models.codex"), (0, "\n"))
        code, out = run("config", "get", "models")
        self.assertEqual((code, json.loads(out)), (0, {"claude": "opus"}))

    def test_get_unknown_key(self):
        code, out = run("config", "get", "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown setting 'nope'", out)
        self.assertIn("max_tasks", out)  # lists known keys
        code, out = run("config", "get", "jobs.x")
        self.assertEqual(code, 1)
        self.assertIn("no sub-keys", out)

    def test_set_and_pick(self):
        code, out = run("config", "set", "jobs", "4")
        self.assertEqual(code, 0)
        self.assertIn("jobs = 4 (was: 2)", out)
        self.assertEqual(self.cfg()["jobs"], 4)
        # cli.py's team command resolves every unpassed flag through settings.pick
        self.assertEqual(settings.pick(None, "jobs"), 4)
        self.assertEqual(settings.pick(7, "jobs"), 7)

    def test_set_lead_reports_previous(self):
        code, out = run("config", "set", "lead", "claude")
        self.assertEqual(code, 0)
        self.assertIn("lead = claude (was: auto)", out)
        self.assertNotIn("not a known agent", out)
        code, out = run("config", "set", "lead", "codex")
        self.assertIn("lead = codex (was: claude)", out)
        self.assertEqual(settings.get("lead"), "codex")

    def test_set_unknown_agent_warns_but_saves(self):
        code, out = run("config", "set", "workers", "claude,", "robo")
        self.assertEqual(code, 0)
        self.assertIn("not a known agent: robo", out)
        self.assertEqual(self.cfg()["workers"], ["claude", "robo"])
        code, out = run("config", "set", "lead", "robo")
        self.assertEqual(code, 0)
        self.assertIn("not a known agent: robo", out)
        self.assertEqual(settings.get("lead"), "robo")

    def test_set_custom_agent_from_agents_json_is_known(self):
        paths.agents_config_path().parent.mkdir(parents=True, exist_ok=True)
        paths.agents_config_path().write_text(json.dumps({"agents": {"robo": {"bin": "robo"}}}))
        code, out = run("config", "set", "lead", "robo")
        self.assertEqual(code, 0)
        self.assertNotIn("not a known agent", out)

    def test_set_auto_restores_default(self):
        settings.set_value("lead", "claude")
        code, out = run("config", "set", "lead", "auto")
        self.assertEqual(code, 0)
        self.assertIn("lead = auto (was: claude)", out)
        self.assertNotIn("lead", self.cfg())
        self.assertIsNone(settings.get("lead"))

    def test_set_dotted_key(self):
        code, out = run("config", "set", "effort.agy", "high")
        self.assertEqual(code, 0)
        self.assertEqual(self.cfg()["effort"], {"agy": "high"})
        self.assertEqual(settings.get("effort.agy"), "high")

    def test_set_types(self):
        run("config", "set", "review", "no")
        run("config", "set", "task_timeout", "12.5")
        self.assertEqual(self.cfg(), {"review": False, "task_timeout": 12.5})

    def test_set_invalid_values(self):
        cases = [
            (("jobs", "lots"), "whole number"),
            (("review", "maybe"), "true or false"),
            (("task_timeout", "soon"), "number"),
            (("jobs", "0"), "at least 1"),
            (("retries", "-1"), "at least 0"),
            (("models", "opus"), "per-agent"),
            (("nope", "1"), "unknown setting"),
            (("rounds.x", "1"), "no sub-keys"),
            (("workers", ","), "at least one agent"),
        ]
        for argv, msg in cases:
            with self.subTest(argv=argv):
                code, out = run("config", "set", *argv)
                self.assertEqual(code, 1)
                self.assertIn(msg, out)
        self.assertFalse(paths.settings_path().exists())

    def test_set_refuses_to_clobber_broken_file(self):
        self.write_cfg('{"jobs": 3,,}')
        code, out = run("config", "set", "jobs", "4")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", out)
        self.assertIn("omnicli config edit", out)
        self.assertEqual(paths.settings_path().read_text(), '{"jobs": 3,,}')
        code, _ = run("config", "unset", "jobs")
        self.assertEqual(code, 1)

    def test_unset(self):
        settings.set_value("jobs", "5")
        settings.set_value("models.claude", "opus")
        code, out = run("config", "unset", "jobs")
        self.assertEqual(code, 0)
        self.assertIn("removed", out)
        self.assertIn("default 2", out)
        self.assertEqual(settings.get("jobs"), 2)
        code, out = run("config", "unset", "jobs")
        self.assertEqual(code, 0)
        self.assertIn("was not set", out)
        code, out = run("config", "unset", "models.claude")
        self.assertEqual(code, 0)
        self.assertIsNone(settings.get("models.claude"))
        code, out = run("config", "unset", "bogus")
        self.assertEqual(code, 1)
        self.assertIn("unknown setting", out)

    def test_path(self):
        self.assertEqual(run("config", "path"), (0, f"{paths.settings_path()}\n"))


class TestConfigEditReset(ConfigBase):
    def test_edit_without_editor(self):
        code, out = run("config", "edit")
        self.assertEqual(code, 1)
        self.assertIn(str(paths.settings_path()), out)
        self.assertIn("EDITOR", out)

    def test_edit_creates_file_and_validates(self):
        os.environ["EDITOR"] = "myeditor --wait"
        seen = {}

        def fake_editor(cmd):
            seen["cmd"] = cmd
            seen["before"] = Path(cmd[-1]).read_text()
            Path(cmd[-1]).write_text(json.dumps({"jobs": 3, "lead": "claude"}))
            return 0

        with mock.patch.object(config_cmd, "_run_editor", side_effect=fake_editor):
            code, out = run("config", "edit")
        self.assertEqual(code, 0, out)
        self.assertEqual(seen["cmd"], ["myeditor", "--wait", str(paths.settings_path())])
        self.assertEqual(json.loads(seen["before"]), {})
        self.assertIn("is valid", out)
        self.assertEqual(settings.get("jobs"), 3)

    def test_visual_wins_over_editor(self):
        os.environ["EDITOR"] = "ed"
        os.environ["VISUAL"] = "vis"
        with mock.patch.object(config_cmd, "_run_editor", return_value=0) as m:
            run("config", "edit")
        self.assertEqual(m.call_args[0][0][0], "vis")

    def test_edit_reports_invalid_json(self):
        os.environ["EDITOR"] = "ed"
        settings.set_value("jobs", "3")

        def broken(cmd):
            Path(cmd[-1]).write_text('{"jobs": 3,')
            return 0

        with mock.patch.object(config_cmd, "_run_editor", side_effect=broken):
            code, out = run("config", "edit")
        self.assertEqual(code, 1)
        self.assertIn("not valid JSON", out)
        self.assertIn("omnicli config edit", out)

    def test_edit_reports_bad_types_and_unknown_keys(self):
        os.environ["EDITOR"] = "ed"

        def bad(cmd):
            Path(cmd[-1]).write_text(json.dumps({"jobs": "three", "workers": "claude", "colour": 1}))
            return 0

        with mock.patch.object(config_cmd, "_run_editor", side_effect=bad):
            code, out = run("config", "edit")
        self.assertEqual(code, 1)
        self.assertIn("'jobs' should be whole number", out)
        self.assertIn("'workers' should be a list", out)
        self.assertIn("unknown setting 'colour'", out)

    def test_edit_missing_editor_binary(self):
        os.environ["EDITOR"] = "no-such-editor-xyz"
        with mock.patch.object(config_cmd, "_run_editor", side_effect=FileNotFoundError("nope")):
            code, out = run("config", "edit")
        self.assertEqual(code, 1)
        self.assertIn("could not start editor", out)

    def test_reset_when_default(self):
        code, out = run("config", "reset", "-y")
        self.assertEqual(code, 0)
        self.assertIn("already default", out)

    def test_reset_confirm(self):
        settings.set_value("jobs", "4")
        code, out = run("config", "reset", stdin=["n"])
        self.assertEqual(code, 0)
        self.assertTrue(paths.settings_path().exists())
        code, out = run("config", "reset", stdin=EOFError())
        self.assertTrue(paths.settings_path().exists())
        code, out = run("config", "reset", stdin=["y"])
        self.assertEqual(code, 0)
        self.assertFalse(paths.settings_path().exists())
        self.assertEqual(settings.get("jobs"), 2)

    def test_reset_yes(self):
        settings.set_value("jobs", "4")
        with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
            code, out = run("config", "reset", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(paths.settings_path().exists())


class TestInit(ConfigBase):
    def test_init_creates_files(self):
        shutil.rmtree(self.home)  # init must create the home dir itself
        code, out = run("init")
        self.assertEqual(code, 0, out)
        cfg = self.cfg()
        self.assertEqual(set(cfg), set(config_cmd.INIT_KEYS))
        self.assertEqual(cfg["jobs"], 2)
        self.assertIsNone(cfg["lead"])
        self.assertEqual(cfg["task_timeout"], 25.0)
        agents = json.loads(paths.agents_config_path().read_text())
        self.assertEqual(agents["agents"], {})
        self.assertEqual(agents["disabled"], [])
        self.assertIn("_help", agents)
        for step in ("omnicli agents", "omnicli config list", "omnicli team"):
            self.assertIn(step, out)
        self.assertEqual(out.count("wrote"), 2)
        # what init wrote is a valid config that the team command reads back unchanged
        self.assertEqual(config_cmd.file_problems(), [])
        self.assertEqual(settings.load(), settings.defaults())

    def test_agents_template_loads_cleanly(self):
        run("init")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            specs = ag.load_agent_specs()
        self.assertEqual(out.getvalue(), "")  # no "ignoring invalid" warning
        self.assertEqual(set(specs), set(ag.BUILTIN_AGENTS))
        self.assertEqual(specs["claude"]["bin"], ag.BUILTIN_AGENTS["claude"]["bin"])
        self.assertNotIn("mybot", specs)  # the _example is documentation only

    def test_template_example_is_usable(self):
        example = config_cmd.AGENTS_TEMPLATE["_example"]
        p = self.home / "agents-example.json"
        p.write_text(json.dumps(example))
        specs = ag.load_agent_specs(p)
        self.assertIn("mybot", specs)
        self.assertNotIn("agy", specs)
        self.assertEqual(specs["claude"]["strengths"], example["agents"]["claude"]["strengths"])
        self.assertEqual(specs["claude"]["bin"], "claude")

    def test_init_is_idempotent(self):
        run("init")
        settings.set_value("jobs", "6")
        before_cfg = paths.settings_path().read_text()
        before_agents = paths.agents_config_path().read_text()
        code, out = run("init")
        self.assertEqual(code, 0)
        self.assertEqual(out.count("skipped"), 2)
        self.assertEqual(paths.settings_path().read_text(), before_cfg)
        self.assertEqual(paths.agents_config_path().read_text(), before_agents)
        self.assertEqual(list(self.home.glob("*.bak")), [])

    def test_init_skip_warns_on_broken_existing_file(self):
        self.write_cfg("nope")
        code, out = run("init")
        self.assertEqual(code, 0)
        self.assertIn("skipped", out)
        self.assertIn("not valid JSON", out)
        self.assertEqual(paths.settings_path().read_text(), "nope")

    def test_init_force_backs_up(self):
        run("init")
        settings.set_value("jobs", "6")
        custom = paths.settings_path().read_text()
        paths.agents_config_path().write_text('{"agents": {"x": {}}}')
        code, out = run("init", "--force")
        self.assertEqual(code, 0)
        bak = paths.settings_path().with_name("config.json.bak")
        self.assertEqual(bak.read_text(), custom)
        self.assertEqual(paths.agents_config_path().with_name("agents.json.bak").read_text(), '{"agents": {"x": {}}}')
        self.assertEqual(self.cfg()["jobs"], 2)
        self.assertIn("backed up", out)

        # second --force: .bak exists, so the new backup gets a timestamped name
        settings.set_value("jobs", "8")
        code, out = run("init", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(bak.read_text())["jobs"], 6)  # first backup untouched
        stamped = [p for p in self.home.glob("config.json.*.bak")]
        self.assertEqual(len(stamped), 1)
        self.assertEqual(json.loads(stamped[0].read_text())["jobs"], 8)
        self.assertEqual(len(list(self.home.glob("agents.json.*.bak"))), 1)

    def test_backup_path_unique(self):
        p = self.home / "config.json"
        p.write_text("{}")
        b1 = config_cmd.backup_path(p)
        self.assertEqual(b1.name, "config.json.bak")
        b1.write_text("{}")
        b2 = config_cmd.backup_path(p)
        b2.write_text("{}")
        b3 = config_cmd.backup_path(p)
        self.assertEqual(len({b1, b2, b3}), 3)
        self.assertFalse(b3.exists())


if __name__ == "__main__":
    unittest.main()
