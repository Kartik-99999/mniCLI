"""
Tests for `omnicli usage` (omnicli/commands/usage.py).

Every test gets its own temp OMNICLI_HOME with a synthetic ledger; no real AI CLI is
ever run (subprocess is patched for the --native tests).
"""

import os
import tempfile

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

import argparse  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from omnicli import agents as ag  # noqa: E402
from omnicli import ledger, paths, runs  # noqa: E402
from omnicli.commands import usage  # noqa: E402

DAY = 86400


def _parser():
    p = argparse.ArgumentParser(prog="omnicli")
    subs = p.add_subparsers(dest="subcommand")
    usage.add_parser(subs)
    return p


class UsageBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="omni-usage-home-"))
        self.work = Path(tempfile.mkdtemp(prefix="omni-usage-work-"))
        self._old = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = str(self.home)
        usage._name_cache.clear()

    def tearDown(self):
        os.environ["OMNICLI_HOME"] = self._old or ""
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.work, ignore_errors=True)

    # -- fixtures
    def add(self, agent, kind="task", run_dir="", dur=10.0, ok=True, ts=None, pin=400, pout=80,
            timed_out=False, task_id=""):
        e = ledger.make_entry(agent, "edit" if kind == "task" else "readonly",
                              {"kind": kind, "run_dir": run_dir, "task_id": task_id},
                              dur, 0 if ok else 1, ok, timed_out, "p" * pin, "o" * pout)
        if ts is not None:
            e["ts"] = ts
        ledger.record(e)
        return e

    def project(self, name, register=True, phase="complete"):
        d = self.work / name.lower().replace(" ", "-")
        sd = d / ".omnicli" / "team"
        sd.mkdir(parents=True)
        (sd / "state.json").write_text(json.dumps({"version": 1, "goal": f"build {name}", "project_name": name,
                                                   "phase": phase, "tasks": []}))
        rd = str(d.resolve())
        if register:
            runs.register_run({"dir": rd, "goal": f"build {name}", "project_name": name, "phase": phase})
        return rd

    # -- running the command
    def run_cmd(self, *argv):
        args = _parser().parse_args(["usage"] + list(argv))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = args.func(args)
        return code, buf.getvalue()

    def run_json(self, *argv):
        code, out = self.run_cmd(*(list(argv) + ["--json"]))
        self.assertEqual(code, 0, out)
        return json.loads(out)


class TestHelpers(unittest.TestCase):
    def test_human_duration(self):
        self.assertEqual(usage.human_duration(0), "0s")
        self.assertEqual(usage.human_duration(42.4), "42s")
        self.assertEqual(usage.human_duration(192), "3m12s")
        self.assertEqual(usage.human_duration(3600 + 4 * 60 + 5), "1h04m")

    def test_parser_contract(self):
        self.assertEqual(usage.NAMES, ["usage"])
        args = _parser().parse_args(["usage"])
        self.assertIs(args.func, usage.handler)
        self.assertEqual(args.by, "agent")
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                _parser().parse_args(["usage", "--days", "3", "--since", "2026-01-01"])
            with self.assertRaises(SystemExit):
                _parser().parse_args(["usage", "--run", "1", "-d", "."])
            with self.assertRaises(SystemExit):
                _parser().parse_args(["usage", "--by", "model"])


class TestEmpty(UsageBase):
    def test_empty_ledger_text(self):
        code, out = self.run_cmd()
        self.assertEqual(code, 0)
        self.assertIn("No AI usage recorded yet", out)
        self.assertIn("omnicli team", out)
        self.assertIn("from now on", out)

    def test_empty_ledger_json_shape(self):
        doc = self.run_json()
        self.assertEqual(set(doc), {"window", "group_by", "groups", "totals", "quota_resets"})
        self.assertEqual(doc["groups"], {})
        self.assertEqual(doc["totals"]["calls"], 0)
        self.assertEqual(doc["totals"]["seconds"], 0.0)
        self.assertIsNone(doc["totals"]["top_run"])
        self.assertEqual(doc["window"]["label"], "all time")

    def test_no_match_with_filters(self):
        self.add("claude")
        code, out = self.run_cmd("--agent", "codex")
        self.assertEqual(code, 0)
        self.assertIn("No AI calls match", out)
        self.assertIn("agent=codex", out)
        self.assertIn("1 call recorded overall", out)


