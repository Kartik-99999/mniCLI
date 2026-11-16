"""
Tests for `omnicli undo` / `omnicli rewind`: revert one team task's git checkpoint,
mark it pending and the run stopped. Uses a REAL temp git repo whose commits look like
OmniCLI checkpoints; no AI CLI is ever invoked.
"""

import os
import tempfile

# Never let tests touch the real ~/.omnicli (must happen before importing omnicli).
os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")
os.environ.setdefault("NO_COLOR", "1")

import argparse  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from omnicli import paths, runs, ui  # noqa: E402
from omnicli.commands import undo  # noqa: E402

HAS_GIT = shutil.which("git") is not None


def git(d, *args, check=True):
    r = subprocess.run(["git", *args], cwd=str(d), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def make_task(tid, title, commit, deps=(), status="done"):
    return {
        "id": tid, "title": title, "assignee": "fakebot", "instructions": f"Do: {title}",
        "depends_on": list(deps), "owns": [], "acceptance": "", "verify": "", "round": 1,
        "status": status, "agent_used": "fakebot" if commit else "", "summary": f"{title} done",
        "review_note": "", "commit": commit, "duration_sec": 12.5,
        "attempts": [{"agent": "fakebot", "revision": 0, "exit_code": 0, "duration_sec": 1.0,
                      "at": "2026-11-15T10:00:00", "result": "approved", "feedback": "",
                      "files_changed": 1, "verify_exit": None}],
    }


@unittest.skipUnless(HAS_GIT, "git not installed")
class UndoBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="omni-undo-home-"))
        self._env = mock.patch.dict(os.environ, {"OMNICLI_HOME": str(self.home)})
        self._env.start()
        self._color = ui.USE_COLOR
        ui.USE_COLOR = False
        # a known agent id for --agent (agents.json overrides are merged into the built-ins)
        (self.home / "agents.json").write_text(json.dumps(
            {"agents": {"fakebot": {"name": "Fake", "bin": "fakebot"},
                        "otherbot": {"name": "Other", "bin": "otherbot"}}}))
        self.tmp = Path(tempfile.mkdtemp(prefix="omni-undo-"))
        self.dir = (self.tmp / "proj").resolve()
        self.dir.mkdir()
        self.dir = self.dir.resolve()
        git(self.dir, "init", "-q")
        git(self.dir, "config", "user.name", "Tester")
        git(self.dir, "config", "user.email", "tester@example.com")
        git(self.dir, "config", "commit.gpgsign", "false")
        (self.dir / "README.md").write_text("# tiny\n")
        (self.dir / "config.txt").write_text("mode = base\n")
        git(self.dir, "add", "README.md", "config.txt")
        git(self.dir, "commit", "-q", "-m", "omnicli: baseline before team run")
        self.base = git(self.dir, "rev-parse", "HEAD")

        # t1: create app.py and change config.txt
        (self.dir / "app.py").write_text("x = 1\nprint(x)\n")
        (self.dir / "config.txt").write_text("mode = t1\n")
        self.c1 = self.checkpoint("t1", "Create app", ["app.py", "config.txt"])
        # t2 (depends on t1): add lib.py
        (self.dir / "lib.py").write_text("def helper():\n    return 42\n")
        self.c2 = self.checkpoint("t2", "Add helper lib", ["lib.py"])
        # t3 (depends on t2): docs
        (self.dir / "README.md").write_text("# tiny\n\nRun: python3 app.py\n")
        self.c3 = self.checkpoint("t3", "Write docs", ["README.md"])

        self.tasks = [
            make_task("t1", "Create app", self.c1),
            make_task("t2", "Add helper lib", self.c2, deps=["t1"]),
            make_task("t3", "Write docs", self.c3, deps=["t2"]),
            make_task("t4", "Broken extra", "", status="failed"),
        ]
        self.write_state()
        # .omnicli/ is deliberately NOT git-excluded: undo must ignore it by itself
        runs.register_run({"dir": str(self.dir), "goal": "Build a tiny app", "project_name": "tiny",
                           "phase": "complete", "tasks_total": 4, "tasks_done": 3, "tasks_failed": 1,
                           "pid": 0})

    def tearDown(self):
        ui.USE_COLOR = self._color
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    # ------------------------------------------------------------ fixtures
    def checkpoint(self, tid, title, files):
        git(self.dir, "add", *files)
        git(self.dir, "commit", "-q", "-m", f"{tid}: {title} [fakebot]\n\nOmniCLI team checkpoint.")
        return git(self.dir, "rev-parse", "--short", "HEAD")

    @property
    def state_path(self):
        return paths.project_state_dir(self.dir) / "state.json"

    def write_state(self, phase="complete", pid=0, tasks=None, workers=None):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "version": 1, "goal": "Build a tiny app", "project_name": "tiny", "plan_summary": "3 steps",
            "lead": "fakebot", "workers": workers if workers is not None else ["fakebot"], "round": 1,
            "final": {"complete": True, "summary": "all good", "how_to_run": "python3 app.py",
                      "remaining_issues": [], "followup_tasks": []},
            "started_at": time.time() - 100, "updated_at": time.time(), "phase": phase, "pid": pid,
            "tasks": tasks if tasks is not None else self.tasks,
        }
        self.state_path.write_text(json.dumps(state, indent=2))

    def state(self):
        return json.loads(self.state_path.read_text())

    def task(self, tid):
        return next(t for t in self.state()["tasks"] if t["id"] == tid)

    def head(self):
        return git(self.dir, "rev-parse", "HEAD")

    def project_dirty(self):
        return git(self.dir, "status", "--porcelain", "--", ".", ":(exclude).omnicli")

    def run_cmd(self, *argv, answer=None):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="subcommand")
        undo.add_parser(sub)
        args = parser.parse_args(list(argv))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            if answer is None:
                code = args.func(args)
            else:
                with mock.patch("builtins.input", return_value=answer) as inp:
                    code = args.func(args)
                    self.prompted = inp.called
        return code, buf.getvalue()

    def run_json(self, *argv):
        code, out = self.run_cmd(*argv, "--json")
        return code, json.loads(out)


