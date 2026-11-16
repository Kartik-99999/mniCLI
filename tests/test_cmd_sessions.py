"""
Tests for `omnicli runs`, `omnicli resume` and `omnicli continue` (omnicli/commands/sessions.py).

Real team runs are created offline: a Conductor driven by tests/fake_agent.py ("good" mode)
makes a plan with --plan-only, leaving a "planned" run in a temp run registry. The handlers
are then called with `_make_conductor` patched to inject the same fake agents.
"""

import os
import tempfile

# Never let tests touch the real ~/.omnicli (ledger, run registry, health cache, config).
os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

import argparse  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from omnicli import paths, runs, settings, ui  # noqa: E402
from omnicli.commands import sessions  # noqa: E402
from omnicli.conductor import Conductor, TeamConfig  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_agent.py"))
SPECS = {"alpha": {"id": "alpha", "name": "Fake good", "bin": sys.executable, "strengths": "t",
                   "readonly": [FAKE, "good", "{prompt_file}"], "edit": [FAKE, "good", "{prompt_file}"]}}
HEALTH = {"alpha": {"installed": True, "ok": True, "reason": "ok"}}


def parse(*argv):
    parser = argparse.ArgumentParser(prog="omnicli")
    sessions.add_parser(parser.add_subparsers(dest="subcommand"))
    return parser.parse_args(list(argv))


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class _NoRun:
    """Stands in for a Conductor when a test only cares about the TeamConfig built."""

    def __init__(self, cfg):
        self.cfg = cfg

    def run(self):
        return 0


class SessionsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="omni-sessions-")).resolve()
        self._old_home = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = str(self.tmp / "home")
        os.environ["FAKE_STATE"] = str(self.tmp / "fake-state")
        self.cfgs = []

        def factory(cfg):
            self.cfgs.append(cfg)
            return Conductor(cfg, specs=SPECS, health=HEALTH, verbose=False)

        self.factory = factory
        for p in (mock.patch.object(ui, "USE_COLOR", False), mock.patch.object(sessions, "_make_conductor", factory)):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        os.environ["OMNICLI_HOME"] = self._old_home or ""
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures
    def plan_run(self, name: str, goal: str) -> Path:
        """A real 'planned' run made by the conductor with fake agents (no work done yet)."""
        proj = self.tmp / name
        cfg = TeamConfig(goal=goal, project_dir=proj, lead="alpha", plan_only=True, use_git=False)
        self.assertEqual(Conductor(cfg, specs=SPECS, health=HEALTH, verbose=False).run(), 0)
        self.assertEqual(runs.load_state(proj)["phase"], "planned")
        return proj

    def set_phase(self, proj: Path, phase: str, pid: int) -> None:
        sp = paths.project_state_dir(proj) / "state.json"
        data = json.loads(sp.read_text())
        data.update(phase=phase, pid=pid)
        sp.write_text(json.dumps(data))
        runs.register_run({"dir": str(proj), "phase": phase, "pid": pid})

    def call(self, *argv, tty=False, answers=None):
        args = parse(*argv)
        out, err = io.StringIO(), io.StringIO()
        stdin = mock.MagicMock()
        stdin.isatty.return_value = tty
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", stdin), \
                mock.patch("builtins.input", side_effect=list(answers or [])) as inp:
            rc = args.func(args)
        self.prompts = [c.args[0] if c.args else "" for c in inp.call_args_list]
        self.err = err.getvalue()
        return rc, out.getvalue()

    def phase(self, proj: Path) -> str:
        return runs.load_state(proj)["phase"]