class TestGrouping(UsageBase):
    def setUp(self):
        super().setUp()
        self.shop = self.project("Shop")
        self.blog = self.project("Blog")
        self.add("claude", "plan", self.shop, dur=30, pin=4000, pout=800)
        self.add("claude", "review", self.shop, dur=20, pin=2000, pout=400)
        self.add("agy", "task", self.shop, dur=100, ok=False, pin=1000, pout=0, task_id="t1")
        self.add("agy", "task", self.shop, dur=200, pin=1000, pout=2000, task_id="t1")
        self.add("muse", "task", self.blog, dur=50, pin=800, pout=400, task_id="t2")
        self.add("muse", "ping", "", dur=2, pin=60, pout=4)

    def test_by_agent_and_totals(self):
        doc = self.run_json()
        self.assertEqual(doc["group_by"], "agent")
        g = doc["groups"]
        self.assertEqual(list(g), ["agy", "muse", "claude"])  # by time, most first: 300s, 52s, 50s
        self.assertEqual((g["agy"]["calls"], g["agy"]["ok"], g["agy"]["failed"]), (2, 1, 1))
        self.assertEqual(g["agy"]["seconds"], 300.0)
        self.assertEqual(g["agy"]["failure_rate"], 0.5)
        self.assertEqual(g["claude"]["est_tokens_in"], 1500)
        self.assertEqual(g["claude"]["est_tokens_out"], 300)
        t = doc["totals"]
        self.assertEqual((t["calls"], t["ok"], t["failed"]), (6, 5, 1))
        self.assertEqual(t["seconds"], 402.0)
        self.assertEqual(t["est_tokens_in"], sum(x["est_tokens_in"] for x in g.values()))
        self.assertEqual(t["est_tokens_out"], sum(x["est_tokens_out"] for x in g.values()))
        self.assertEqual(t["failures_by_agent"], {"agy": {"failed": 1, "calls": 2, "rate": 0.5, "timed_out": 0}})
        self.assertEqual(t["top_run"]["dir"], self.shop)
        self.assertEqual(t["top_run"]["name"], "Shop")
        self.assertEqual(t["top_run"]["calls"], 4)

    def test_by_kind(self):
        g = self.run_json("--by", "kind")["groups"]
        self.assertEqual(set(g), {"plan", "review", "task", "ping"})
        self.assertEqual(g["task"]["calls"], 3)
        self.assertEqual(g["ping"]["calls"], 1)

    def test_by_run_uses_project_names(self):
        orphan = self.project("Orphan Project", register=False)  # only state.json knows its name
        self.add("codex", "task", orphan, dur=1)
        doc = self.run_json("--by", "run")
        g = doc["groups"]
        self.assertEqual(g[self.shop]["name"], "Shop")
        self.assertEqual(g[self.blog]["name"], "Blog")
        self.assertEqual(g[orphan]["name"], "Orphan Project")
        self.assertEqual(g[orphan]["dir"], orphan)
        self.assertIn("", g)  # pings have no run
        code, out = self.run_cmd("--by", "run")
        self.assertEqual(code, 0)
        self.assertIn("Shop", out)
        self.assertIn("Orphan Project", out)
        self.assertNotIn(self.blog, out)  # the raw path is replaced by the project name

    def test_by_day_groups_by_local_date(self):
        old = time.time() - 3 * DAY
        self.add("claude", "plan", self.shop, dur=5, ts=old)
        g = self.run_json("--by", "day")["groups"]
        days = list(g)
        self.assertEqual(days, sorted(days))  # chronological
        self.assertEqual(days[0], time.strftime("%Y-%m-%d", time.localtime(old)))
        self.assertEqual(days[-1], time.strftime("%Y-%m-%d", time.localtime()))
        self.assertEqual(g[days[0]]["calls"], 1)
        self.assertEqual(sum(x["calls"] for x in g.values()), 7)

    def test_text_output(self):
        code, out = self.run_cmd()
        self.assertEqual(code, 0)
        for needle in ("OmniCLI usage", "all time", "~Tokens in", "~Tokens out", "Total", "5m00s",
                       "Most expensive run: Shop", "Failure rate:", "agy 1/2 (50%", "estimates (characters / 4)",
                       "omnicli usage --native"):
            self.assertIn(needle, out)
        total_line = [l for l in out.splitlines() if l.strip().startswith("Total")][0]
        self.assertIn("6", total_line)
        self.assertIn("6m42s", total_line)

    def test_no_failure_line_without_failures(self):
        code, out = self.run_cmd("--agent", "claude")
        self.assertNotIn("Failure rate:", out)


