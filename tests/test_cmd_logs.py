"""Tests for `omnicli logs` (omnicli/commands/logs.py). Never touches ~/.omnicli or a real AI CLI."""

import os
import tempfile

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")
os.environ.setdefault("NO_COLOR", "1")

import argparse  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402

from omnicli import paths, runs  # noqa: E402
from omnicli.commands import logs as L  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
BUILTINS = ["claude", "codex", "agy", "muse"]


def plain(text: str) -> str:
    return ANSI.sub("", text)


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omnicli")
    sub = p.add_subparsers(dest="subcommand")
    L.add_parser(sub)
    return p


STATE = {
    "version": 1,
    "goal": "build a tiny app",
    "project_name": "logsdemo",
    "lead": "claude",
    "workers": ["codex", "agy", "muse", "my-bot"],
    "round": 2,
    "phase": "running",
    "pid": 0,
    "tasks": [
        {"id": "t1", "title": "Backend", "assignee": "codex", "status": "done",
         "attempts": [{"agent": "codex", "revision": 0, "exit_code": 0, "duration_sec": 45.0,
                       "at": "2026-11-15T10:00:00", "result": "approved", "feedback": "",
                       "files_changed": 2, "verify_exit": 0}]},
        {"id": "t2", "title": "Parser", "assignee": "agy", "status": "done",
         "attempts": [{"agent": "agy", "revision": 0, "exit_code": 0, "duration_sec": 114.0,
                       "result": "revise", "feedback": "Add tests for the parser.",
                       "files_changed": 1, "verify_exit": 1},
                      {"agent": "agy", "revision": 1, "exit_code": 0, "duration_sec": 60.0,
                       "result": "approved", "feedback": "", "files_changed": 2, "verify_exit": 0}]},
        {"id": "r2-t1", "title": "Follow-up", "assignee": "claude", "round": 2, "status": "done",
         "attempts": [{"agent": "claude", "revision": 0, "exit_code": 0, "duration_sec": 30.0,
                       "result": "approved"}]},
        {"id": "fix-ui", "title": "Fix UI", "assignee": "my-bot", "status": "failed",
         "attempts": [{"agent": "my-bot", "revision": 0, "exit_code": 3, "duration_sec": 2.0,
                       "result": "agent error: exit code 3"}]},
        {"id": "t3", "title": "Docs", "assignee": "muse", "status": "running", "attempts": []},
        {"id": "t4", "title": "Deploy", "assignee": "codex", "status": "pending", "attempts": []},
    ],
}

# stem -> (seconds after base, which files exist)
LOGS = [
    ("lead-001-plan-claude", 0, ("output", "prompt", "stderr")),
    ("t1-codex-1", 10, ("output", "prompt", "stderr")),
    ("t2-agy-1", 20, ("output", "prompt", "stderr")),
    ("lead-002-review-claude", 30, ("output", "prompt", "stderr")),
    ("lead-003-review-claude", 40, ("output", "prompt", "stderr")),
    ("t2-agy-2", 50, ("output", "prompt", "stderr")),
    ("lead-004-review-claude", 60, ("output", "prompt", "stderr")),
    ("r2-t1-claude-1", 70, ("output", "prompt", "stderr")),
    ("fix-ui-my-bot-1", 80, ("output", "prompt", "stderr")),
    ("t3-muse-1", 90, ("prompt", "stderr")),          # still running: no output yet
    ("lead-005-final-codex", 100, ("output", "prompt", "stderr")),
]
SUFFIX = {"output": ".md", "prompt": ".prompt.md", "stderr": ".stderr.log"}


def build_project(root: Path, with_logs: bool = True) -> Path:
    proj = root / "proj"
    sdir = paths.project_state_dir(proj)
    sdir.mkdir(parents=True)
    (sdir / "state.json").write_text(json.dumps(STATE), encoding="utf-8")
    if not with_logs:
        return proj.resolve()
    logs = sdir / "logs"
    logs.mkdir()
    base = time.time() - 3600
    for stem, offset, streams in LOGS:
        for s in streams:
            f = logs / (stem + SUFFIX[s])
            if s == "output":
                body = "".join(f"OUTPUT {stem} line {i}\n" for i in range(1, 6))
            elif s == "prompt":
                body = f"PROMPT for {stem}\n"
            else:
                body = f"STDERR of {stem}\n"
            f.write_text(body, encoding="utf-8")
            os.utime(f, (base + offset, base + offset))
    stray = logs / "scratch.md"
    stray.write_text("not a call log\n", encoding="utf-8")
    os.utime(stray, (base - 10, base - 10))
    (logs / "ignore.txt").write_text("x", encoding="utf-8")
    return proj.resolve()


class LogsBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="omni-logs-home-"))
        self._old_home = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = str(self.home)
        self._old_cwd = os.getcwd()
        self.root = Path(tempfile.mkdtemp(prefix="omni-logs-"))
        self.proj = build_project(self.root)
        self.logs = paths.project_state_dir(self.proj) / "logs"
        self.parser = make_parser()

    def tearDown(self):
        os.chdir(self._old_cwd)
        os.environ["OMNICLI_HOME"] = self._old_home or ""
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cmd(self, *argv, **extra):
        args = self.parser.parse_args(["logs", *argv])
        for k, v in extra.items():
            setattr(args, k, v)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = args.func(args)
        return code, plain(buf.getvalue())

    def run_d(self, *argv, **extra):
        return self.run_cmd("-d", str(self.proj), *argv, **extra)


# --------------------------------------------------------------------------- parsing

class TestParsing(unittest.TestCase):
    def test_split_filename(self):
        self.assertEqual(L.split_log_filename("t2-agy-1.md"), ("t2-agy-1", "output"))
        self.assertEqual(L.split_log_filename("t2-agy-1.prompt.md"), ("t2-agy-1", "prompt"))
        self.assertEqual(L.split_log_filename("t2-agy-1.stderr.log"), ("t2-agy-1", "stderr"))
        self.assertEqual(L.split_log_filename("lead-001-plan-claude.prompt.md"), ("lead-001-plan-claude", "prompt"))
        self.assertEqual(L.split_log_filename("t1.5-codex-1.md"), ("t1.5-codex-1", "output"))
        self.assertIsNone(L.split_log_filename("notes.txt"))
        self.assertIsNone(L.split_log_filename(".md"))

    def check(self, stem, expect, agents=BUILTINS, tasks=()):
        got = L.parse_log_stem(stem, agents, tasks)
        for k, v in expect.items():
            self.assertEqual(got[k], v, f"{stem}: {k} -> {got}")

    def test_simple_worker_and_lead(self):
        self.check("t2-agy-1", {"role": "task", "task": "t2", "agent": "agy", "attempt": 1})
        self.check("t1-codex-12", {"role": "task", "task": "t1", "agent": "codex", "attempt": 12})
        self.check("lead-001-plan-claude", {"role": "lead", "call": 1, "lead_kind": "plan", "agent": "claude"})
        self.check("lead-1000-final-codex", {"role": "lead", "call": 1000, "lead_kind": "final", "agent": "codex"})

    def test_task_ids_with_dashes(self):
        self.check("r2-t1-claude-3", {"role": "task", "task": "r2-t1", "agent": "claude", "attempt": 3})
        self.check("api-auth-login-codex-2", {"role": "task", "task": "api-auth-login", "agent": "codex"})
        self.check("t2-muse-agy-1", {"task": "t2-muse", "agent": "agy"})

    def test_agent_names_with_dashes(self):
        self.check("fix-ui-my-bot-1", {"task": "fix-ui", "agent": "my-bot", "attempt": 1},
                   agents=BUILTINS + ["my-bot"])
        # agent unknown but the task id is known from state.json
        self.check("fix-ui-my-bot-1", {"task": "fix-ui", "agent": "my-bot"}, tasks=["fix-ui"])
        # nothing known: the agent is the last dash-segment
        self.check("fix-ui-my-bot-1", {"role": "task", "task": "fix-ui-my", "agent": "bot"}, agents=[])
        # a known short agent must not steal a longer known one
        self.check("t1-my-bot-2", {"task": "t1", "agent": "my-bot"}, agents=["bot", "my-bot"])
        self.check("lead-012-review-my-bot", {"role": "lead", "call": 12, "lead_kind": "review", "agent": "my-bot"},
                   agents=BUILTINS + ["my-bot"])
        self.check("t1-gpt-4-2", {"task": "t1", "agent": "gpt-4", "attempt": 2}, agents=["gpt-4"])
        self.check("lead-003-review-gpt-4", {"role": "lead", "lead_kind": "review", "agent": "gpt-4"},
                   agents=["gpt-4"])

    def test_lead_looking_names(self):
        # a task literally called "lead-1" is still a worker log
        self.check("lead-1-claude-2", {"role": "task", "task": "lead-1", "agent": "claude", "attempt": 2},
                   tasks=["lead-1"])
        self.check("lead-1-claude-2", {"role": "task", "task": "lead-1", "agent": "claude"})
        # unknown lead agent whose name ends in a number
        self.check("lead-003-review-gpt-4", {"role": "lead", "call": 3, "lead_kind": "review", "agent": "gpt-4"})
        self.check("lead-007-followup-claude", {"role": "lead", "lead_kind": "followup", "agent": "claude"})
        self.check("lead-002-plan", {"role": "lead", "lead_kind": "plan", "agent": ""})

    def test_unparseable(self):
        for stem in ("scratch", "t1-claude", "README", "7"):
            self.assertEqual(L.parse_log_stem(stem, BUILTINS)["role"], "other", stem)

    def test_tail_and_formatting(self):
        self.assertEqual(L.tail_text("a\nb\nc\n", 2), ("b\nc\n", 1))
        self.assertEqual(L.tail_text("a\nb\n", 5), ("a\nb\n", 0))
        self.assertEqual(L.tail_text("a\nb\n", 0), ("", 2))
        self.assertEqual(L.tail_text("a\nb", None), ("a\nb", 0))
        self.assertEqual(L.fmt_duration(114), "1.9 min")
        self.assertEqual(L.fmt_duration(42), "42s")
        self.assertEqual(L.fmt_duration(None), "")
        self.assertEqual(L.fmt_size(512), "512 B")
        self.assertEqual(L.fmt_size(2048), "2.0 KB")
        self.assertEqual(L.fmt_ago(100.0, now=100.0 + 300), "5m ago")
        self.assertEqual(L.fmt_ago(0), "-")