class TestRunsList(SessionsBase):
    def test_lists_newest_first_with_hint(self):
        a = self.plan_run("todo-app", "build a todo app")
        b = self.plan_run("weather-app", "build a weather app")
        rc, out = self.call("runs")
        self.assertEqual(rc, 0)
        self.assertIn("Resume one with: omnicli resume <#>", out)
        self.assertIn("planned", out)
        self.assertIn("0/3", out)
        self.assertLess(out.index(b.name), out.index(a.name))  # newest first
        self.assertIn("alpha", out)  # lead column

    def test_json_output(self):
        a = self.plan_run("todo-app", "build a todo app")
        b = self.plan_run("weather-app", "build a weather app")
        rc, out = self.call("runs", "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual([r["dir"] for r in data], [str(b), str(a)])
        self.assertEqual([r["index"] for r in data], [1, 2])
        self.assertEqual(data[0]["status"], "planned")
        self.assertEqual((data[0]["tasks_total"], data[0]["tasks_done"]), (3, 0))
        self.assertEqual(data[0]["goal"], "build a weather app")

    def test_marks_live_and_interrupted(self):
        live = self.plan_run("live-app", "a live one")
        dead = self.plan_run("dead-app", "an interrupted one")
        self.set_phase(live, "running", os.getpid())
        self.set_phase(dead, "running", dead_pid())
        rc, out = self.call("runs")
        self.assertEqual(rc, 0)
        self.assertIn("● running", out)
        self.assertIn("interrupted", out)
        status = {r["dir"]: r["status"] for r in json.loads(self.call("runs", "--json")[1])}
        self.assertEqual(status[str(live)], "live")
        self.assertEqual(status[str(dead)], "interrupted")

    def test_all_shows_missing_and_prune_forgets(self):
        kept = self.plan_run("kept-app", "keep me")
        runs.register_run({"dir": str(self.tmp / "gone-app"), "project_name": "gone-app", "phase": "stopped"})

        rc, out = self.call("runs")
        self.assertNotIn("gone-app", out)
        self.assertIn("1 run(s) whose folder is gone are hidden", out)

        rc, out = self.call("runs", "--all")
        self.assertIn("gone-app", out)
        self.assertIn("missing", out)
        data = json.loads(self.call("runs", "--all", "--json")[1])
        gone = [r for r in data if r["dir"].endswith("gone-app")][0]
        self.assertEqual((gone["status"], gone["index"], gone["exists"]), ("missing", None, False))
        self.assertEqual([r["index"] for r in data if r["exists"]], [1])  # numbering skips missing runs

        rc, out = self.call("runs", "--prune")
        self.assertEqual(rc, 0)
        self.assertIn("Forgot 1 run(s)", out)
        self.assertEqual([r["dir"] for r in runs.list_runs(include_missing=True)], [str(kept)])

        rc, out = self.call("runs", "--prune", "--json")  # nothing left to prune; stdout stays valid JSON
        self.assertEqual(len(json.loads(out)), 1)
        self.assertIn("Forgot 0 run(s)", self.err)

    def test_empty(self):
        rc, out = self.call("runs")
        self.assertEqual(rc, 0)
        self.assertIn("No team runs yet", out)
        self.assertEqual(json.loads(self.call("runs", "--json")[1]), [])


class TestResume(SessionsBase):
    def test_resume_by_index_completes_that_run(self):
        older = self.plan_run("todo-app", "build a todo app")
        newer = self.plan_run("weather-app", "build a weather app")
        rc, out = self.call("resume", "2")  # #2 in `omnicli runs` = the older run; git checkpoints on (default)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.phase(older), "complete")
        self.assertEqual(self.phase(newer), "planned")
        self.assertIn("Resuming team run", out)
        cfg = self.cfgs[-1]
        self.assertTrue(cfg.resume)
        self.assertEqual(cfg.goal, "")
        self.assertEqual(Path(cfg.project_dir), older)
        self.assertTrue((older / "a" / "a.txt").exists())
        self.assertTrue((older / ".git").exists())

    def test_resume_by_name_and_path(self):
        todo = self.plan_run("todo-app", "build a todo app")
        weather = self.plan_run("weather-app", "build a weather app")
        rc, _ = self.call("resume", "TODO", "--no-git")  # case-insensitive goal/name substring
        self.assertEqual(rc, 0)
        self.assertEqual(self.phase(todo), "complete")
        self.assertEqual(self.phase(weather), "planned")
        rc, _ = self.call("resume", str(weather), "--no-git")
        self.assertEqual(rc, 0)
        self.assertEqual(self.phase(weather), "complete")

    def test_unknown_refs(self):
        self.plan_run("todo-app", "build a todo app")
        rc, out = self.call("resume", "nothing-like-this")
        self.assertEqual(rc, 1)
        self.assertIn("No run matches 'nothing-like-this'", out)
        self.assertIn("omnicli runs", out)
        rc, out = self.call("resume", "9")
        self.assertEqual(rc, 1)
        self.assertIn("There is no run #9", out)
        empty = self.tmp / "empty-dir"
        empty.mkdir()
        rc, out = self.call("resume", str(empty))
        self.assertEqual(rc, 1)
        self.assertIn("No team run in", out)
        self.assertIn('omnicli team "<goal>"', out)
        self.assertEqual(self.cfgs, [])

    def test_refuses_a_run_another_process_is_running(self):
        proj = self.plan_run("busy-app", "busy")
        self.set_phase(proj, "running", os.getpid())
        rc, out = self.call("resume", str(proj))
        self.assertEqual(rc, 1)
        self.assertIn("running right now in another process", out)
        self.assertIn("omnicli status", out)
        self.assertEqual(self.cfgs, [])
        self.assertEqual(self.phase(proj), "running")

    def test_interrupted_run_is_resumed(self):
        proj = self.plan_run("crashed-app", "crashed")
        self.set_phase(proj, "running", dead_pid())
        rc, out = self.call("resume", "crashed", "--no-git")
        self.assertEqual(rc, 0, out)
        self.assertIn("interrupted", out)
        self.assertEqual(self.phase(proj), "complete")
        self.assertEqual(runs.load_state(proj)["pid"], 0)

    def test_complete_run_asks_or_needs_yes(self):
        proj = self.plan_run("done-app", "finished thing")
        self.assertEqual(self.call("resume", "1", "--no-git")[0], 0)
        self.assertEqual(self.phase(proj), "complete")
        self.cfgs.clear()

        rc, out = self.call("resume", "1")  # not a TTY, no -y: refuse
        self.assertEqual(rc, 1)
        self.assertIn("already complete", out)
        self.assertIn("-y", out)

        rc, out = self.call("resume", "1", tty=True, answers=["n"])  # TTY: ask; "n" leaves it alone
        self.assertEqual(rc, 0)
        self.assertIn("Resume it anyway?", self.prompts[0])
        self.assertEqual(self.cfgs, [])

        rc, _ = self.call("resume", "1", "--no-git", tty=True, answers=["y"])  # TTY: "y" resumes it
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.cfgs), 1)

        rc, _ = self.call("resume", "1", "-y", "--no-git")  # -y: no question asked
        self.assertEqual(rc, 0)
        self.assertEqual(self.prompts, [])
        self.assertEqual(len(self.cfgs), 2)
        self.assertEqual(self.phase(proj), "complete")

    def test_picker_numbers_unfinished_runs(self):
        older = self.plan_run("todo-app", "build a todo app")
        newer = self.plan_run("weather-app", "build a weather app")
        finished = self.plan_run("done-app", "finished thing")
        self.assertEqual(self.call("resume", str(finished), "--no-git")[0], 0)  # complete -> not offered

        rc, out = self.call("resume", "--no-git", tty=True, answers=["q"])
        self.assertEqual(rc, 0)
        self.assertIn("Cancelled", out)
        self.assertIn("Unfinished team runs", out)
        self.assertNotIn("done-app", out)
        self.assertEqual(len(self.cfgs), 1)

        rc, _ = self.call("resume", "--no-git", tty=True, answers=["7", "2"])  # bad answer, then #2 = older
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.prompts), 2)
        self.assertEqual(self.phase(older), "complete")
        self.assertEqual(self.phase(newer), "planned")

        rc, _ = self.call("resume", "--no-git", tty=True, answers=[""])  # Enter = 1 = the only one left
        self.assertEqual(rc, 0)
        self.assertEqual(self.phase(newer), "complete")

    def test_no_ref_without_tty_takes_most_recent_unfinished(self):
        older = self.plan_run("todo-app", "build a todo app")
        newer = self.plan_run("weather-app", "build a weather app")
        rc, out = self.call("resume", "--no-git")
        self.assertEqual(rc, 0)
        self.assertIn("most recent unfinished", out)
        self.assertEqual(self.phase(newer), "complete")
        self.assertEqual(self.phase(older), "planned")

    def test_nothing_to_resume(self):
        rc, out = self.call("resume", tty=True)
        self.assertEqual(rc, 1)
        self.assertIn("Nothing to resume", out)
        self.assertIn('omnicli team "<goal>"', out)

    def test_options_fall_back_to_settings(self):
        proj = self.plan_run("todo-app", "build a todo app")
        settings.set_value("jobs", "4")
        settings.set_value("retries", "0")
        settings.set_value("review", "false")
        settings.set_value("task_timeout", "7")
        with mock.patch.object(sessions, "_make_conductor", _NoRun):
            rc, _ = self.call("resume", "1", "--rounds", "3", "--no-git", "--workers", "alpha, beta", "--lead", "alpha",
                              "--plan-only", "--recheck")
        self.assertEqual(rc, 0)
        args = parse("resume", "1")
        cfg = sessions._build_config(args, proj)
        self.assertEqual((cfg.jobs, cfg.retries, cfg.review, cfg.task_timeout_min), (4, 0, False, 7.0))
        self.assertEqual((cfg.max_rounds, cfg.max_tasks, cfg.use_git, cfg.lead, cfg.workers), (2, 10, True, None, None))
        self.assertFalse(cfg.auto_approve or cfg.plan_only or cfg.recheck)
        self.assertTrue(cfg.resume)

        cfg = sessions._build_config(parse("resume", "1", "--rounds", "3", "--no-git", "--workers", "alpha, beta",
                                           "--lead", "alpha", "-j", "1", "--plan-only", "--recheck", "-y",
                                           "--no-review", "--retries", "2", "--task-timeout", "1.5"), proj)
        self.assertEqual((cfg.max_rounds, cfg.use_git, cfg.workers, cfg.lead, cfg.jobs), (3, False, ["alpha", "beta"], "alpha", 1))
        self.assertTrue(cfg.plan_only and cfg.recheck and cfg.auto_approve)
        self.assertEqual((cfg.review, cfg.retries, cfg.task_timeout_min), (False, 2, 1.5))
        settings.set_value("auto_approve", "true")
        self.assertTrue(sessions._build_config(parse("resume"), proj).auto_approve)

    def test_plan_only_shows_plan_without_running(self):
        proj = self.plan_run("todo-app", "build a todo app")
        rc, _ = self.call("resume", "1", "--plan-only")
        self.assertEqual(rc, 0)
        self.assertEqual(self.phase(proj), "planned")
        self.assertFalse((proj / "a" / "a.txt").exists())


