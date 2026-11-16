"""
Tests for `omnicli doctor` (omnicli/commands/doctor.py).

No real AI CLI is ever run: built-in agents are disabled through a fixture agents.json,
the only "installed" agents use bin=sys.executable (so `--version` is just
`python3 --version`), git and the dev-tool probe are faked, and --check's
agents.check_health is patched.
"""

import os
import tempfile

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")
os.environ["NO_COLOR"] = "1"

import argparse  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from collections import namedtuple  # noqa: E402
from contextlib import redirect_stderr, redirect_stdout  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from omnicli import agents as ag  # noqa: E402
from omnicli import paths, runs, ui  # noqa: E402
from omnicli.commands import doctor  # noqa: E402

Usage = namedtuple("Usage", "total used free")
GB = 1024 ** 3
REAL_CAPTURE = doctor._capture


def fake_spec(name):
    return {"name": name, "bin": sys.executable, "strengths": "t",
            "readonly": ["-c", "print('PONG')"], "edit": ["-c", "print('ok')"]}


class DoctorBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="omni-doctor-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self._old_home = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = str(self.home)
        self.git_identity = {"user.name": "Test User", "user.email": "test@example.com"}
        self.which_map = {"omnicli": str(doctor._checkout_bin()), "git": "/usr/bin/git"}
        self.write_agents({"fakeok": fake_spec("fakeok")})

        patches = [
            mock.patch.object(doctor, "_which", side_effect=lambda n: self.which_map.get(n)),
            mock.patch.object(doctor, "_capture", side_effect=self._fake_capture),
            mock.patch.object(doctor, "_tool_report", return_value=(["python3", "git"], [])),
            mock.patch.object(doctor.shutil, "disk_usage", return_value=Usage(500 * GB, 100 * GB, 400 * GB)),
            # ui decides on colour at import time; force plain text even if another test module
            # imported it first while stdout was a terminal
            mock.patch.object(ui, "USE_COLOR", False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        for p in self.tmp.rglob("*"):
            try:
                os.chmod(p, stat.S_IRWXU)
            except OSError:
                pass
        os.environ["OMNICLI_HOME"] = self._old_home or ""
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------- fixtures
    def _fake_capture(self, cmd, timeout, env=None):
        if cmd[0] == "/usr/bin/git":
            if cmd[1:] == ["--version"]:
                return 0, "git version 9.9.9\n", ""
            if cmd[1:3] == ["config", "--global"]:
                value = self.git_identity.get(cmd[3], "")
                return (0, value + "\n", "") if value else (1, "", "")
        return REAL_CAPTURE(cmd, timeout, env)

    def write_agents(self, agents, disabled=None, raw=None):
        path = paths.agents_config_path()
        if raw is not None:
            path.write_text(raw, encoding="utf-8")
            return
        cfg = {"agents": agents, "disabled": list(ag.BUILTIN_AGENTS) if disabled is None else disabled}
        path.write_text(json.dumps(cfg), encoding="utf-8")

    def write_health(self, cache):
        paths.health_cache_path().write_text(json.dumps(cache), encoding="utf-8")

    def ok_health(self, *names, age=120):
        return {n: {"ok": True, "reason": "ok (1s)", "checked_at": time.time() - age} for n in names}

    def make_project(self, name, phase="running", pid=0, register=True):
        d = self.tmp / name
        state_dir = d / ".omnicli" / "team"
        state_dir.mkdir(parents=True)
        state = {"version": 1, "goal": f"build {name}", "project_name": name, "phase": phase, "pid": pid,
                 "round": 1, "tasks": [{"id": "t1", "status": "running"}]}
        (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
        if register:
            runs.register_run({"dir": str(d), "goal": state["goal"], "project_name": name, "phase": phase, "pid": pid})
        return d

    def dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    # ------------------------------------------------------------- running
    def run_doctor(self, *argv):
        parser = argparse.ArgumentParser(prog="omnicli")
        sub = parser.add_subparsers(dest="subcommand")
        doctor.add_parser(sub)
        args = parser.parse_args(["doctor", *argv])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv):
        code, out, err = self.run_doctor("--json", *argv)
        return code, json.loads(out), err

    @staticmethod
    def rows(data, section, name=None):
        return [c for c in data["checks"] if c["section"] == section and (name is None or c["name"] == name)]

    def row(self, data, section, name):
        found = self.rows(data, section, name)
        self.assertEqual(len(found), 1, f"expected one {section}/{name} row, got {found}")
        return found[0]


class TestContractAndOutput(DoctorBase):
    def test_names_and_parser(self):
        self.assertEqual(doctor.NAMES, ["doctor"])
        parser = argparse.ArgumentParser()
        doctor.add_parser(parser.add_subparsers(dest="subcommand"))
        args = parser.parse_args(["doctor", "--check", "--fix", "--json"])
        self.assertTrue(args.check and args.fix and args.json)
        self.assertIs(args.func, doctor.handler)

    def test_help_warns_that_check_sends_a_prompt(self):
        parser = argparse.ArgumentParser()
        doctor.add_parser(parser.add_subparsers(dest="subcommand"))
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            parser.parse_args(["doctor", "--help"])
        self.assertIn("one tiny prompt", " ".join(out.getvalue().split()))

    def test_healthy_setup_passes(self):
        self.write_health(self.ok_health("fakeok"))
        code, data, _ = self.run_json()
        self.assertEqual(code, 0)
        self.assertEqual(data["summary"]["failures"], 0)
        self.assertEqual(data["summary"]["warnings"], 0, [c for c in data["checks"] if c["status"] == "WARN"])
        agent = self.row(data, "AI agents", "fakeok")
        self.assertEqual(agent["status"], "PASS")
        self.assertIn("Python 3", agent["detail"])  # `<bin> --version` really ran (bin = python)
        self.assertIn("ready", agent["detail"])
        self.assertEqual(self.row(data, "AI agents", "ready agents")["status"], "PASS")

    def test_json_shape(self):
        code, data, _ = self.run_json()
        self.assertEqual(set(data), {"checks", "fixes", "summary"})
        self.assertEqual({c["section"] for c in data["checks"]}, set(doctor.SECTIONS))
        for c in data["checks"]:
            self.assertEqual(set(c), {"section", "status", "name", "detail", "fix_hint", "fixable"})
            self.assertIn(c["status"], {"PASS", "WARN", "FAIL", "INFO", "FIXED"})
        for key in ("passed", "warnings", "failures", "fixed", "exit_code", "fresh_check"):
            self.assertIn(key, data["summary"])
        self.assertEqual(data["fixes"], [])

    def test_text_output_sections_and_verdict(self):
        self.write_health(self.ok_health("fakeok"))
        code, out, _ = self.run_doctor()
        self.assertEqual(code, 0)
        for section in doctor.SECTIONS:
            self.assertIn(f"\n{section}\n", out)
        self.assertRegex(out, r"\d+ passed · 0 warnings · 0 failures")
        self.assertNotIn("\033[", out)  # plain text when colour is off

    def test_crashing_check_is_reported_not_raised(self):
        with mock.patch.object(doctor, "check_disk_space", side_effect=RuntimeError("boom")):
            code, data, _ = self.run_json()
        crashed = [c for c in data["checks"] if "crashed" in c["detail"]]
        self.assertEqual(len(crashed), 1)
        self.assertEqual(crashed[0]["status"], "WARN")
        self.assertIn("boom", crashed[0]["detail"])


class TestInstall(DoctorBase):
    def test_python_version(self):
        self.assertEqual(doctor.check_python((3, 8, 10)).status, "FAIL")
        self.assertIn("3.9", doctor.check_python((3, 8, 10)).detail)
        self.assertEqual(doctor.check_python((3, 9, 0)).status, "PASS")
        self.assertEqual(doctor.check_python().status, "PASS")

    def test_package_version(self):
        c = doctor.check_package()
        self.assertEqual(c.status, "PASS")
        import omnicli
        self.assertIn(omnicli.__version__, c.detail)

    def test_command_on_path(self):
        self.assertEqual(doctor.check_command_on_path().status, "PASS")
        self.which_map["omnicli"] = None
        missing = doctor.check_command_on_path()
        self.assertEqual(missing.status, "WARN")
        self.assertIn("ln -sf", missing.hint)
        self.assertIn(".local/bin", missing.hint)
        other = self.tmp / "other-omnicli"
        other.write_text("#!/bin/sh\n")
        self.which_map["omnicli"] = str(other)
        elsewhere = doctor.check_command_on_path()
        self.assertEqual(elsewhere.status, "WARN")
        self.assertIn("not this checkout", elsewhere.detail)


class TestEnvironment(DoctorBase):
    def test_git_and_identity_pass(self):
        self.assertEqual(doctor.check_git().status, "PASS")
        ident = doctor.check_git_identity()
        self.assertEqual(ident.status, "PASS")
        self.assertIn("Test User <test@example.com>", ident.detail)

    def test_git_missing_warns(self):
        self.which_map["git"] = None
        git = doctor.check_git()
        self.assertEqual(git.status, "WARN")
        self.assertIn("checkpoint", git.detail)
        self.assertEqual(doctor.check_git_identity().status, "INFO")

    def test_git_identity_missing_email_warns(self):
        self.git_identity["user.email"] = ""
        c = doctor.check_git_identity()
        self.assertEqual(c.status, "WARN")
        self.assertIn('"OmniCLI"', c.detail)
        self.assertIn("git config --global user.email", c.hint)

    def test_home_missing_warns_and_fix_creates_it(self):
        missing = self.tmp / "nohome"
        os.environ["OMNICLI_HOME"] = str(missing)
        with mock.patch.object(doctor, "_load_specs", return_value={"fakeok": dict(fake_spec("fakeok"), id="fakeok")}):
            code, data, _ = self.run_json()
            home = self.row(data, "Environment", "OmniCLI home")
            self.assertEqual((home["status"], home["fixable"]), ("WARN", True))
            self.assertFalse(missing.exists(), "doctor without --fix must not create anything")
            code, data, _ = self.run_json("--fix")
        self.assertTrue(missing.is_dir())
        self.assertEqual(self.row(data, "Environment", "OmniCLI home")["status"], "FIXED")
        self.assertTrue(any("created" in f["message"] for f in data["fixes"]))
        self.assertEqual(code, 0)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can write anywhere")
    def test_home_not_writable_fails(self):
        os.chmod(self.home, stat.S_IRUSR | stat.S_IXUSR)
        code, data, _ = self.run_json()
        self.assertEqual(self.row(data, "Environment", "OmniCLI home")["status"], "FAIL")
        self.assertEqual(code, 1)

    def test_low_disk_space_warns(self):
        with mock.patch.object(doctor.shutil, "disk_usage", return_value=Usage(100 * GB, 99 * GB, GB)):
            c = doctor.check_disk_space()
        self.assertEqual(c.status, "WARN")
        self.assertIn("1.0 GB", c.detail)
        self.assertEqual(doctor.check_disk_space().status, "PASS")

    def test_missing_dev_tools_warn(self):
        with mock.patch.object(doctor, "_tool_report", return_value=(["python3"], ["node", "python3 -m pytest"])):
            c = doctor.check_dev_tools()
        self.assertEqual(c.status, "WARN")
        self.assertIn("python3 -m pytest", c.detail)
        self.assertEqual(doctor.check_dev_tools().status, "PASS")


class TestConfig(DoctorBase):
    def test_no_config_files_pass(self):
        paths.agents_config_path().unlink()
        self.assertEqual([c.status for c in doctor.check_agents_config()], ["PASS"])
        self.assertEqual([c.status for c in doctor.check_settings({})], ["PASS"])

    def test_invalid_agents_json_fails_with_line(self):
        self.write_agents(None, raw='{\n  "agents": {\n    "x": {},\n  }\n}')
        checks = doctor.check_agents_config()
        self.assertEqual(checks[0].status, "FAIL")
        self.assertIn("line 4", checks[0].detail)
        with mock.patch.object(doctor, "_load_specs", return_value={"fakeok": dict(fake_spec("fakeok"), id="fakeok")}):
            code, data, _ = self.run_json()
        self.assertEqual(code, 1)
        self.assertEqual(self.row(data, "Config", "agents.json")["status"], "FAIL")

    def test_load_specs_does_not_print_on_invalid_config(self):
        self.write_agents(None, raw="{broken")
        out = io.StringIO()
        with redirect_stdout(out):
            specs = doctor._load_specs()
        self.assertEqual(out.getvalue(), "")  # would corrupt --json output
        self.assertIn("claude", specs)

    def test_agents_json_wrong_shape_fails(self):
        self.write_agents(None, raw='{"agents": ["claude"]}')
        self.assertEqual(doctor.check_agents_config()[0].status, "FAIL")
        self.write_agents(None, raw='[1, 2]')
        self.assertEqual(doctor.check_agents_config()[0].status, "FAIL")

    def test_custom_agent_with_missing_bin_warns(self):
        self.write_agents({"fakeok": fake_spec("fakeok"),
                           "ghost": {"name": "Ghost", "bin": str(self.tmp / "no-such-cli")}})
        checks = doctor.check_agents_config()
        self.assertEqual(checks[0].status, "PASS")
        ghost = [c for c in checks if c.name == "agent ghost"]
        self.assertEqual(len(ghost), 1)
        self.assertEqual(ghost[0].status, "WARN")
        self.assertIn("no-such-cli", ghost[0].detail)

    def test_invalid_config_json_fails(self):
        paths.settings_path().write_text('{"jobs": 3,,}', encoding="utf-8")
        code, data, _ = self.run_json()
        cfg = self.row(data, "Config", "config.json")
        self.assertEqual(cfg["status"], "FAIL")
        self.assertIn("line 1", cfg["detail"])
        self.assertEqual(code, 1)

    def test_bad_setting_values_warn_per_key(self):
        paths.settings_path().write_text(json.dumps({
            "jobs": "three", "review": "false", "bogus": 1, "lead": "nobody",
            "models": "opus", "workers": ["fakeok", "ghost"], "rounds": 3, "task_timeout": 5,
            "effort": {"fakeok": "high"},
        }), encoding="utf-8")
        checks = doctor.check_settings(doctor._load_specs())
        warned = sorted(c.name for c in checks if c.status == "WARN")
        self.assertEqual(warned, ["setting bogus", "setting jobs", "setting lead", "setting models",
                                  "setting review", "setting workers"])
        review = [c for c in checks if c.name == "setting review"][0]
        self.assertIn("omnicli config set review false", review.hint)  # coerce() accepts it -> exact command
        self.assertNotIn("PASS", [c.status for c in checks])

    def test_valid_settings_pass(self):
        paths.settings_path().write_text(json.dumps({"jobs": 3, "lead": "fakeok", "review": False,
                                                     "task_timeout": 12.5, "lead_timeout": 10}), encoding="utf-8")
        checks = doctor.check_settings(doctor._load_specs())
        self.assertEqual([c.status for c in checks], ["PASS"])
        self.assertIn("5 settings", checks[0].detail)


class TestAgents(DoctorBase):
    def test_no_agent_installed_fails(self):
        self.write_agents({"ghost": {"bin": str(self.tmp / "no-such-cli")}})
        code, data, _ = self.run_json()
        self.assertEqual(self.row(data, "AI agents", "ghost")["status"], "INFO")
        self.assertEqual(self.row(data, "AI agents", "ready agents")["status"], "FAIL")
        self.assertEqual(code, 1)

    def test_failed_agent_warns_with_reason_and_login_hint(self):
        self.write_agents({"fakeok": fake_spec("fakeok"), "fakebad": fake_spec("fakebad")})
        cache = self.ok_health("fakeok")
        cache["fakebad"] = {"ok": False, "reason": "not logged in / auth expired", "checked_at": time.time() - 300}
        self.write_health(cache)
        code, data, _ = self.run_json()
        bad = self.row(data, "AI agents", "fakebad")
        self.assertEqual(bad["status"], "WARN")
        self.assertIn("not logged in", bad["detail"])
        self.assertIn("5m ago", bad["detail"])
        self.assertIn("log in", bad["fix_hint"])
        self.assertTrue(bad["fixable"])
        self.assertEqual(self.row(data, "AI agents", "ready agents")["status"], "PASS")
        self.assertEqual(code, 0)

    def test_login_hint_names_the_cli_command(self):
        hint = doctor._agent_hint("codex", {"bin": "codex"}, "not logged in / auth expired")
        self.assertIn("codex login", hint)
        self.assertIn("omnicli doctor --check", hint)
        self.assertIn("slow", doctor._agent_hint("x", {"bin": "x"}, "timed out after 120s"))
        self.assertIn("yourself", doctor._agent_hint("x", {"bin": "x"}, "exit code 3: panic"))

    def test_all_installed_agents_failed_is_fail(self):
        self.write_health({"fakeok": {"ok": False, "reason": "exit code 3: panic", "checked_at": time.time()}})
        code, data, _ = self.run_json()
        self.assertEqual(self.row(data, "AI agents", "fakeok")["status"], "WARN")
        self.assertEqual(self.row(data, "AI agents", "ready agents")["status"], "FAIL")
        self.assertEqual(code, 1)

    def test_never_checked_agent_is_warn_not_fail(self):
        code, data, _ = self.run_json()
        self.assertEqual(self.row(data, "AI agents", "fakeok")["status"], "INFO")
        ready = self.row(data, "AI agents", "ready agents")
        self.assertEqual(ready["status"], "WARN")
        self.assertIn("--check", ready["fix_hint"])
        self.assertEqual(code, 0)

    def test_old_cached_success_still_passes_but_says_so(self):
        self.write_health(self.ok_health("fakeok", age=ag.HEALTH_TTL_SEC + 3600))
        _, data, _ = self.run_json()
        row = self.row(data, "AI agents", "fakeok")
        self.assertEqual(row["status"], "PASS")
        self.assertIn("re-checked on the next run", row["detail"])

    def test_check_flag_forces_a_fresh_health_check(self):
        fresh = {"fakeok": {"installed": True, "ok": False, "reason": "not logged in / auth expired",
                            "checked_at": time.time()}}
        self.write_health(self.ok_health("fakeok"))  # cache says ok; the fresh result must win
        with mock.patch.object(doctor.ag, "check_health", return_value=fresh) as ch:
            code, data, err = self.run_json("--check")
        ch.assert_called_once()
        self.assertTrue(ch.call_args.kwargs.get("force"))
        self.assertIn("one tiny prompt", err)  # notice goes to stderr so stdout stays valid JSON
        row = self.row(data, "AI agents", "fakeok")
        self.assertEqual(row["status"], "WARN")
        self.assertIn("checked just now", row["detail"])
        self.assertTrue(data["summary"]["fresh_check"])
        self.assertEqual(code, 1)  # the only agent is not ready

    def test_check_notice_in_text_mode(self):
        fresh = {"fakeok": {"installed": True, "ok": True, "reason": "ok (1s)", "checked_at": time.time()}}
        with mock.patch.object(doctor.ag, "check_health", return_value=fresh) as ch:
            code, out, _ = self.run_doctor("--check")
        self.assertTrue(ch.call_args.kwargs.get("force"))
        self.assertIn("one tiny prompt", out)
        self.assertEqual(code, 0)

    def test_without_check_no_ping(self):
        with mock.patch.object(doctor.ag, "check_health") as ch:
            self.run_json()
            self.run_doctor()
        ch.assert_not_called()

    def test_fix_clears_failed_health_cache_entries(self):
        self.write_agents({"fakeok": fake_spec("fakeok"), "fakebad": fake_spec("fakebad")})
        cache = self.ok_health("fakeok")
        cache["fakebad"] = {"ok": False, "reason": "not logged in", "checked_at": time.time()}
        cache["retired"] = {"ok": False, "reason": "exit code 1", "checked_at": time.time()}
        self.write_health(cache)
        code, data, _ = self.run_json("--fix")
        self.assertEqual(set(json.loads(paths.health_cache_path().read_text())), {"fakeok"})
        self.assertEqual(self.row(data, "AI agents", "fakebad")["status"], "WARN")  # still not logged in
        self.assertEqual(code, 0)

    def test_version_probe_times_out_and_kills(self):
        start = time.time()
        code, out, err = REAL_CAPTURE([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)
        self.assertIsNone(code)
        self.assertIn("timed out", err)
        self.assertLess(time.time() - start, 10)


class TestRuns(DoctorBase):
    def setUp(self):
        super().setUp()
        self.write_health(self.ok_health("fakeok"))

    def test_no_runs_pass(self):
        _, data, _ = self.run_json()
        self.assertEqual([c["status"] for c in self.rows(data, "Runs")], ["PASS"])

    def test_stale_registry_entry_warns_and_fix_forgets_it(self):
        d = self.make_project("gone-app", phase="complete")
        shutil.rmtree(d)
        code, data, _ = self.run_json()
        row = self.row(data, "Runs", "gone-app")
        self.assertEqual((row["status"], row["fixable"]), ("WARN", True))
        self.assertEqual(len(runs.list_runs(include_missing=True)), 1)
        code, data, _ = self.run_json("--fix")
        self.assertEqual(self.row(data, "Runs", "gone-app")["status"], "FIXED")
        self.assertEqual(runs.list_runs(include_missing=True), [])
        self.assertEqual(code, 0)

    def test_fix_forgets_non_canonical_registry_key(self):
        key = str(self.tmp / "sub" / ".." / "gone2")
        paths.runs_registry_path().write_text(json.dumps({key: {"dir": key, "phase": "complete"}}))
        code, data, _ = self.run_json("--fix")
        self.assertEqual(json.loads(paths.runs_registry_path().read_text()), {})
        self.assertEqual(data["summary"]["fixed"], 1)

    def test_interrupted_run_warns_and_fix_marks_it_stopped(self):
        d = self.make_project("demo", phase="running", pid=self.dead_pid())
        code, data, _ = self.run_json()
        row = self.row(data, "Runs", "demo")
        self.assertEqual((row["status"], row["fixable"]), ("WARN", True))
        self.assertIn("interrupted", row["detail"])
        self.assertIn("omnicli resume", row["fix_hint"])

        code, data, _ = self.run_json("--fix")
        self.assertEqual(self.row(data, "Runs", "demo")["status"], "FIXED")
        state = json.loads((d / ".omnicli" / "team" / "state.json").read_text())
        self.assertEqual((state["phase"], state["pid"]), ("stopped", 0))
        self.assertEqual(state["goal"], "build demo")  # everything else preserved
        self.assertEqual(state["tasks"], [{"id": "t1", "status": "running"}])
        rec = runs.find_run(str(d))
        self.assertEqual((rec["phase"], rec["pid"], rec["goal"]), ("stopped", 0, "build demo"))
        self.assertEqual(len(runs.list_runs(include_missing=True)), 1)
        self.assertEqual(code, 0)
        _, data, _ = self.run_json()  # and it stays fixed
        self.assertEqual([c["status"] for c in self.rows(data, "Runs")], ["PASS"])

    def test_live_run_is_left_alone(self):
        self.make_project("live", phase="running", pid=os.getpid())
        _, data, _ = self.run_json("--fix")
        row = self.row(data, "Runs", "live")
        self.assertEqual((row["status"], row["fixable"]), ("INFO", False))
        self.assertEqual(runs.load_state(str(self.tmp / "live"))["phase"], "running")

    def test_unreadable_state_is_reported_and_never_rewritten(self):
        d = self.make_project("broken", phase="stopped")
        sp = d / ".omnicli" / "team" / "state.json"
        sp.write_text("{not json")
        code, data, _ = self.run_json("--fix")
        row = self.row(data, "Runs", "broken")
        self.assertEqual((row["status"], row["fixable"]), ("WARN", False))
        self.assertEqual(sp.read_text(), "{not json")

    def test_corrupt_registry_warns(self):
        paths.runs_registry_path().write_text("[oops")
        _, data, _ = self.run_json()
        self.assertEqual(self.row(data, "Runs", "run registry")["status"], "WARN")


class TestSafety(DoctorBase):
    def _snapshot(self):
        return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in self.tmp.rglob("*") if p.is_file()}

    def test_doctor_without_fix_writes_nothing(self):
        cache = self.ok_health("fakeok")
        cache["fakeok"]["ok"] = False
        self.write_health(cache)
        paths.settings_path().write_text('{"jobs": "x"}')
        self.make_project("demo", phase="running", pid=self.dead_pid())
        gone = self.make_project("gone", phase="complete")
        shutil.rmtree(gone)
        before = self._snapshot()
        self.run_json()
        self.run_doctor()
        self.assertEqual(self._snapshot(), before)

    def test_fix_never_touches_user_config_files(self):
        self.write_agents(None, raw='{"agents": {"x": }')
        paths.settings_path().write_text('{"jobs": "three", "bogus": true}')
        agents_before = paths.agents_config_path().read_bytes()
        settings_before = paths.settings_path().read_bytes()
        with mock.patch.object(doctor, "_load_specs", return_value={"fakeok": dict(fake_spec("fakeok"), id="fakeok")}):
            code, _, _ = self.run_json("--fix")
        self.assertEqual(paths.agents_config_path().read_bytes(), agents_before)
        self.assertEqual(paths.settings_path().read_bytes(), settings_before)
        self.assertEqual(code, 1)

    def test_fix_output_lists_each_fix(self):
        self.write_health(self.ok_health("fakeok"))
        self.make_project("demo", phase="running", pid=self.dead_pid())
        code, out, _ = self.run_doctor("--fix")
        self.assertIn("✓ marked it stopped", out)
        self.assertIn("1 fixed", out)
        self.assertIn("--fix applied 1 of 1", out)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
