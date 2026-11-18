"""
Tests for `omnicli status` (omnicli/commands/status.py).

All fixtures are fake state.json files / logs in temp dirs; nothing calls an AI CLI.
"""

import os
import tempfile

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")
os.environ["NO_COLOR"] = "1"

import argparse  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402

from omnicli import runs  # noqa: E402
from omnicli.commands import status  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def plain(text: str) -> str:
    return ANSI.sub("", text)


def dead_pid() -> int:
    """A pid that is not running: a child we started and already reaped."""
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def ns(**kw) -> argparse.Namespace:
    base = {"ref": None, "dir": None, "watch": None, "json": False}
    base.update(kw)
    return argparse.Namespace(**base)


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


def task(tid, title, status_, assignee="claude", agent_used="", tries=0, commit="", duration=0.0,
         depends_on=None, summary="", review_note=""):
    return {"id": tid, "title": title, "assignee": assignee, "instructions": "do it",
            "depends_on": depends_on or [],
            "owns": [], "acceptance": "", "verify": "", "round": 1, "status": status_,
            "agent_used": agent_used, "summary": summary, "review_note": review_note,
            "attempts": [{"agent": agent_used or assignee, "revision": i, "exit_code": 0, "duration_sec": 1.0,
                          "at": "2026-11-15T10:00:00", "result": "approved"} for i in range(tries)],
            "commit": commit, "duration_sec": duration}


class StatusBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="omni-status-home-"))
        self._old_home = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = str(self.home)
        self.tmp = Path(tempfile.mkdtemp(prefix="omni-status-proj-")).resolve()

    def tearDown(self):
        os.environ["OMNICLI_HOME"] = self._old_home or ""
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- fixtures
    def make_project(self, name, phase="planned", tasks=None, pid=0, final=None, register=True, **extra):
        d = self.tmp / name
        sdir = d / ".omnicli" / "team"
        (sdir / "logs").mkdir(parents=True, exist_ok=True)
        now = time.time()
        state = {"version": 1, "goal": extra.pop("goal", f"Build the {name} project"), "project_name": name,
                 "plan_summary": "plan", "lead": "claude", "workers": ["claude", "agy", "muse"], "round": 1,
                 "final": final or {}, "started_at": now - 600, "updated_at": now - 30, "phase": phase,
                 "pid": pid, "tasks": tasks or []}
        state.update(extra)
        (sdir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        if register:
            runs.register_run({"dir": str(d), "goal": state["goal"], "project_name": name, "phase": phase,
                               "pid": pid, "started_at": state["started_at"]})
        return d

    def write_state(self, d, **changes):
        p = d / ".omnicli" / "team" / "state.json"
        state = json.loads(p.read_text(encoding="utf-8"))
        state.update(changes)
        p.write_text(json.dumps(state), encoding="utf-8")

    def complete_project(self, name="shop"):
        d = self.make_project(name, phase="complete", tasks=[
            task("t1", "Scaffold the app", "done", agent_used="claude", tries=1, commit="abcdef1234567", duration=95),
            task("t2", "Add cart", "done", agent_used="agy", tries=2, commit="1234567890abc", duration=250),
            task("t3", "Write README", "done", agent_used="muse", tries=1, commit="fedcba9876543", duration=30),
        ], final={"complete": True, "summary": ("Shop built. " * 40) + "TAILMARKER",
                  "how_to_run": "python3 app.py", "remaining_issues": ["flaky test", "no auth"],
                  "followup_tasks": []})
        (d / ".omnicli" / "team" / "REPORT.md").write_text("# report\n", encoding="utf-8")
        return d

    def running_project(self, name="api", pid=None):
        d = self.make_project(name, phase="running", pid=os.getpid() if pid is None else pid, tasks=[
            task("t1", "Scaffold", "done", agent_used="claude", tries=1, commit="aaaaaaa1", duration=60,
                 summary="Created the project skeleton."),
            task("t2", "Build the REST endpoints", "running", assignee="agy"),
            task("t2-2", "Lookalike task", "pending", assignee="claude", depends_on=["t4"]),
            task("t3", "UI", "pending", assignee="muse"),
            task("t4", "Tests", "failed", agent_used="claude", tries=2, duration=120,
                 review_note="verify still fails: one assertion"),
        ])
        logs = d / ".omnicli" / "team" / "logs"
        prompt = logs / "t2-agy-1.prompt.md"
        prompt.write_text("prompt", encoding="utf-8")
        then = time.time() - 65
        os.utime(prompt, (then, then))
        (logs / "t2-agy-1.md").write_text("alpha\n\nbeta\n\x1b[31mgamma\x1b[0m\n\n\ndelta\n\n", encoding="utf-8")
        (logs / "t2-agy-1.stderr.log").write_text("", encoding="utf-8")
        # a different task whose name starts with "t2-": must not be mistaken for t2's log
        look = logs / "t2-2-claude-1.prompt.md"
        look.write_text("x", encoding="utf-8")
        (logs / "t2-2-claude-1.md").write_text("WRONGTASK\n", encoding="utf-8")
        os.utime(look, (time.time(), time.time()))
        return d

    # ---------------------------------------------------------------- runners
    def run_status(self, cwd=None, **kw):
        out, err = io.StringIO(), io.StringIO()
        code = status.run(ns(**kw), out=out, err=err, cwd=str(cwd or self.tmp), width=110, sleep=lambda s: None)
        return code, plain(out.getvalue()), plain(err.getvalue())

    def run_json(self, **kw):
        code, out, err = self.run_status(json=True, **kw)
        self.assertEqual(code, 0, err)
        return json.loads(out)


class TestTextOutput(StatusBase):
    def test_complete_run(self):
        d = self.complete_project()
        code, out, err = self.run_status(dir=str(d))
        self.assertEqual(code, 0, err)
        self.assertIn("shop", out)
        self.assertIn(str(d), out)
        self.assertIn("[✓ COMPLETE]", out)
        self.assertIn("3/3 done", out)
        self.assertIn("[" + "#" * status.BAR_WIDTH + "]", out)
        self.assertIn("abcdef1", out)
        self.assertNotIn("abcdef1234567", out)
        self.assertIn("1m 35s", out)
        self.assertIn("complete: yes", out)
        self.assertIn("Shop built.", out)
        self.assertNotIn("TAILMARKER", out)  # summary cut to ~300 chars
        self.assertIn("2 remaining", out)
        self.assertIn(str(d / ".omnicli" / "team" / "REPORT.md"), out)
        self.assertNotIn("live", out)
        self.assertNotIn("interrupted", out)

    def test_running_alive(self):
        d = self.running_project()
        code, out, err = self.run_status(dir=str(d))
        self.assertEqual(code, 0, err)
        self.assertIn("[⚡ RUNNING]", out)
        self.assertIn(f"● live (pid {os.getpid()})", out)
        self.assertIn("1/5 done · 1 failed · 1 running", out)
        self.assertRegex(out, r"\[#+-+\] 1/5 done")
        row = next(l for l in out.splitlines() if l.strip().startswith("t2 "))
        self.assertIn("agy", row)
        self.assertIn("running", row)
        self.assertRegex(row, r"1m 0\ds")
        # last 3 non-empty lines of the newest t2 log, ANSI stripped; lookalike task ignored
        self.assertIn("│ beta", out)
        self.assertIn("│ gamma", out)
        self.assertIn("│ delta", out)
        self.assertNotIn("alpha", out)
        self.assertNotIn("WRONGTASK", out)
        self.assertIn("t2 · agy · attempt 1 · running 1m", out)
        self.assertIn("Agents", out)
        self.assertIn("agy (worker) · working · 0/1 tasks done · 1 active", out)
        self.assertIn("Latest: delta", out)
        self.assertIn("Done: t1 Scaffold — Created the project skeleton.", out)
        self.assertIn("Blocker (t4): verify still fails: one assertion", out)
        self.assertIn("Blocker (t2-2): Waiting for t4", out)
        self.assertNotIn("omnicli resume", out)

    def test_running_but_dead_is_interrupted(self):
        d = self.running_project(pid=dead_pid())
        code, out, err = self.run_status(dir=str(d))
        self.assertEqual(code, 0, err)
        self.assertIn("interrupted", out)
        self.assertIn(f"omnicli resume {d}", out)
        self.assertNotIn("● live", out)
        self.assertIn("1 interrupted", out)

    def test_planned_empty(self):
        d = self.make_project("blank", phase="planned")
        code, out, err = self.run_status(dir=str(d))
        self.assertEqual(code, 0, err)
        self.assertIn("[◆ PLANNED]", out)
        self.assertIn("no tasks planned yet", out)
        self.assertIn("omnicli resume", out)
        self.assertNotIn("Final", out)

    def test_long_title_truncated_and_goal_wrapped(self):
        long_goal = "word " * 80
        d = self.make_project("wide", phase="planned", goal=long_goal,
                              tasks=[task("t1", "x" * 300, "pending")])
        code, out, _ = self.run_status(dir=str(d))
        self.assertEqual(code, 0)
        self.assertIn("…", out)
        self.assertTrue(all(len(l) <= 160 for l in out.splitlines()
                            if "Logs:" not in l and str(d) not in l), out)
        goal_lines = [l for l in out.splitlines() if "word" in l]
        self.assertGreater(len(goal_lines), 1)

    def test_stderr_fallback_when_no_output_log(self):
        d = self.running_project()
        logs = d / ".omnicli" / "team" / "logs"
        (logs / "t2-agy-1.md").unlink()
        (logs / "t2-agy-1.stderr.log").write_text("thinking\nediting api.py\n", encoding="utf-8")
        _, out, _ = self.run_status(dir=str(d))
        self.assertIn("(stderr)", out)
        self.assertIn("│ editing api.py", out)

    def test_active_lead_review_is_attached_to_reviewer_agent(self):
        d = self.running_project()
        logs = d / ".omnicli" / "team" / "logs"
        prompt = logs / "lead-001-review-claude.prompt.md"
        prompt.write_text("[OmniCLI team - review]\nTASK t2: Build the REST endpoints\n", encoding="utf-8")
        (logs / "lead-001-review-claude.md").write_text("Checking the endpoint diff.\n", encoding="utf-8")
        snap = status.build_snapshot(d, runs.load_state(d))
        claude = next(a for a in snap["agents"] if a["id"] == "claude")
        self.assertEqual(claude["status"], "reviewing")
        self.assertEqual(claude["reviewing_task"], {"task_id": "t2", "task_title": "Build the REST endpoints",
                                                     "builder": "agy"})
        self.assertEqual(claude["latest_output"], "Checking the endpoint diff.")
        _, out, _ = self.run_status(dir=str(d))
        self.assertIn("Reviewing: t2 Build the REST endpoints by agy", out)

    def test_no_output_yet(self):
        d = self.running_project()
        logs = d / ".omnicli" / "team" / "logs"
        (logs / "t2-agy-1.md").unlink()
        _, out, _ = self.run_status(dir=str(d))
        self.assertIn("(no output yet)", out)

    def test_newest_attempt_log_wins(self):
        d = self.running_project()
        logs = d / ".omnicli" / "team" / "logs"
        p2 = logs / "t2-claude-2.prompt.md"
        p2.write_text("retry", encoding="utf-8")
        (logs / "t2-claude-2.md").write_text("second attempt line\n", encoding="utf-8")
        then = time.time() - 5
        os.utime(p2, (then, then))
        snap = status.build_snapshot(d, runs.load_state(d))
        t2 = next(t for t in snap["tasks"] if t["id"] == "t2")
        self.assertEqual(t2["worker"], "claude")
        self.assertLess(t2["duration_sec"], 60)
        self.assertEqual(snap["_live"]["t2"]["lines"], ["second attempt line"])


class TestJson(StatusBase):
    KEYS = {"dir", "project_name", "goal", "phase", "alive", "interrupted", "lead", "workers", "round",
            "counts", "tasks", "agents", "final"}

    def test_complete_json(self):
        d = self.complete_project()
        data = self.run_json(dir=str(d))
        self.assertTrue(self.KEYS <= set(data))
        self.assertFalse(any(k.startswith("_") for k in data))
        self.assertEqual(data["dir"], str(d))
        self.assertEqual(data["phase"], "complete")
        self.assertFalse(data["alive"])
        self.assertFalse(data["interrupted"])
        self.assertEqual(data["counts"], {"done": 3, "failed": 0, "running": 0, "pending": 0, "skipped": 0,
                                          "total": 3})
        self.assertEqual(data["tasks"][1], {"id": "t2", "title": "Add cart", "worker": "agy", "status": "done",
                                            "tries": 2, "duration_sec": 250.0, "commit": "1234567890abc"})
        self.assertTrue(data["final"]["complete"])
        self.assertEqual(data["workers"], ["claude", "agy", "muse"])

    def test_running_json(self):
        d = self.running_project()
        data = self.run_json(dir=str(d))
        self.assertTrue(data["alive"])
        self.assertFalse(data["interrupted"])
        self.assertEqual(data["counts"]["running"], 1)
        self.assertEqual(data["counts"]["pending"], 2)
        self.assertEqual(data["counts"]["total"], 5)
        t2 = next(t for t in data["tasks"] if t["id"] == "t2")
        self.assertEqual(t2["worker"], "agy")
        self.assertGreaterEqual(t2["duration_sec"], 60)
        self.assertIsNone(data["final"])
        agy = next(a for a in data["agents"] if a["id"] == "agy")
        self.assertEqual(agy["current_tasks"], [{"id": "t2", "title": "Build the REST endpoints"}])
        self.assertEqual(agy["latest_output"], "delta")
        self.assertEqual(agy["progress"]["total"], 1)
        claude = next(a for a in data["agents"] if a["id"] == "claude")
        self.assertIn({"task_id": "t4", "reason": "verify still fails: one assertion"}, claude["blockers"])
        self.assertEqual(claude["completed_work"][0]["id"], "t1")

    def test_dead_json(self):
        d = self.running_project(pid=dead_pid())
        data = self.run_json(dir=str(d))
        self.assertFalse(data["alive"])
        self.assertTrue(data["interrupted"])

    def test_planned_json(self):
        d = self.make_project("blank", phase="planned")
        data = self.run_json(dir=str(d))
        self.assertEqual(data["counts"]["total"], 0)
        self.assertEqual(data["tasks"], [])
        self.assertIsNone(data["final"])
        self.assertFalse(data["interrupted"])

    def test_corrupt_fields_do_not_crash(self):
        d = self.make_project("odd", phase="running", pid="nope", workers="claude", round="x",
                              tasks=[{"id": "t1", "status": "running", "attempts": "?"}, "junk"],
                              started_at="yesterday")
        code, out, err = self.run_status(dir=str(d))
        self.assertEqual(code, 0, err)
        self.assertIn("interrupted", out)
        data = self.run_json(dir=str(d))
        self.assertEqual(data["counts"]["total"], 1)


class TestResolution(StatusBase):
    def test_dir_wins_over_ref_and_cwd(self):
        a = self.make_project("alpha")
        b = self.make_project("beta")
        data = self.run_json(dir=str(a), ref="beta", cwd=b)
        self.assertEqual(data["project_name"], "alpha")

    def test_ref_by_name_and_index(self):
        self.make_project("alpha")
        self.make_project("beta")
        other = self.tmp / "empty"
        other.mkdir()
        self.assertEqual(self.run_json(ref="alph", cwd=other)["project_name"], "alpha")
        self.assertEqual(self.run_json(ref="1", cwd=other)["project_name"], "beta")  # newest first

    def test_cwd_beats_most_recent(self):
        a = self.make_project("alpha")
        self.make_project("beta")  # registered later -> most recent
        self.assertEqual(self.run_json(cwd=a)["project_name"], "alpha")

    def test_real_cwd_used_by_default(self):
        a = self.make_project("alpha", register=False)
        old = os.getcwd()
        os.chdir(str(a))
        try:
            out, err = io.StringIO(), io.StringIO()
            code = status.run(ns(json=True), out=out, err=err)
        finally:
            os.chdir(old)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["project_name"], "alpha")

    def test_most_recent_when_cwd_has_no_run(self):
        self.make_project("alpha")
        self.make_project("beta")
        other = self.tmp / "elsewhere"
        other.mkdir()
        self.assertEqual(self.run_json(cwd=other)["project_name"], "beta")

    def test_nothing_found(self):
        other = self.tmp / "nothing"
        other.mkdir()
        code, out, err = self.run_status(cwd=other)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("omnicli runs", err)
        self.assertIn('omnicli team "<goal>"', err)

    def test_bad_ref(self):
        self.make_project("alpha")
        code, _, err = self.run_status(ref="zzz-no-such-run")
        self.assertEqual(code, 1)
        self.assertIn("No team run matches 'zzz-no-such-run'", err)
        self.assertIn("omnicli runs", err)

    def test_dir_without_state(self):
        empty = self.tmp / "plain"
        empty.mkdir()
        code, _, err = self.run_status(dir=str(empty))
        self.assertEqual(code, 1)
        self.assertIn("No team run in", err)
        self.assertIn("omnicli team", err)
        code, _, err = self.run_status(dir=str(self.tmp / "missing"))
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)

    def test_corrupt_state_file(self):
        d = self.make_project("broken")
        (d / ".omnicli" / "team" / "state.json").write_text("{nope", encoding="utf-8")
        code, _, err = self.run_status(dir=str(d))
        self.assertEqual(code, 1)
        self.assertIn("missing or corrupt", err)