class TestContinue(SessionsBase):
    def test_continue_picks_most_recent_unfinished(self):
        unfinished = self.plan_run("todo-app", "build a todo app")
        later = self.plan_run("weather-app", "build a weather app")
        self.assertEqual(self.call("resume", str(later), "--no-git")[0], 0)  # now the newest run, but complete
        rc, out = self.call("continue", "--no-git")
        self.assertEqual(rc, 0, out)
        self.assertEqual(Path(self.cfgs[-1].project_dir), unfinished)
        self.assertEqual(self.phase(unfinished), "complete")

    def test_continue_with_nothing_unfinished(self):
        rc, out = self.call("continue")
        self.assertEqual(rc, 1)
        self.assertIn("Nothing to resume", out)
        self.assertIn('omnicli team "<goal>"', out)
        self.assertEqual(self.cfgs, [])

    def test_continue_refuses_live_run(self):
        proj = self.plan_run("busy-app", "busy")
        self.set_phase(proj, "running", os.getpid())
        rc, out = self.call("continue")
        self.assertEqual(rc, 1)
        self.assertIn("omnicli status", out)
        self.assertEqual(self.cfgs, [])


class TestRegistration(unittest.TestCase):
    def test_names_and_cli_dispatch(self):
        from omnicli import cli
        from omnicli.commands import command_names
        self.assertTrue({"runs", "resume", "continue"} <= command_names())
        self.assertTrue({"runs", "resume", "continue"} <= cli._known_subcommands())
        home = tempfile.mkdtemp(prefix="omni-sessions-cli-")
        old = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = home
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cli.main(["--no-banner", "runs", "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(out.getvalue()), [])
        finally:
            os.environ["OMNICLI_HOME"] = old or ""
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