class TestUndoList(UndoBase):
    def test_names_and_rewind_alias(self):
        self.assertEqual(set(undo.NAMES), {"undo", "rewind"})
        code, data = self.run_json("rewind", "-d", str(self.dir))
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])

    def test_list_json(self):
        code, data = self.run_json("undo", "--list", "-d", str(self.dir))
        self.assertEqual(code, 0)
        self.assertTrue(data["git"])
        self.assertFalse(data["live"])
        rows = {r["id"]: r for r in data["checkpoints"]}
        self.assertEqual(list(rows), ["t1", "t2", "t3", "t4"])
        self.assertEqual(rows["t2"]["subject"], "t2: Add helper lib [fakebot]")
        self.assertTrue(rows["t1"]["in_head"])
        self.assertTrue(rows["t3"]["undoable"])
        self.assertEqual(rows["t1"]["worker"], "fakebot")
        self.assertEqual(rows["t4"]["commit"], "")
        self.assertFalse(rows["t4"]["in_head"])

    def test_list_text_is_default_without_task(self):
        code, out = self.run_cmd("undo", "-d", str(self.dir))
        self.assertEqual(code, 0)
        self.assertIn("t1: Create app [fakebot]", out)
        self.assertIn("Write docs", out)
        self.assertIn("Undo one with: omnicli undo <task>", out)

    def test_list_marks_commit_not_in_head(self):
        git(self.dir, "checkout", "-q", "-b", "other", self.base)
        code, data = self.run_json("undo", "-d", str(self.dir))
        self.assertEqual(code, 0)
        self.assertFalse(any(r["in_head"] for r in data["checkpoints"]))

    def test_run_resolution_registry_cwd_and_most_recent(self):
        code, data = self.run_json("undo", "--run", "tiny")
        self.assertEqual((code, data["dir"]), (0, str(self.dir)))
        old = os.getcwd()
        try:
            os.chdir(self.dir)
            code, data = self.run_json("undo")
            self.assertEqual(data["dir"], str(self.dir))
            os.chdir(self.tmp)  # no state here -> most recent registered run
            code, data = self.run_json("undo")
            self.assertEqual((code, data["dir"]), (0, str(self.dir)))
        finally:
            os.chdir(old)

    def test_no_run_found(self):
        code, data = self.run_json("undo", "--run", "does-not-exist")
        self.assertEqual(code, 1)
        self.assertIn("omnicli runs", data["error"])
        code, out = self.run_cmd("undo", "-d", str(self.tmp))
        self.assertEqual(code, 1)
        self.assertIn("No team run", out)


