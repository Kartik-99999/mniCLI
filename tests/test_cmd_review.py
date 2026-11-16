"""
Tests for `omnicli review`: a read-only AI reviewer inspects any project folder; --fix turns its
follow-up tasks into a team run. Real model CLIs are replaced by a tiny fake reviewer script
(written to a temp dir) and, for --fix, by tests/fake_agent.py in "good" mode.
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
import sys  # noqa: E402
import textwrap  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402
from unittest import mock  # noqa: E402

from omnicli import ledger, settings, ui  # noqa: E402
from omnicli.commands import review  # noqa: E402
from omnicli.conductor import Conductor  # noqa: E402

FAKE_AGENT = str(Path(__file__).with_name("fake_agent.py"))

FAKE_REVIEWER = textwrap.dedent('''
    """Fake reviewer: python3 fake_reviewer.py <mode> <prompt_file>"""
    import json, re, sys
    from pathlib import Path

    mode, prompt = sys.argv[1], Path(sys.argv[2]).read_text(encoding="utf-8")
    if not prompt.startswith("[OmniCLI team - project review]\\n"):
        print("unknown prompt")
        sys.exit(0)
    if mode == "crash":
        sys.stderr.write("panic: reviewer exploded\\n")
        sys.exit(3)
    if mode == "authfail":
        sys.stderr.write("ERROR: unauthorized (401)\\n")
        sys.exit(1)
    retry = "reply with ONLY the JSON" in prompt
    if mode == "garbled" or (mode == "garbled_once" and not retry):
        print("Overall the project looks fine, nothing to report in JSON form.")
        sys.exit(0)
    section = prompt.split("WORKERS who will fix")[1].split("RULES:")[0]
    workers = re.findall(r"^- (\\w+): ", section, flags=re.M)
    if mode == "clean":
        review = {"summary": "Nothing wrong.", "score": 10, "findings": [], "followup_tasks": []}
    else:
        review = {
            "summary": "Works, but crashes on empty input and has no tests.",
            "score": "6",
            "findings": [
                {"severity": "low", "title": "Inconsistent naming", "detail": "mixes camelCase and snake_case",
                 "files": ["app.py"], "fix": "Use snake_case everywhere"},
                {"severity": "critical", "title": "Crash on empty input", "detail": "IndexError when the list is empty",
                 "files": "app.py", "fix": "Guard the empty list in main()"},
                {"severity": "medium", "title": "No tests", "detail": "nothing is tested",
                 "files": [], "fix": "Add unittest tests for main()"},
            ],
            "followup_tasks": [
                {"id": "fix1", "title": "Fix crash", "assignee": workers[0], "instructions": "guard the empty list",
                 "owns": ["fix1.txt"], "verify": "test -f fix1.txt"},
                {"id": "fix2", "title": "Add tests", "assignee": workers[-1], "instructions": "add tests",
                 "owns": ["fix2.txt"], "verify": ""},
            ],
        }
    print("Here is my review:\\n```json\\n" + json.dumps(review) + "\\n```")
''')


def parse(argv):
    p = argparse.ArgumentParser(prog="omnicli")
    sub = p.add_subparsers(dest="subcommand")
    review.add_parser(sub)
    return p.parse_args(["review"] + list(argv))


class ReviewBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scripts = Path(tempfile.mkdtemp(prefix="omnicli-review-fake-"))
        cls.reviewer_script = cls.scripts / "fake_reviewer.py"
        cls.reviewer_script.write_text(FAKE_REVIEWER, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scripts, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="omnicli-review-test-"))
        self.home = self.tmp / "home"
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        (self.proj / "app.py").write_text("def main(xs):\n    return xs[0]\n", encoding="utf-8")
        (self.proj / "README.md").write_text("# Demo app\nRun with python3 app.py\n", encoding="utf-8")
        (self.proj / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
        env = mock.patch.dict(os.environ, {"OMNICLI_HOME": str(self.home), "OMNICLI_FAKE_STATE": str(self.tmp / "fake-state")})
        env.start()
        self.addCleanup(env.stop)
        self.patch(ui, "USE_COLOR", False)
        self.health_calls = 0
        self.conductors = []
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def patch(self, target, attr, value):
        p = mock.patch.object(target, attr, value)
        p.start()
        self.addCleanup(p.stop)

    # ------------------------------------------------------------ fakes
    def reviewer_spec(self, name, mode):
        return {"id": name, "name": f"Fake reviewer {name}", "bin": sys.executable, "strengths": f"{name} strengths",
                "readonly": [str(self.reviewer_script), mode, "{prompt_file}"],
                "edit": [str(self.reviewer_script), mode, "{prompt_file}"]}

    def use_agents(self, modes, ready=None, reasons=None):
        """modes: {name: fake reviewer mode}; ready: names that pass the health check (default: all)."""
        specs = {n: self.reviewer_spec(n, m) for n, m in modes.items()}
        ready = set(modes) if ready is None else set(ready)
        reasons = reasons or {}
        health = {n: {"installed": True, "ok": n in ready, "reason": reasons.get(n, "ok" if n in ready else "logged out")}
                  for n in modes}

        def fake_health(s, names=None, **kw):
            self.health_calls += 1
            return {n: health[n] for n in (names or s)}

        self.patch(review, "_load_specs", lambda: specs)
        self.patch(review, "_check_health", fake_health)
        return specs

    def use_fake_conductor(self, names):
        specs = {n: {"id": n, "name": f"Fake {n}", "bin": sys.executable, "strengths": "testing",
                     "readonly": [FAKE_AGENT, "good", "{prompt_file}"], "edit": [FAKE_AGENT, "good", "{prompt_file}"]}
                 for n in names}
        health = {n: {"installed": True, "ok": True, "reason": "ok"} for n in names}

        def factory(cfg):
            self.conductors.append(cfg)
            return Conductor(cfg, specs=specs, health=health, verbose=False)

        self.patch(review, "_make_conductor", factory)

    def run_cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = review.handler(parse(list(argv)))
        return rc, out.getvalue(), err.getvalue()

    def review_dir(self):
        return self.proj / ".omnicli" / "review"

    def logs(self):
        return sorted(p.name for p in self.review_dir().glob("review-*.md") if p.name.count(".") == 1)

    def prompt_text(self):
        prompts = sorted(self.review_dir().glob("review-*.prompt.md"))
        self.assertTrue(prompts, "no prompt was written")
        return prompts[0].read_text(encoding="utf-8")

    def write_state(self, **fields):
        d = self.proj / ".omnicli" / "team"
        d.mkdir(parents=True, exist_ok=True)
        state = {"version": 1, "goal": "build a widget", "phase": "complete", "tasks": []}
        state.update(fields)
        (d / "state.json").write_text(json.dumps(state), encoding="utf-8")


class TestGuards(ReviewBase):
    def test_home_and_root_are_refused(self):
        self.use_agents({"alpha": "good"})
        for d in (str(Path.home()), "/"):
            rc, out, err = self.run_cmd("-d", d)
            self.assertEqual(rc, 1)
            self.assertIn("Refusing to review", err)
        self.assertEqual(self.health_calls, 0)

    def test_missing_dir(self):
        self.use_agents({"alpha": "good"})
        rc, _, err = self.run_cmd("-d", str(self.tmp / "nope"))
        self.assertEqual(rc, 1)
        self.assertIn("No such project directory", err)

    def test_no_ready_agent(self):
        self.use_agents({"alpha": "good"}, ready=[])
        rc, _, err = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 1)
        self.assertIn("no AI CLI is ready", err)
        self.assertIn("omnicli agents --check", err)

    def test_json_and_fix_together_refused(self):
        self.use_agents({"alpha": "good"})
        rc, _, err = self.run_cmd("-d", str(self.proj), "--json", "--fix")
        self.assertEqual(rc, 1)
        self.assertIn("cannot be combined", err)

    def test_registered_in_command_list(self):
        from omnicli.commands import COMMAND_MODULES
        self.assertIn("review", COMMAND_MODULES)
        self.assertEqual(review.NAMES, ["review"])
        args = parse(["security", "tests", "-d", "x", "--lead", "codex", "--fix", "-y", "--max-tasks", "3",
                      "--timeout", "2.5", "--image", "shot.png", "-v"])
        self.assertEqual((args.focus, args.dir, args.lead, args.fix, args.yes, args.max_tasks, args.timeout, args.verbose),
                         (["security", "tests"], "x", "codex", True, True, 3, 2.5, True))
        self.assertIs(args.func, review.handler)

    def test_visual_prompt_names_attached_screenshot(self):
        prompt = review.build_review_prompt(self.proj, "visual layout", {}, [], [], [], 2,
                                            ["/tmp/homepage.png"])
        self.assertIn("VISUAL REFERENCES", prompt)
        self.assertIn("/tmp/homepage.png", prompt)
        self.assertIn("responsive behavior", prompt)


class TestReviewerSelection(ReviewBase):
    def test_first_ready_in_preferred_order(self):
        # codex outranks agy in PREFERRED_LEAD_ORDER but is logged out; zed is unknown to the order
        self.use_agents({"zed": "good", "agy": "good", "codex": "good"}, ready=["zed", "agy"])
        rc, out, _ = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads((self.review_dir() / "latest.json").read_text())["reviewer"], "agy")

    def test_lead_flag_wins(self):
        self.use_agents({"claude": "good", "zed": "good"})
        rc, _, _ = self.run_cmd("-d", str(self.proj), "--lead", "zed")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads((self.review_dir() / "latest.json").read_text())["reviewer"], "zed")

    def test_lead_flag_not_ready_or_unknown_is_an_error(self):
        self.use_agents({"claude": "good", "codex": "good"}, ready=["claude"], reasons={"codex": "not logged in"})
        rc, _, err = self.run_cmd("-d", str(self.proj), "--lead", "codex")
        self.assertEqual(rc, 1)
        self.assertIn("not logged in", err)
        rc, _, err = self.run_cmd("-d", str(self.proj), "--lead", "ghost")
        self.assertEqual(rc, 1)
        self.assertIn("unknown agent 'ghost'", err)
        self.assertFalse(self.review_dir().exists())

    def test_lead_setting_used_and_falls_back(self):
        self.use_agents({"claude": "good", "zed": "good", "codex": "good"}, ready=["claude", "zed"])
        settings.set_value("lead", "zed")
        rc, _, _ = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads((self.review_dir() / "latest.json").read_text())["reviewer"], "zed")

        settings.set_value("lead", "codex")  # saved lead is logged out: warn and use the best ready one
        rc, out, _ = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 0)
        self.assertIn("'codex' is not usable", out)
        self.assertEqual(json.loads((self.review_dir() / "latest.json").read_text())["reviewer"], "claude")

    def test_pick_reviewer_unit(self):
        specs = {"claude": {}, "agy": {}}
        health = {"claude": {"ok": False, "reason": "x"}, "agy": {"ok": True}}
        self.assertEqual(review.pick_reviewer(specs, health, ["claude", "agy"], None, False), ("agy", ""))
        self.assertEqual(review.pick_reviewer(specs, health, ["claude", "agy"], "claude", False)[0], "agy")
        self.assertIsNone(review.pick_reviewer(specs, health, ["claude", "agy"], "claude", True)[0])


class TestReviewRun(ReviewBase):
    def test_prompt_contents(self):
        self.use_agents({"alpha": "good", "beta": "good"})
        self.write_state(goal="build a widget", phase="complete", plan_summary="one module",
                         tasks=[{"id": "t1", "title": "Widget core", "status": "done", "agent_used": "beta",
                                 "summary": "- built widget.py\n- added tests"}])
        rc, _, _ = self.run_cmd("security", "input validation", "-d", str(self.proj), "--max-tasks", "4")
        self.assertEqual(rc, 0)
        p = self.prompt_text()
        self.assertEqual(p.splitlines()[0], "[OmniCLI team - project review]")
        self.assertIn("READ-ONLY", p)
        self.assertIn("security input validation", p)          # focus
        self.assertIn("app.py", p)                              # tree
        self.assertIn("# Demo app", p)                          # README excerpt
        self.assertIn("--- pyproject.toml", p)                  # manifest excerpt
        self.assertIn("- alpha: Fake reviewer alpha - strengths: alpha strengths", p)
        self.assertIn("- beta: ", p)
        self.assertIn("Team goal: build a widget", p)           # team state context
        self.assertIn("t1 [done] (beta) Widget core: built widget.py", p)
        self.assertIn("at most 4 tasks", p)
        self.assertIn('"followup_tasks"', p)
        self.assertIn('"assignee": "<worker id>"', p)
        self.assertIn("Tools installed on this machine:", p)
        self.assertNotIn(".omnicli", p.split("Contents:")[1].split("REVIEW FOCUS")[0])

    def test_general_review_without_focus_and_ledger_kind(self):
        self.use_agents({"alpha": "good"})
        rc, _, _ = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 0)
        self.assertIn("do a general review", self.prompt_text())
        e = ledger.read()[-1]
        self.assertEqual((e["agent"], e["kind"], e["mode"], e["run_dir"]),
                         ("alpha", "project-review", "readonly", str(self.proj.resolve())))

    def test_saved_json_is_sorted_and_normalized(self):
        self.use_agents({"alpha": "good"})
        rc, out, _ = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 0)
        data = json.loads((self.review_dir() / "latest.json").read_text())
        self.assertEqual([f["severity"] for f in data["findings"]], ["high", "medium", "low"])
        self.assertEqual(data["findings"][0]["title"], "Crash on empty input")  # "critical" -> high
        self.assertEqual(data["findings"][0]["files"], ["app.py"])             # str -> [str]
        self.assertEqual(data["score"], 6)
        self.assertEqual(len(data["followup_tasks"]), 2)
        self.assertEqual(data["dir"], str(self.proj.resolve()))
        self.assertTrue(Path(data["log"]).exists())

    def test_human_output(self):
        self.use_agents({"alpha": "good"})
        rc, out, _ = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 0)
        self.assertIn("6/10", out)
        self.assertIn("Findings (3: 1 high, 1 medium, 1 low)", out)
        self.assertLess(out.index("Crash on empty input"), out.index("No tests"))
        self.assertLess(out.index("No tests"), out.index("Inconsistent naming"))
        self.assertIn("Fix: Guard the empty list in main()", out)
        self.assertNotIn("IndexError when the list is empty", out)  # detail only with --verbose
        self.assertIn("omnicli review --fix", out)
        rc, out, _ = self.run_cmd("-d", str(self.proj), "--verbose")
        self.assertIn("Detail: IndexError when the list is empty", out)

    def test_clean_review(self):
        self.use_agents({"alpha": "clean"})
        rc, out, _ = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 0)
        self.assertIn("No findings", out)
        self.assertIn("10/10", out)

    def test_json_output(self):
        self.use_agents({"alpha": "good"})
        rc, out, err = self.run_cmd("-d", str(self.proj), "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)  # stdout must be pure JSON; progress went to stderr
        self.assertEqual(data, json.loads((self.review_dir() / "latest.json").read_text()))
        self.assertEqual(data["findings"][0]["severity"], "high")
        self.assertIn("is reviewing", err)

    def test_parse_retry(self):
        self.use_agents({"alpha": "garbled_once"})
        rc, out, _ = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.logs()), 2)
        retry_prompt = next(self.review_dir().glob("review-*-retry.prompt.md")).read_text()
        self.assertIn("reply with ONLY the JSON", retry_prompt)
        self.assertTrue(retry_prompt.startswith("[OmniCLI team - project review]\n"))
        self.assertIn("not valid review JSON", out)

    def test_unparseable_twice_fails_with_log_path(self):
        self.use_agents({"alpha": "garbled"})
        rc, _, err = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 1)
        self.assertIn("did not reply with review JSON", err)
        self.assertIn(str(self.review_dir()), err)
        self.assertFalse((self.review_dir() / "latest.json").exists())

    def test_crashing_reviewer_fails(self):
        self.use_agents({"alpha": "crash"})
        rc, _, err = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 1)
        self.assertIn("exit code 3", err)
        self.assertIn(".stderr.log", err)
        self.assertEqual(len(self.logs()), 1)  # a crash is not retried

    def test_logged_out_reviewer_hint(self):
        self.use_agents({"alpha": "authfail"})
        rc, _, err = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 1)
        self.assertIn("not logged in", err)
        self.assertIn("--lead", err)

    def test_normalize_review_unit(self):
        r = review.normalize_review({"score": 42, "findings": [{"severity": "weird", "detail": "d\nmore"}, "junk"],
                                     "followup_tasks": "nope"})
        self.assertEqual(r["score"], 10)
        self.assertEqual(r["findings"][0]["severity"], "medium")
        self.assertEqual(r["findings"][0]["title"], "d")
        self.assertEqual(r["followup_tasks"], [])
        self.assertIsNone(review.normalize_review({"score": "n/a"})["score"])
        self.assertEqual(review.normalize_review({"score": 7.25})["score"], 7.2)


class TestFix(ReviewBase):
    def test_fix_happy_path(self):
        self.use_agents({"alpha": "good", "beta": "good"})
        self.use_fake_conductor(["alpha", "beta"])
        rc, out, err = self.run_cmd("-d", str(self.proj), "--fix", "-y")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.conductors), 1)
        cfg = self.conductors[0]
        self.assertEqual((cfg.goal, cfg.resume, cfg.auto_approve, cfg.lead), ("", True, True, "alpha"))
        state = json.loads((self.proj / ".omnicli" / "team" / "state.json").read_text())
        self.assertEqual(state["phase"], "complete")
        self.assertTrue(state["goal"].startswith("Fix review findings: Works, but crashes"))
        self.assertEqual(state["lead"], "alpha")
        self.assertEqual([t["id"] for t in state["tasks"]], ["fix1", "fix2"])
        self.assertTrue(all(t["status"] == "done" for t in state["tasks"]))
        self.assertEqual({t["assignee"] for t in state["tasks"]}, {"alpha", "beta"})
        for f in ("fix1.txt", "fix2.txt"):
            self.assertTrue((self.proj / f).exists(), f)
        self.assertIn("Fix plan", out)

    def test_fix_writes_planned_state_and_respects_decline(self):
        self.use_agents({"alpha": "good"})
        self.use_fake_conductor(["alpha"])
        self.write_state(phase="complete", goal="old finished run")
        with mock.patch("builtins.input", return_value="n"):
            rc, out, _ = self.run_cmd("-d", str(self.proj), "--fix", "--max-tasks", "1")
        self.assertEqual(rc, 0)
        self.assertEqual(self.conductors, [])
        team = self.proj / ".omnicli" / "team"
        state = json.loads((team / "state.json").read_text())
        self.assertEqual((state["phase"], state["version"], state["pid"], state["round"]), ("planned", 1, 0, 1))
        self.assertEqual(len(state["tasks"]), 1)  # --max-tasks
        self.assertEqual(state["workers"], ["alpha"])
        for key in ("project_name", "plan_summary", "final", "started_at", "updated_at"):
            self.assertIn(key, state)
        self.assertEqual(state["tasks"][0]["status"], "pending")
        backups = list(team.glob("state.before-review-fix-*.json"))
        self.assertEqual(len(backups), 1)  # the finished run is kept, not overwritten
        self.assertEqual(json.loads(backups[0].read_text())["goal"], "old finished run")
        self.assertIn("omnicli resume", out)
        from omnicli import runs
        self.assertEqual(runs.find_run(str(self.proj))["phase"], "planned")

    def test_fix_refused_when_unfinished_run_exists(self):
        self.use_agents({"alpha": "good"})
        self.use_fake_conductor(["alpha"])
        for phase in ("incomplete", "running", "planned"):
            self.write_state(phase=phase)
            rc, _, err = self.run_cmd("-d", str(self.proj), "--fix", "-y")
            self.assertEqual(rc, 1)
            self.assertIn(f"unfinished team run (phase: {phase})", err)
            self.assertIn("omnicli resume", err)
        self.assertEqual(self.health_calls, 0)       # refused before spending an AI call
        self.assertFalse(self.review_dir().exists())
        self.assertEqual(self.conductors, [])

    def test_plain_review_ignores_unfinished_run(self):
        self.use_agents({"alpha": "good"})
        self.write_state(phase="incomplete")
        rc, _, _ = self.run_cmd("-d", str(self.proj))
        self.assertEqual(rc, 0)

    def test_fix_config_uses_saved_settings(self):
        settings.set_value("jobs", "3")
        settings.set_value("git", "false")
        settings.set_value("models.alpha", "big-model")
        cfg = review._fix_config(self.proj, "alpha", ["alpha", "beta"], 4)
        self.assertEqual((cfg.goal, cfg.resume, cfg.auto_approve, cfg.jobs, cfg.use_git, cfg.max_tasks, cfg.workers),
                         ("", True, True, 3, False, 4, ["alpha", "beta"]))
        if hasattr(cfg, "models"):
            self.assertEqual(cfg.models, {"alpha": "big-model"})

    def test_saved_model_reaches_reviewer_spec_only_when_supported(self):
        settings.set_value("models.alpha", "big-model")
        settings.set_value("effort.alpha", "turbo")
        spec = {"id": "alpha", "model_args": ["--model", "{model}"], "effort_args": ["--effort", "{effort}"],
                "effort_levels": ["low", "high"]}
        out = review._with_saved_options(spec, "alpha")
        self.assertEqual(out.get("_model"), "big-model")
        self.assertNotIn("_effort", out)          # not an accepted level
        self.assertNotIn("_model", spec)          # caller's spec untouched
        self.assertNotIn("_model", review._with_saved_options({"id": "alpha"}, "alpha"))  # no verified flag

    def test_fix_with_nothing_to_fix(self):
        self.use_agents({"alpha": "clean"})
        self.use_fake_conductor(["alpha"])
        rc, out, _ = self.run_cmd("-d", str(self.proj), "--fix", "-y")
        self.assertEqual(rc, 0)
        self.assertIn("nothing for the team to fix", out)
        self.assertEqual(self.conductors, [])
        self.assertFalse((self.proj / ".omnicli" / "team" / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