# --------------------------------------------------------------------------- collecting

class TestCollect(LogsBase):
    def test_entries_grouped_and_ordered(self):
        state = runs.load_state(self.proj)
        entries = L.collect_entries(self.logs, state)
        stems = [e["stem"] for e in entries]
        self.assertEqual(stems, ["scratch"] + [s for s, _, _ in LOGS])
        by = {e["stem"]: e for e in entries}
        self.assertEqual(by["scratch"]["kind"], "other")
        self.assertEqual(by["lead-003-review-claude"]["kind"], "lead:review")
        self.assertEqual(by["fix-ui-my-bot-1"]["agent"], "my-bot")  # known from state.json
        self.assertEqual(by["r2-t1-claude-1"]["task"], "r2-t1")
        t3 = by["t3-muse-1"]
        self.assertIsNone(t3["output"])
        self.assertTrue(t3["prompt"].endswith("t3-muse-1.prompt.md"))
        self.assertEqual(t3["size"], 0)
        self.assertEqual(by["t2-agy-1"]["result"], "revise")
        self.assertEqual(by["t2-agy-1"]["duration_sec"], 114.0)
        self.assertEqual(by["t2-agy-2"]["result"], "approved")
        self.assertIsNone(by["t3-muse-1"]["result"])

    def test_attempt_matching_falls_back_to_agent_order(self):
        entries = [
            {"stem": "t5-codex-1", "role": "task", "task": "t5", "agent": "codex", "attempt": 1},
            {"stem": "t5-agy-2", "role": "task", "task": "t5", "agent": "agy", "attempt": 2},
        ]
        state = {"tasks": [{"id": "t5", "attempts": [{"agent": "agy", "result": "A"},
                                                     {"agent": "codex", "result": "C"}]}]}
        recs = L.match_attempts(entries, state)
        self.assertEqual(recs["t5-codex-1"]["result"], "C")
        self.assertEqual(recs["t5-agy-2"]["result"], "A")

    def test_filters(self):
        entries = L.collect_entries(self.logs, runs.load_state(self.proj))
        self.assertEqual(len(L.filter_entries(entries, task="t2")), 2)
        self.assertEqual(len(L.filter_entries(entries, lead="")), 5)
        self.assertEqual(len(L.filter_entries(entries, lead="REVIEW")), 3)
        self.assertEqual({e["stem"] for e in L.filter_entries(entries, agent="claude")},
                         {"lead-001-plan-claude", "lead-002-review-claude", "lead-003-review-claude",
                          "lead-004-review-claude", "r2-t1-claude-1"})
        self.assertEqual(L.newest_entry(entries)["stem"], "lead-005-final-codex")
        self.assertEqual(L.newest_entry(L.filter_entries(entries, task="t2"))["stem"], "t2-agy-2")


# --------------------------------------------------------------------------- list