class TestUndoTask(UndoBase):
    def test_successful_revert_updates_files_git_state_and_registry(self):
        before = self.head()
        code, out = self.run_cmd("undo", "t3", "-y", "-d", str(self.dir))
        self.assertEqual(code, 0, out)
        # files restored
        self.assertEqual((self.dir / "README.md").read_text(), "# tiny\n")
        # a new revert commit on top, history kept
        self.assertNotEqual(self.head(), before)
        self.assertEqual(git(self.dir, "rev-parse", "HEAD~1"), before)
        self.assertTrue(git(self.dir, "log", "-1", "--format=%s").startswith("Revert \"t3: Write docs"))
        self.assertEqual(self.project_dirty(), "")
        new_short = git(self.dir, "rev-parse", "--short", "HEAD")
        self.assertIn(f"Reverted t3 ({new_short}). Redo it with: omnicli resume {self.dir}", out)
        # state
        st = self.state()
        t3 = self.task("t3")
        self.assertEqual(t3["status"], "pending")
        for key in ("commit", "agent_used", "summary", "review_note"):
            self.assertEqual(t3[key], "", key)
        self.assertEqual(t3["duration_sec"], 0)
        last = t3["attempts"][-1]
        self.assertEqual(last["agent"], "")
        self.assertEqual(last["result"], f"undone (revert {self.c3[:7]})")
        self.assertIn("at", last)
        self.assertEqual(len(t3["attempts"]), 2)
        self.assertEqual((st["phase"], st["pid"]), ("stopped", 0))
        self.assertIs(st["final"]["complete"], False)
        self.assertEqual(st["final"]["summary"], "all good")
        self.assertEqual(self.task("t1")["status"], "done")
        # registry
        rec = runs.find_run(str(self.dir))
        self.assertEqual(rec["phase"], "stopped")
        self.assertEqual((rec["tasks_done"], rec["tasks_failed"], rec["tasks_total"]), (2, 1, 4))
        self.assertEqual(rec["pid"], 0)
        # nothing left to undo for t3; the list remembers the undo
        code, data = self.run_json("undo", "t3", "-y", "-d", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("nothing to undo", data["error"])
        code, data = self.run_json("undo", "-d", str(self.dir))
        row = next(r for r in data["checkpoints"] if r["id"] == "t3")
        self.assertEqual(row["undone"]["revert_commit"], new_short)

    def test_json_result_and_dependent_warning(self):
        code, data = self.run_json("undo", "t1", "-y", "-d", str(self.dir))
        self.assertEqual(code, 0, data)
        self.assertEqual(data["task"], "t1")
        self.assertEqual(data["reverted_commit"], self.c1[:7])
        self.assertEqual(data["new_commit"], git(self.dir, "rev-parse", "--short", "HEAD"))
        self.assertEqual([d["id"] for d in data["affected_dependents"]], ["t2", "t3"])
        self.assertEqual(data["affected_dependents"][1]["via"], "t2")
        self.assertFalse((self.dir / "app.py").exists())
        self.assertEqual((self.dir / "config.txt").read_text(), "mode = base\n")
        self.assertTrue((self.dir / "lib.py").exists())  # dependents' work stays
        self.assertEqual(self.task("t2")["status"], "done")

    def test_dependent_warning_text(self):
        code, out = self.run_cmd("undo", "t1", "-y", "-d", str(self.dir))
        self.assertEqual(code, 0, out)
        self.assertIn("already done", out)
        self.assertIn("t2", out)
        self.assertIn("(via t2)", out)
        self.assertIn("app.py", out)  # git show --stat preview

    def test_agent_and_note(self):
        code, out = self.run_cmd("undo", "t3", "-y", "-d", str(self.dir), "--agent", "otherbot",
                                 "--note", "Use a table for the docs")
        self.assertEqual(code, 0, out)
        t3 = self.task("t3")
        self.assertEqual(t3["assignee"], "otherbot")
        self.assertTrue(t3["instructions"].endswith(
            "\n\nNOTE FROM THE USER (after undo): Use a table for the docs"))
        self.assertTrue(t3["instructions"].startswith("Do: Write docs"))
        self.assertIn("otherbot", self.state()["workers"])  # so resume really uses it
        self.assertNotIn("not a known agent", out)

    def test_unknown_agent_warns_but_sets_assignee(self):
        code, out = self.run_cmd("undo", "t3", "-y", "-d", str(self.dir), "--agent", "nosuchbot")
        self.assertEqual(code, 0, out)
        self.assertIn("not a known agent", out)
        self.assertEqual(self.task("t3")["assignee"], "nosuchbot")
        self.assertEqual(self.state()["workers"], ["fakebot"])

    def test_confirmation_declined_changes_nothing(self):
        before_head, before_state = self.head(), self.state_path.read_text()
        code, out = self.run_cmd("undo", "t3", "-d", str(self.dir), answer="n")
        self.assertTrue(self.prompted)
        self.assertEqual(code, 1)
        self.assertIn("nothing changed", out)
        self.assertEqual(self.head(), before_head)
        self.assertEqual(self.state_path.read_text(), before_state)
        code, out = self.run_cmd("undo", "t3", "-d", str(self.dir), answer="")  # default is No
        self.assertEqual(self.head(), before_head)

    def test_confirmation_accepted(self):
        code, out = self.run_cmd("undo", "t3", "-d", str(self.dir), answer="y")
        self.assertTrue(self.prompted)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.task("t3")["status"], "pending")

    def test_json_without_yes_does_not_prompt_or_change(self):
        before = self.head()
        with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")):
            code, data = self.run_json("undo", "t3", "-d", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("-y", data["error"])
        self.assertEqual(data["would_revert"], self.c3[:7])
        self.assertEqual(self.head(), before)

    def test_fallback_identity_when_repo_has_none(self):
        git(self.dir, "config", "--unset", "user.email")
        git(self.dir, "config", "--unset", "user.name")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}):
            code, out = self.run_cmd("undo", "t3", "-y", "-d", str(self.dir))
        self.assertEqual(code, 0, out)
        self.assertEqual(git(self.dir, "log", "-1", "--format=%an <%ae>"), "OmniCLI <omnicli@localhost>")