class TestFilters(UsageBase):
    def setUp(self):
        super().setUp()
        self.shop = self.project("Shop")
        self.blog = self.project("Blog")
        now = time.time()
        self.add("claude", "plan", self.shop, dur=10, ts=now - 10 * DAY)
        self.add("agy", "task", self.shop, dur=20, ts=now - 2 * DAY)
        self.add("muse", "task", self.blog, dur=30, ts=now - 60)

    def test_agent_filter(self):
        doc = self.run_json("--agent", "agy")
        self.assertEqual(list(doc["groups"]), ["agy"])
        self.assertEqual(doc["totals"]["calls"], 1)
        self.assertEqual(doc["window"]["agent"], "agy")

    def test_run_by_name_substring(self):
        doc = self.run_json("--run", "blo")
        self.assertEqual(doc["window"]["run_dir"], self.blog)
        self.assertEqual(doc["window"]["run_name"], "Blog")
        self.assertEqual(list(doc["groups"]), ["muse"])

    def test_run_by_index_and_path(self):
        newest = runs.list_runs()[0]["dir"]
        doc = self.run_json("--run", "1")
        self.assertEqual(doc["window"]["run_dir"], newest)
        doc = self.run_json("--run", self.shop)
        self.assertEqual(doc["totals"]["calls"], 2)

    def test_dir_flag_resolves_path(self):
        rel = os.path.relpath(self.shop)
        doc = self.run_json("-d", rel)
        self.assertEqual(doc["window"]["run_dir"], self.shop)
        self.assertEqual(doc["window"]["run_name"], "Shop")
        self.assertEqual(doc["totals"]["calls"], 2)

    def test_unknown_run_is_friendly_error(self):
        code, out = self.run_cmd("--run", "no-such-project-xyz")
        self.assertEqual(code, 2)
        self.assertIn("omnicli runs", out)
        code, out = self.run_cmd("--run", "no-such-project-xyz", "--json")
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(out))

    def test_days(self):
        self.assertEqual(self.run_json("--days", "1")["totals"]["calls"], 1)
        doc = self.run_json("--days", "7")
        self.assertEqual(doc["totals"]["calls"], 2)
        self.assertEqual(doc["window"]["days"], 7)
        self.assertIn("last 7 days", doc["window"]["label"])
        self.assertEqual(self.run_json("--days", "30")["totals"]["calls"], 3)

    def test_since(self):
        since = datetime.fromtimestamp(time.time() - 5 * DAY).strftime("%Y-%m-%d")
        doc = self.run_json("--since", since)
        self.assertEqual(doc["totals"]["calls"], 2)
        self.assertEqual(doc["window"]["since_date"], since)
        self.assertEqual(doc["window"]["since"], time.mktime(datetime.strptime(since, "%Y-%m-%d").timetuple()))

    def test_bad_window_values(self):
        code, out = self.run_cmd("--since", "15/11/2026")
        self.assertEqual(code, 2)
        self.assertIn("2026-11-01", out)
        code, _ = self.run_cmd("--days", "0")
        self.assertEqual(code, 2)

    def test_combined_filters(self):
        doc = self.run_json("--days", "7", "--run", "shop", "--agent", "agy", "--by", "kind")
        self.assertEqual(doc["groups"], {"task": doc["groups"]["task"]})
        self.assertEqual(doc["totals"]["calls"], 1)
        code, out = self.run_cmd("--days", "7", "--run", "shop")
        self.assertIn("run=Shop", out)
        self.assertNotIn("Most expensive run", out)  # pointless when scoped to one run


def _completed(cmd, out='{"remaining": 42}', code=0):
    return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")