class TestList(LogsBase):
    def test_default_is_list(self):
        code, out = self.run_d()
        self.assertEqual(code, 0)
        for needle in ("lead:plan", "lead:review", "lead:final", "r2-t1", "my-bot", "call 5", "logsdemo",
                       "Modified", "omnicli logs t1"):
            self.assertIn(needle, out)

    def test_list_json(self):
        code, out = self.run_d("--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(len(data), len(LOGS) + 1)
        t3 = next(e for e in data if e["stem"] == "t3-muse-1")
        self.assertEqual((t3["kind"], t3["task"], t3["agent"], t3["attempt"]), ("task", "t3", "muse", 1))
        self.assertIsNone(t3["output"])
        self.assertIsNotNone(t3["prompt"])
        self.assertIsNotNone(t3["stderr"])
        lead = next(e for e in data if e["stem"] == "lead-005-final-codex")
        self.assertEqual((lead["kind"], lead["call"], lead["agent"]), ("lead:final", 5, "codex"))

    def test_list_filters(self):
        code, out = self.run_d("--agent", "claude", "--json")
        self.assertEqual({e["stem"] for e in json.loads(out)},
                         {"lead-001-plan-claude", "lead-002-review-claude", "lead-003-review-claude",
                          "lead-004-review-claude", "r2-t1-claude-1"})
        code, out = self.run_d("t2", "--list", "--json")
        self.assertEqual([e["stem"] for e in json.loads(out)], ["t2-agy-1", "t2-agy-2"])
        code, out = self.run_d("--lead", "review", "--list", "--json")
        self.assertEqual(len(json.loads(out)), 3)
        code, out = self.run_d("t2", "--list")
        self.assertEqual(code, 0)
        self.assertIn("agy", out)
        self.assertIn("2 of 12 calls", out)
        self.assertNotIn("codex", out)

    def test_list_no_match(self):
        code, out = self.run_d("--agent", "nobody")
        self.assertEqual(code, 1)
        self.assertIn("Agents in this run", out)
        self.assertIn("my-bot", out)

    def test_bad_tail(self):
        code, out = self.run_d("t2", "--tail", "-1")
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------- task view

class TestTaskView(LogsBase):
    def test_task_attempts_in_order(self):
        code, out = self.run_d("t2")
        self.assertEqual(code, 0)
        h1 = "t2 · agy · attempt 1 · result: revise · 1.9 min"
        h2 = "t2 · agy · attempt 2 · result: approved · 1.0 min"
        self.assertIn(h1, out)
        self.assertIn(h2, out)
        self.assertLess(out.index(h1), out.index(h2))
        self.assertIn("OUTPUT t2-agy-1 line 1", out)
        self.assertIn("OUTPUT t2-agy-2 line 5", out)
        self.assertIn("lead feedback: Add tests for the parser.", out)
        self.assertIn("verify exit 1", out)
        self.assertNotIn("PROMPT for", out)
        self.assertNotIn("STDERR of", out)
        self.assertNotIn("t1-codex", out)

    def test_prompt_and_stderr(self):
        code, out = self.run_d("t2", "--prompt")
        self.assertIn("PROMPT for t2-agy-1", out)
        self.assertIn("PROMPT for t2-agy-2", out)
        self.assertNotIn("OUTPUT t2", out)
        code, out = self.run_d("t2", "--stderr")
        self.assertIn("STDERR of t2-agy-2", out)
        self.assertNotIn("OUTPUT t2", out)
        code, out = self.run_d("t2", "--prompt", "--stderr")
        self.assertIn("PROMPT for t2-agy-1", out)
        self.assertIn("STDERR of t2-agy-1", out)
        self.assertIn("── prompt ──", out)

    def test_tail(self):
        code, out = self.run_d("t2", "--tail", "2")
        self.assertEqual(code, 0)
        self.assertNotIn("OUTPUT t2-agy-1 line 3", out)
        self.assertIn("OUTPUT t2-agy-1 line 4", out)
        self.assertIn("OUTPUT t2-agy-1 line 5", out)
        self.assertIn("3 earlier lines hidden", out)

    def test_task_json(self):
        code, out = self.run_d("t2", "--tail", "1", "--json")
        data = json.loads(out)
        self.assertEqual([e["attempt"] for e in data], [1, 2])
        self.assertEqual(data[0]["header"], "t2 · agy · attempt 1 · result: revise · 1.9 min")
        self.assertEqual(data[0]["content"]["output"], "OUTPUT t2-agy-1 line 5\n")
        self.assertEqual(data[0]["record"]["feedback"], "Add tests for the parser.")

    def test_dashed_ids_and_custom_agent(self):
        code, out = self.run_d("r2-t1")
        self.assertIn("r2-t1 · claude · attempt 1 · result: approved · 30s", out)
        code, out = self.run_d("fix-ui")
        self.assertIn("fix-ui · my-bot · attempt 1 · result: agent error: exit code 3 · 2s", out)

    def test_running_task_without_output(self):
        code, out = self.run_d("t3")
        self.assertEqual(code, 0)
        self.assertIn("t3 · muse · attempt 1 · result: in progress", out)
        self.assertIn("no output yet", out)
        code, out = self.run_d("t3", "--stderr")
        self.assertIn("STDERR of t3-muse-1", out)

    def test_task_without_logs(self):
        code, out = self.run_d("t4")
        self.assertEqual(code, 0)
        self.assertIn("t4 has no logs yet (status: pending)", out)

    def test_unknown_task_lists_valid_ids(self):
        code, out = self.run_d("t99")
        self.assertEqual(code, 1)
        self.assertIn("No task 't99'", out)
        self.assertIn("t1, t2, r2-t1, fix-ui, t3, t4", out)
        code, out = self.run_d("t99", "--json")
        self.assertIn("t1, t2", json.loads(out)["error"])

    def test_case_insensitive_and_agent_filter(self):
        code, out = self.run_d("T2")
        self.assertIn("t2 · agy · attempt 1", out)
        code, out = self.run_d("t2", "--agent", "agy")
        self.assertIn("attempt 2", out)
        code, out = self.run_d("t2", "--agent", "codex")
        self.assertEqual(code, 1)
        self.assertIn("ran on: agy", out)

    def test_task_and_lead_conflict(self):
        code, out = self.run_d("t2", "--lead")
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------- lead view

class TestLeadView(LogsBase):
    def test_all_lead_calls_newest_last(self):
        code, out = self.run_d("--lead")
        self.assertEqual(code, 0)
        idx = [out.index(f"call {n}") for n in range(1, 6)]
        self.assertEqual(idx, sorted(idx))
        self.assertIn("lead · plan · claude · call 1", out)
        self.assertIn("lead · final · codex · call 5", out)
        self.assertIn("OUTPUT lead-001-plan-claude line 1", out)
        self.assertNotIn("OUTPUT t2", out)

    def test_lead_kind_and_agent(self):
        code, out = self.run_d("--lead", "review")
        self.assertEqual(out.count("lead · review"), 3)
        self.assertNotIn("lead · plan", out)
        code, out = self.run_d("--lead", "review", "--agent", "codex")
        self.assertEqual(code, 1)
        self.assertIn("Kinds in this run: final, plan, review", out)
        code, out = self.run_d("--lead", "final", "--json")
        data = json.loads(out)
        self.assertEqual([(e["agent"], e["call"]) for e in data], [("codex", 5)])
        self.assertIn("OUTPUT lead-005-final-codex", data[0]["content"]["output"])
        code, out = self.run_d("--lead", "--prompt", "--tail", "1")
        self.assertIn("PROMPT for lead-004-review-claude", out)


# --------------------------------------------------------------------------- which run

class TestRunResolution(LogsBase):
    def test_no_runs_anywhere(self):
        empty = Path(tempfile.mkdtemp(prefix="omni-empty-", dir=str(self.root)))
        os.chdir(str(empty))
        code, out = self.run_cmd()
        self.assertEqual(code, 1)
        self.assertIn("No team runs found yet", out)
        self.assertIn('omnicli team "<goal>"', out)

    def test_cwd_run(self):
        os.chdir(str(self.proj))
        code, out = self.run_cmd("t1")
        self.assertEqual(code, 0)
        self.assertIn("t1 · codex · attempt 1 · result: approved · 45s", out)

    def test_run_ref_and_most_recent(self):
        runs.register_run({"dir": str(self.proj), "project_name": "logsdemo", "goal": STATE["goal"],
                           "phase": "running"})
        code, out = self.run_cmd("--run", "logsdemo", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)), len(LOGS) + 1)
        code, out = self.run_cmd("--run", "no-such-thing")
        self.assertEqual(code, 1)
        self.assertIn("omnicli runs", out)
        empty = Path(tempfile.mkdtemp(prefix="omni-empty-", dir=str(self.root)))
        os.chdir(str(empty))
        code, out = self.run_cmd("t2", "--tail", "0")
        self.assertEqual(code, 0)
        self.assertIn("t2 · agy · attempt 2", out)

    def test_dir_without_run_or_logs(self):
        empty = Path(tempfile.mkdtemp(prefix="omni-empty-", dir=str(self.root)))
        code, out = self.run_cmd("-d", str(empty))
        self.assertEqual(code, 1)
        self.assertIn("No team run in", out)
        planned = build_project(Path(tempfile.mkdtemp(dir=str(self.root))), with_logs=False)
        code, out = self.run_cmd("-d", str(planned))
        self.assertEqual(code, 0)
        self.assertIn("No logs yet", out)
        code, out = self.run_cmd("-d", str(planned), "--json")
        self.assertEqual(json.loads(out), [])


