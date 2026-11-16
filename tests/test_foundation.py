"""
Tests for the shared foundation used by the Claude-style commands:
paths (OMNICLI_HOME), usage ledger, run registry, settings, command plugin loader,
and the run phase/registry bookkeeping inside the conductor.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

from omnicli import ledger, paths, runs, settings  # noqa: E402
from omnicli.conductor import Conductor, TeamConfig  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_agent.py"))


class FoundationBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="omni-home-"))
        self._old = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = str(self.home)

    def tearDown(self):
        os.environ["OMNICLI_HOME"] = self._old or ""
        shutil.rmtree(self.home, ignore_errors=True)


class TestPathsAndLedger(FoundationBase):
    def test_paths_follow_env(self):
        self.assertEqual(paths.omni_home(), self.home)
        self.assertEqual(paths.ledger_path(), self.home / "ledger.jsonl")

    def test_ledger_roundtrip_and_summary(self):
        ledger.record(ledger.make_entry("claude", "readonly", {"kind": "plan", "run_dir": "/p"}, 2.0, 0, True, False, "x" * 40, "y" * 8))
        ledger.record(ledger.make_entry("agy", "edit", {"kind": "task", "run_dir": "/p", "task_id": "t1"}, 3.0, 1, False, False, "", ""))
        with open(paths.ledger_path(), "a") as fh:
            fh.write("not json\n")
        entries = ledger.read()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["est_tokens_in"], 10)
        s = ledger.summarize(entries)
        self.assertEqual(s["claude"]["ok"], 1)
        self.assertEqual(s["agy"]["failed"], 1)
        self.assertEqual(len(ledger.read(agent="agy")), 1)
        self.assertEqual(len(ledger.read(run_dir="/other")), 0)

    def test_run_agent_writes_ledger(self):
        from omnicli import agents as ag
        spec = {"id": "fake", "bin": sys.executable, "readonly": ["-c", "print('PONG')"]}
        d = tempfile.mkdtemp()
        r = ag.run_agent(spec, "ping", "readonly", d, meta={"kind": "ping"})
        self.assertTrue(r.ok)
        e = ledger.read()[-1]
        self.assertEqual((e["agent"], e["kind"], e["ok"]), ("fake", "ping", True))


class TestRunsRegistry(FoundationBase):
    def _mk(self, name, phase, goal="g"):
        d = self.home / name
        st = paths.project_state_dir(d)
        st.mkdir(parents=True)
        (st / "state.json").write_text(json.dumps({"goal": goal, "phase": phase, "tasks": []}))
        runs.register_run({"dir": str(d), "goal": goal, "project_name": name, "phase": phase})
        return d

    def test_register_list_find(self):
        a = self._mk("alpha-app", "complete", "build alpha")
        b = self._mk("beta-app", "stopped", "build beta")
        listed = runs.list_runs()
        self.assertEqual([r["dir"] for r in listed], [str(b.resolve()), str(a.resolve())])  # newest first
        self.assertEqual(runs.find_run("1")["dir"], str(b.resolve()))
        self.assertEqual(runs.find_run("alpha")["dir"], str(a.resolve()))
        self.assertEqual(runs.find_run(str(a))["dir"], str(a.resolve()))
        self.assertIsNone(runs.find_run("nope"))
        self.assertEqual(runs.most_recent_run(unfinished_only=True)["dir"], str(b.resolve()))
        self.assertEqual(runs.load_state(a)["phase"], "complete")

    def test_missing_dirs_hidden_and_forget(self):
        runs.register_run({"dir": str(self.home / "gone"), "phase": "complete"})
        self.assertEqual(runs.list_runs(), [])
        self.assertEqual(len(runs.list_runs(include_missing=True)), 1)
        self.assertTrue(runs.forget_run(self.home / "gone"))

    def test_pid_alive(self):
        self.assertTrue(runs.pid_alive(os.getpid()))
        self.assertFalse(runs.pid_alive(0))


class TestSettings(FoundationBase):
    def test_defaults_set_get_unset(self):
        self.assertEqual(settings.get("jobs"), 2)
        self.assertEqual(settings.set_value("jobs", "4"), 4)
        self.assertEqual(settings.get("jobs"), 4)
        settings.set_value("models.claude", "opus")
        self.assertEqual(settings.get("models.claude"), "opus")
        self.assertEqual(settings.set_value("workers", "claude, agy"), ["claude", "agy"])
        self.assertTrue(settings.set_value("review", "no") is False)
        self.assertTrue(settings.unset("jobs"))
        self.assertEqual(settings.get("jobs"), 2)
        self.assertEqual(settings.pick(7, "jobs"), 7)
        self.assertEqual(settings.pick(None, "jobs"), 2)

    def test_invalid_values_rejected(self):
        for key, val in [("jobs", "lots"), ("nope", "1"), ("review", "maybe"), ("models", "x"), ("jobs.x", "1")]:
            with self.assertRaises(settings.SettingsError):
                settings.set_value(key, val)


class TestCoreHardening(FoundationBase):
    def test_lead_task_ids_cannot_escape_logs_folder(self):
        from omnicli.conductor import normalize_tasks
        tasks = normalize_tasks([{"id": "../../etc/x", "title": "a"}, {"id": "ok id/1", "title": "b"},
                                 {"id": "...", "title": "c"}], ["alpha"])
        ids = [t.id for t in tasks]
        self.assertEqual(ids[0], "etc-x")
        self.assertEqual(ids[1], "ok-id-1")
        self.assertEqual(ids[2], "t3")
        for i in ids:
            self.assertNotIn("/", i)
            self.assertFalse(i.startswith("."))

    def test_output_log_is_written_while_agent_runs(self):
        from omnicli import agents as ag
        import threading, time as _t
        d = Path(tempfile.mkdtemp())
        log = d / "logs" / "t1-fake-1.md"
        code = "import time,sys\nprint('first', flush=True)\ntime.sleep(1.5)\nprint('second', flush=True)"
        spec = {"id": "fake", "bin": sys.executable, "edit": ["-c", code]}
        seen = {}
        def peek():
            _t.sleep(0.8)
            seen["mid"] = log.read_text() if log.exists() else ""
        th = threading.Thread(target=peek); th.start()
        r = ag.run_agent(spec, "go", "edit", str(d), log_path=log)
        th.join()
        self.assertTrue(r.ok)
        self.assertIn("first", seen["mid"])
        self.assertNotIn("second", seen["mid"])
        self.assertEqual(log.read_text(), "first\nsecond\n")

    def test_prompt_file_adapter_works_without_log_dir(self):
        from omnicli import agents as ag
        spec = {"id": "pf", "bin": sys.executable,
                "readonly": ["-c", "import sys; print(open(sys.argv[1]).read().strip())", "{prompt_file}"]}
        r = ag.run_agent(spec, "PONG", "readonly", tempfile.mkdtemp())
        self.assertTrue(r.ok, r.failure_reason())
        self.assertEqual(r.output.strip(), "PONG")

    def test_usage_capped_agent_is_dropped_for_the_run(self):
        os.environ["FAKE_STATE"] = str(self.home / "fake")
        proj = self.home / "capped"
        def spec(n, mode):
            return {"id": n, "name": n, "bin": sys.executable, "strengths": "t",
                    "readonly": [FAKE, mode, "{prompt_file}"], "edit": [FAKE, mode, "{prompt_file}"]}
        c = Conductor(TeamConfig(goal="g", project_dir=proj, lead="alpha", auto_approve=True, jobs=1),
                      specs={"alpha": spec("alpha", "limited"), "beta": spec("beta", "good")},
                      health={"alpha": {"installed": True, "ok": True}, "beta": {"installed": True, "ok": True}},
                      verbose=False)
        self.assertEqual(c.run(), 0)
        self.assertEqual(c.lead, "beta")
        self.assertIn("usage limit", c.down.get("alpha", ""))
        alpha_calls = [e for e in ledger.read(agent="alpha")]
        self.assertEqual(len(alpha_calls), 1, "a capped agent must not be retried in the same run")
        self.assertTrue(all(t.agent_used == "beta" for t in c.tasks))

    def test_images_native_flag_or_folder_access(self):
        from omnicli import agents as ag
        img = "/tmp/shots/a.png"
        codex = ag.option_args(dict(ag.BUILTIN_AGENTS["codex"]), "readonly", images=[img])
        self.assertIn("--image=/tmp/shots/a.png", codex)
        muse = ag.option_args(dict(ag.BUILTIN_AGENTS["muse"]), "readonly", images=[img])
        self.assertEqual(muse[-2:], ["--image", img])
        claude = ag.option_args(dict(ag.BUILTIN_AGENTS["claude"]), "readonly", images=[img])
        self.assertEqual(claude[-2:], ["--add-dir", "/tmp/shots"])  # opens it by path
        self.assertFalse(ag.BUILTIN_AGENTS["agy"].get("sees_images"))

    def test_team_images_reach_every_agent(self):
        from omnicli.conductor import Conductor, TeamConfig
        spec = {"id": "a", "bin": sys.executable, "readonly": ["{prompt}"], "image_args": ["--img", "{image}"]}
        c = Conductor(TeamConfig(goal="g", project_dir=self.home / "p", images=["/x/1.png"]),
                      specs={"a": spec}, health={"a": {"ok": True}}, verbose=False)
        self.assertEqual(c.specs["a"].get("_images"), ["/x/1.png"])

    def test_banner_never_printed_into_pipes_or_json(self):
        import contextlib, io
        from omnicli import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["config", "path"])
        self.assertNotIn("Universal", buf.getvalue())
        self.assertEqual(buf.getvalue().strip().splitlines()[-1], str(paths.settings_path()))


class TestCommandPlugins(unittest.TestCase):
    def test_agents_command_registered_and_dispatched(self):
        from omnicli.commands import command_names
        self.assertIn("agents", command_names())
        from omnicli import cli
        self.assertIn("agents", cli._known_subcommands())


class TestConductorBookkeeping(FoundationBase):
    def test_phase_and_registry_after_run(self):
        os.environ["FAKE_STATE"] = str(self.home / "fake")
        proj = self.home / "proj"
        spec = {"id": "alpha", "name": "Fake", "bin": sys.executable, "strengths": "t",
                "readonly": [FAKE, "good", "{prompt_file}"], "edit": [FAKE, "good", "{prompt_file}"]}
        c = Conductor(TeamConfig(goal="fake goal", project_dir=proj, lead="alpha", auto_approve=True),
                      specs={"alpha": spec}, health={"alpha": {"installed": True, "ok": True}}, verbose=False)
        self.assertEqual(c.run(), 0)
        state = runs.load_state(proj)
        self.assertEqual(state["phase"], "complete")
        self.assertEqual(state["pid"], 0)
        rec = runs.find_run(str(proj))
        self.assertEqual((rec["phase"], rec["tasks_done"], rec["tasks_total"]), ("complete", 3, 3))
        kinds = {e["kind"] for e in ledger.read(run_dir=str(proj.resolve()))}
        self.assertTrue({"plan", "task", "review", "final"} <= kinds, kinds)


if __name__ == "__main__":
    unittest.main()