class TestNative(UsageBase):
    SPECS = {
        "claude": {"id": "claude", "name": "Claude Code", "bin": "claude"},
        "codex": {"id": "codex", "name": "Codex CLI", "bin": "codex"},
        "agy": {"id": "agy", "name": "Antigravity CLI", "bin": "agy"},
        "muse": {"id": "muse", "name": "Muse Code", "bin": "muse"},
        "gemini": {"id": "gemini", "name": "Gemini CLI", "bin": "gemini"},
        "mybot": {"id": "mybot", "name": "My Bot", "bin": "mybot"},
    }

    def _resolve(self, spec):
        return None if spec["bin"] == "gemini" else "/fake/bin/" + os.path.basename(spec["bin"])

    def _patched(self, run_mock, specs=None):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(ag, "load_agent_specs", return_value=dict(specs or self.SPECS)))
        stack.enter_context(mock.patch.object(ag, "resolve_bin", side_effect=self._resolve))
        stack.enter_context(mock.patch.object(usage.subprocess, "run", run_mock))
        return stack

    def test_only_verified_commands_run(self):
        self.add("claude")
        run = mock.Mock(side_effect=lambda cmd, **kw: _completed(cmd, "Weekly allowance: 80% left"))
        with self._patched(run), mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
            code, out = self.run_cmd("--native")
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 1)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd, ["/fake/bin/muse"] + usage.NATIVE_USAGE_COMMANDS["muse"]["args"])
        kw = run.call_args[1]
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["timeout"], 20)
        self.assertNotIn("CLAUDECODE", kw["env"])
        self.assertIn("Native usage reports", out)
        self.assertIn("Weekly allowance: 80% left", out)
        self.assertIn("no usage command", out)
        self.assertIn("not installed", out)  # gemini

    def test_every_run_command_comes_from_verified_dict(self):
        seen = []
        run = mock.Mock(side_effect=lambda cmd, **kw: seen.append(cmd) or _completed(cmd))
        with self._patched(run):
            native = usage.collect_native()
            usage.collect_native(as_json=True)
        allowed = []
        for name, entry in usage.NATIVE_USAGE_COMMANDS.items():
            allowed.append(["/fake/bin/" + entry["bin"]] + entry["args"])
            allowed.append(["/fake/bin/" + entry["bin"]] + entry.get("json_args", entry["args"]))
        self.assertTrue(seen)
        for cmd in seen:
            self.assertIn(cmd, allowed)
        for name in ("claude", "codex", "agy", "mybot"):
            self.assertIsNone(native[name]["command"])
            self.assertEqual(native[name]["note"], "no usage command")
        self.assertEqual(native["gemini"]["note"], "not installed")
        self.assertIn("hint", native["claude"])

    def test_json_native_shape(self):
        run = mock.Mock(side_effect=lambda cmd, **kw: _completed(cmd, '{"remaining": 42}'))
        with self._patched(run):
            doc = self.run_json("--native")
        self.assertEqual(set(doc), {"window", "group_by", "groups", "totals", "native", "quota_resets"})
        m = doc["native"]["muse"]
        self.assertEqual(m["command"], ["muse", "usage", "--json"])
        self.assertEqual(m["data"], {"remaining": 42})
        self.assertEqual(m["exit_code"], 0)
        self.assertEqual(doc["native"]["codex"]["note"], "no usage command")

    def test_id_reused_by_other_program_is_not_run(self):
        specs = {"muse": {"id": "muse", "name": "Not Muse", "bin": "/opt/tools/somethingelse"}}
        run = mock.Mock(side_effect=AssertionError("must not run"))
        with self._patched(run, specs):
            native = usage.collect_native()
        run.assert_not_called()
        self.assertEqual(native["muse"]["note"], "no usage command")

    def test_timeout_and_failure_are_reported(self):
        run = mock.Mock(side_effect=subprocess.TimeoutExpired(["muse", "usage"], 20))
        with self._patched(run):
            native = usage.collect_native()
        self.assertIn("timed out", native["muse"]["error"])
        run = mock.Mock(side_effect=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not logged in"))
        with self._patched(run):
            code, out = self.run_cmd("--native")
        self.assertEqual(code, 0)
        self.assertIn("not logged in", out)

    def test_native_on_empty_ledger_still_runs(self):
        run = mock.Mock(side_effect=lambda cmd, **kw: _completed(cmd, "allowance ok"))
        with self._patched(run):
            code, out = self.run_cmd("--native")
        self.assertIn("No AI usage recorded yet", out)
        self.assertIn("allowance ok", out)

    def test_without_native_no_subprocess(self):
        self.add("claude")
        run = mock.Mock(side_effect=AssertionError("must not run"))
        with mock.patch.object(usage.subprocess, "run", run):
            self.run_cmd()
            self.run_json()
        run.assert_not_called()


class TestCliIntegration(UsageBase):
    def test_main_dispatches_usage(self):
        from omnicli import cli
        self.add("claude", "plan", "", dur=3)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--no-banner", "usage", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buf.getvalue())["totals"]["calls"], 1)
        self.assertEqual(paths.ledger_path().parent, self.home)


if __name__ == "__main__":
    unittest.main()