class TestWatch(StatusBase):
    def watch(self, d, out=None, max_frames=10, sleep=None, watch=1.0, **kw):
        out = out if out is not None else io.StringIO()
        err = io.StringIO()
        args = ns(dir=str(d), watch=watch, **kw)
        code = status.run(args, out=out, err=err, width=110, max_frames=max_frames,
                          sleep=sleep or (lambda s: None))
        return code, plain(out.getvalue()), err.getvalue()

    def test_not_running_draws_one_frame(self):
        d = self.make_project("blank", phase="planned")
        code, out, _ = self.watch(d)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("[◆ PLANNED]"), 1)
        self.assertIn("watch ended: phase is planned", out)

    def test_stops_when_phase_leaves_running(self):
        d = self.running_project()
        calls = []

        def fake_sleep(secs):
            calls.append(secs)
            self.write_state(d, phase="complete", pid=0)

        code, out, _ = self.watch(d, sleep=fake_sleep, watch=2.5)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [2.5])
        self.assertEqual(out.count("[⚡ RUNNING]"), 1)
        self.assertEqual(out.count("[✓ COMPLETE]"), 1)
        # one timestamped separator between the two frames (not a TTY)
        self.assertEqual(len(re.findall(r"^── \d\d:\d\d:\d\d ", out, re.M)), 1)
        self.assertNotIn("\x1b[2J", out)
        self.assertIn("watch ended: phase is complete", out)

    def test_max_frames_bounds_a_live_run(self):
        d = self.running_project()
        code, out, _ = self.watch(d, max_frames=3)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("[⚡ RUNNING]"), 3)

    def test_interrupted_run_stops(self):
        d = self.running_project(pid=dead_pid())
        code, out, _ = self.watch(d)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("[⚡ RUNNING]"), 1)
        self.assertIn("watch ended: the run was interrupted", out)

    def test_ctrl_c_exits_zero(self):
        d = self.running_project()

        def interrupt(_):
            raise KeyboardInterrupt

        code, out, _ = self.watch(d, sleep=interrupt)
        self.assertEqual(code, 0)
        self.assertEqual(out.count("[⚡ RUNNING]"), 1)

    def test_tty_clears_screen(self):
        d = self.running_project()
        buf = TTYBuffer()
        code, out, _ = self.watch(d, out=buf, max_frames=2)
        self.assertEqual(code, 0)
        self.assertEqual(buf.getvalue().count("\x1b[H\x1b[2J"), 2)
        self.assertIn("Ctrl-C to stop", out)

    def test_watch_json_lines(self):
        d = self.running_project()
        code, out, _ = self.watch(d, max_frames=2, json=True)
        self.assertEqual(code, 0)
        frames = [json.loads(l) for l in out.splitlines() if l.strip()]
        self.assertEqual(len(frames), 2)
        self.assertTrue(all(f["alive"] for f in frames))

    def test_bad_watch_value(self):
        d = self.make_project("blank")
        for bad in ("0", "-2", "nan"):
            code, out, err = self.watch(d, watch=bad, ref="x")
            self.assertEqual(code, 2, bad)
            self.assertIn("--watch", err)