class TestUndoRefusals(UndoBase):
    def assert_untouched(self, before_head, before_state):
        self.assertEqual(self.head(), before_head)
        self.assertEqual(self.state_path.read_text(), before_state)

    def snapshot(self):
        return self.head(), self.state_path.read_text()

    def test_dirty_tree(self):
        (self.dir / "scratch.txt").write_text("wip\n")
        snap = self.snapshot()
        code, out = self.run_cmd("undo", "t3", "-y", "-d", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("uncommitted changes", out)
        self.assertIn("scratch.txt", out)
        self.assert_untouched(*snap)
        (self.dir / "scratch.txt").unlink()
        (self.dir / "app.py").write_text("x = 99\n")  # tracked modification
        code, data = self.run_json("undo", "t3", "-y", "-d", str(self.dir))
        self.assertEqual(code, 1)
        self.assertTrue(any("app.py" in f for f in data["dirty_files"]))

    def test_dirty_check_fallback_without_exclude_pathspec(self):
        real = undo._git

        def no_magic(cwd, *args):
            if ":(exclude).omnicli" in args:
                return subprocess.CompletedProcess(args, 128, "", "fatal: bad pathspec")
            return real(cwd, *args)

        with mock.patch.object(undo, "_git", side_effect=no_magic):
            self.assertEqual(undo.dirty_files(self.dir), [])  # untracked .omnicli/ ignored
            (self.dir / "scratch.txt").write_text("wip\n")
            self.assertEqual([ln[3:] for ln in undo.dirty_files(self.dir)], ["scratch.txt"])
            # project in a sub-directory of a bigger repo: porcelain paths carry the prefix
            sub = self.dir / "pkg"
            (sub / ".omnicli").mkdir(parents=True)
            (sub / ".omnicli" / "x.json").write_text("{}")
            self.assertEqual(undo.dirty_files(sub), [])
            (sub / "mod.py").write_text("pass\n")
            self.assertEqual(len(undo.dirty_files(sub)), 1)

    def test_unknown_task(self):
        snap = self.snapshot()
        code, out = self.run_cmd("undo", "t9", "-y", "-d", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("No task 't9'", out)
        self.assertIn("t1, t2, t3, t4", out)
        code, data = self.run_json("undo", "t9", "-y", "-d", str(self.dir))
        self.assertEqual(data["valid_ids"], ["t1", "t2", "t3", "t4"])
        self.assert_untouched(*snap)

    def test_no_commit(self):
        snap = self.snapshot()
        code, out = self.run_cmd("undo", "t4", "-y", "-d", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("nothing to undo", out)
        self.assert_untouched(*snap)

    def test_live_run(self):
        self.write_state(phase="running", pid=os.getpid())
        snap = self.snapshot()
        code, out = self.run_cmd("undo", "t3", "-y", "-d", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("still running", out)
        self.assert_untouched(*snap)

    def test_stale_running_phase_with_dead_pid_is_allowed(self):
        self.write_state(phase="running", pid=0)
        code, out = self.run_cmd("undo", "t3", "-y", "-d", str(self.dir))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()["phase"], "stopped")

    def test_not_a_git_repo(self):
        plain = self.tmp / "plain"
        shutil.copytree(self.dir / ".omnicli", plain / ".omnicli")
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)}):
            code, out = self.run_cmd("undo", "t3", "-y", "-d", str(plain))
            self.assertEqual(code, 1)
            self.assertIn("not a git work tree", out)
            code, out = self.run_cmd("undo", "-d", str(plain))  # listing still works
            self.assertEqual(code, 0)
            self.assertIn("not a git work tree", out)

    def test_commit_not_in_head(self):
        git(self.dir, "checkout", "-q", "-b", "other", self.base)
        snap = self.snapshot()
        code, out = self.run_cmd("undo", "t1", "-y", "-d", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("not in HEAD's history", out)
        self.assert_untouched(*snap)

    def test_commit_missing_from_repo(self):
        tasks = [dict(t) for t in self.tasks]
        tasks[2]["commit"] = "deadbee"
        self.write_state(tasks=tasks)
        code, out = self.run_cmd("undo", "t3", "-y", "-d", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("not in this repository", out)

    def test_conflict_is_aborted_and_leaves_repo_clean(self):
        # t5 (built on t1) later rewrites the same line t1 changed -> reverting t1 conflicts
        (self.dir / "config.txt").write_text("mode = t5\n")
        c5 = self.checkpoint("t5", "Tune config", ["config.txt"])
        self.write_state(tasks=self.tasks + [make_task("t5", "Tune config", c5, deps=["t1"])])
        snap = self.snapshot()
        code, out = self.run_cmd("undo", "t1", "-y", "-d", str(self.dir))
        self.assertEqual(code, 1, out)
        self.assertIn("conflicts", out)
        self.assertIn("config.txt", out)
        self.assertIn("t5", out)  # hint: undo the dependent first
        self.assert_untouched(*snap)
        self.assertEqual(self.project_dirty(), "")
        no_revert = subprocess.run(["git", "rev-parse", "-q", "--verify", "REVERT_HEAD"], cwd=str(self.dir))
        self.assertNotEqual(no_revert.returncode, 0)  # no revert left in progress
        self.assertEqual((self.dir / "config.txt").read_text(), "mode = t5\n")
        self.assertTrue((self.dir / "app.py").exists())
        code, data = self.run_json("undo", "t1", "-y", "-d", str(self.dir))
        self.assertEqual(data["conflicts"], ["config.txt"])
        self.assertEqual(self.project_dirty(), "")

    def test_already_reverted_by_hand(self):
        git(self.dir, "revert", "--no-edit", self.c3)
        snap = self.snapshot()
        code, out = self.run_cmd("undo", "t3", "-y", "-d", str(self.dir))
        self.assertEqual(code, 1)
        self.assertIn("already gone", out)
        self.assert_untouched(*snap)


class TestHelpers(unittest.TestCase):
    def test_dependents_are_transitive_and_cycle_safe(self):
        tasks = [{"id": "a"}, {"id": "b", "depends_on": ["a"]}, {"id": "c", "depends_on": ["b"]},
                 {"id": "d", "depends_on": ["a", "c"]}, {"id": "e"}, {"id": "f", "depends_on": ["f", "a"]}]
        out = undo.dependents(tasks, "a")
        self.assertEqual([x["task"]["id"] for x in out], ["b", "d", "f", "c"])
        self.assertEqual(out[3]["via"], "b")
        self.assertEqual(undo.dependents(tasks, "e"), [])

    def test_registered_with_cli(self):
        from omnicli.commands import COMMAND_MODULES
        self.assertIn("undo", COMMAND_MODULES)


class TestThroughCli(UndoBase):
    def test_main_routes_undo_and_rewind(self):
        from omnicli import cli
        for name in ("undo", "rewind"):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main(["--no-banner", name, "--json", "-d", str(self.dir)])
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(buf.getvalue())["checkpoints"]), 4)


if __name__ == "__main__":
    unittest.main()