# --------------------------------------------------------------------------- follow

class TestFollow(LogsBase):
    def later(self, delay, fn):
        def run():
            time.sleep(delay)
            fn()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        return th

    def test_follow_newest_overall_picks_up_appends(self):
        target = self.logs / "lead-005-final-codex.md"

        def append():
            with open(target, "a", encoding="utf-8") as fh:
                fh.write("APPENDED LINE\n")
        th = self.later(0.3, append)
        code, out = self.run_d("--follow", follow_max_seconds=1.2, follow_poll=0.05)
        th.join()
        self.assertEqual(code, 0)
        self.assertIn("lead · final · codex · call 5", out)
        self.assertIn("OUTPUT lead-005-final-codex line 5", out)
        self.assertIn("APPENDED LINE", out)
        self.assertEqual(out.count("OUTPUT lead-005-final-codex line 1"), 1)

    def test_follow_task_waits_for_output_file(self):
        target = self.logs / "t3-muse-1.md"
        th = self.later(0.3, lambda: target.write_text("FINAL OUTPUT OF t3\n", encoding="utf-8"))
        code, out = self.run_d("t3", "--follow", follow_max_seconds=1.2, follow_poll=0.05)
        th.join()
        self.assertEqual(code, 0)
        self.assertIn("t3 · muse · attempt 1", out)
        self.assertIn("waiting for t3-muse-1.md", out)
        self.assertIn("FINAL OUTPUT OF t3", out)

    def test_follow_switches_to_new_attempt(self):
        def new_attempt():
            (self.logs / "t2-agy-3.prompt.md").write_text("PROMPT 3\n", encoding="utf-8")
            (self.logs / "t2-agy-3.md").write_text("THIRD ATTEMPT OUTPUT\n", encoding="utf-8")
        th = self.later(0.3, new_attempt)
        code, out = self.run_d("t2", "--follow", "--tail", "1", follow_max_seconds=1.2, follow_poll=0.05)
        th.join()
        self.assertIn("t2 · agy · attempt 2", out)
        self.assertIn("OUTPUT t2-agy-2 line 5", out)
        self.assertNotIn("OUTPUT t2-agy-2 line 4", out)  # --tail 1 trims what existed before
        self.assertIn("t2 · agy · attempt 3", out)
        self.assertIn("THIRD ATTEMPT OUTPUT", out)
        self.assertLess(out.index("attempt 2"), out.index("attempt 3"))

    def test_follow_stderr_stream(self):
        target = self.logs / "t3-muse-1.stderr.log"

        def append():
            with open(target, "a", encoding="utf-8") as fh:
                fh.write("still working...\n")
        th = self.later(0.2, append)
        code, out = self.run_d("t3", "--follow", "--stderr", follow_max_seconds=1.0, follow_poll=0.05)
        th.join()
        self.assertIn("STDERR of t3-muse-1", out)
        self.assertIn("still working...", out)

    def test_follow_handles_ctrl_c_and_bad_combos(self):
        def pick():
            raise KeyboardInterrupt
        buf = io.StringIO()
        self.assertEqual(L.follow_logs(pick, self.logs, out=buf), 0)
        code, out = self.run_d("--follow", "--json")
        self.assertEqual(code, 2)
        code, out = self.run_d("--follow", "--list")
        self.assertEqual(code, 2)


# --------------------------------------------------------------------------- CLI wiring

class TestCliWiring(LogsBase):
    def test_main_dispatches_logs(self):
        try:
            from omnicli import cli
        except Exception as ex:  # cli.py may be mid-edit by another change
            self.skipTest(f"omnicli.cli not importable: {ex}")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--no-banner", "logs", "-d", str(self.proj), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(buf.getvalue())), len(LOGS) + 1)


if __name__ == "__main__":
    unittest.main()