class TestParser(unittest.TestCase):
    def parse(self, argv):
        parser = argparse.ArgumentParser(prog="omnicli")
        sub = parser.add_subparsers(dest="subcommand")
        status.add_parser(sub)
        return parser.parse_args(argv)

    def test_names_and_defaults(self):
        self.assertEqual(status.NAMES, ["status"])
        a = self.parse(["status"])
        self.assertIs(a.func, status.handler)
        self.assertIsNone(a.watch)
        self.assertFalse(a.json)

    def test_watch_forms(self):
        self.assertEqual(self.parse(["status", "--watch"]).watch, status.DEFAULT_WATCH_SECONDS)
        a = self.parse(["status", "myproj", "--watch", "5", "-d", "/x", "--json"])
        self.assertEqual((a.ref, a.watch, a.dir, a.json), ("myproj", "5", "/x", True))
        secs, problem = status._parse_watch(a)
        self.assertEqual((secs, problem), (5.0, ""))

    def test_watch_followed_by_ref(self):
        a = self.parse(["status", "--watch", "myproj"])
        secs, problem = status._parse_watch(a)
        self.assertEqual(problem, "")
        self.assertEqual(secs, status.DEFAULT_WATCH_SECONDS)
        self.assertEqual(a.ref, "myproj")


class TestHelpers(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(status.fmt_duration(5), "5s")
        self.assertEqual(status.fmt_duration(65), "1m 05s")
        self.assertEqual(status.fmt_duration(3720), "1h 02m")
        self.assertEqual(status.fmt_duration(90000), "1d 1h")
        self.assertEqual(status.fmt_ago(None, 100.0), "unknown")
        self.assertEqual(status.fmt_ago(99.0, 100.0), "just now")
        self.assertEqual(status.fmt_ago(0.0 + 40, 100.0), "1m 00s ago")

    def test_progress_bar(self):
        counts = {"done": 5, "failed": 1, "running": 1, "pending": 1, "skipped": 0, "total": 8}
        bar = plain(status.progress_bar(counts, width=12))
        self.assertTrue(bar.startswith("[########----] 5/8 done · 1 failed · 1 running"), bar)
        self.assertIn("[------------] no tasks", plain(status.progress_bar({"total": 0}, width=12)))
        almost = plain(status.progress_bar({"done": 99, "total": 100}, width=10))
        self.assertIn("[#########-]", almost)  # never looks finished while work remains


class TestCliEntrypoint(StatusBase):
    def test_bin_omnicli_status_json(self):
        d = self.complete_project()
        env = dict(os.environ, OMNICLI_HOME=str(self.home), NO_COLOR="1")
        r = subprocess.run([sys.executable, str(ROOT / "bin" / "omnicli"), "--no-banner", "status", "-d", str(d),
                            "--json"], capture_output=True, text=True, env=env, cwd=str(self.tmp), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["phase"], "complete")


if __name__ == "__main__":
    unittest.main()
