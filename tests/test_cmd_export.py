"""
Tests for `omnicli export` (omnicli/commands/export.py): Markdown / JSON / self-contained HTML
exports of a team run, built from a fixture state.json, ledger entries, a real temp git repo
and fake attempt logs. No AI CLI is ever called.
"""

import os
import tempfile

# Never let tests touch the real ~/.omnicli (ledger, run registry, ...).
os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

import argparse  # noqa: E402
import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
from pathlib import Path  # noqa: E402

from omnicli import ledger, paths, runs  # noqa: E402
from omnicli.commands import export as ex  # noqa: E402

XSS = '<script>alert("pwned")</script>'
XSS_ESCAPED = "&lt;script&gt;alert(&quot;pwned&quot;)&lt;/script&gt;"
EXTERNAL_IMG = '<img src="https://evil.example/x.png">'
HAS_GIT = shutil.which("git") is not None


def fixture_state(goal: str, now: float, phase: str = "complete", complete: bool = True, pid: int = 0):
    return {
        "version": 1,
        "goal": goal,
        "project_name": "todo-app",
        "plan_summary": "Split the work into an API | a UI.\nThen write docs.",
        "lead": "claude",
        "workers": ["codex", "agy"],
        "round": 2,
        "final": {
            "complete": complete,
            "summary": "Built a small todo app " + XSS,
            "how_to_run": "pip install -r requirements.txt\npython app.py",
            "remaining_issues": ["No auth | yet", "Tests are thin"],
            "followup_tasks": [{"title": "Add login"}],
        },
        "started_at": now - 725,
        "updated_at": now - 2,
        "phase": phase,
        "pid": pid,
        "tasks": [
            {"id": "t1", "title": "Build API | backend", "assignee": "codex",
             "instructions": "Create app.py with a Flask API.", "depends_on": [], "owns": ["app.py"],
             "acceptance": "GET /todos returns JSON", "verify": "python -m pytest -q", "round": 1,
             "status": "done", "agent_used": "codex", "summary": "API done\n" + EXTERNAL_IMG, "review_note": "",
             "attempts": [
                 {"agent": "codex", "revision": 0, "exit_code": 0, "duration_sec": 61.2, "at": "2026-11-14T10:01:00",
                  "result": "revise", "feedback": "Add input validation for | titles", "files_changed": 2,
                  "verify_exit": 1},
                 {"agent": "codex", "revision": 1, "exit_code": 0, "duration_sec": 30.0, "at": "2026-11-14T10:03:00",
                  "result": "approved", "feedback": "", "files_changed": 1, "verify_exit": 0},
             ],
             "commit": "abc1234", "duration_sec": 95.0},
            {"id": "t2", "title": "Build UI " + XSS, "assignee": "agy", "instructions": "Make index.html.",
             "depends_on": ["t1"], "owns": ["static/"], "acceptance": "", "verify": "", "round": 1,
             "status": "failed", "agent_used": "codex", "summary": "",
             "review_note": "verify still failing after 2 attempts: tests fail",
             "attempts": [
                 {"agent": "agy", "revision": 0, "exit_code": 3, "duration_sec": 5.0, "at": "2026-11-14T10:04:00",
                  "result": "agent error: exit code 3: panic: something exploded"},
                 {"agent": "codex", "revision": 0, "exit_code": 0, "duration_sec": 40.0, "at": "2026-11-14T10:05:00",
                  "result": "revise", "feedback": "UI never loads", "files_changed": 0, "verify_exit": None},
             ],
             "commit": "", "duration_sec": 45.0},
            {"id": "t3", "title": "Write docs", "assignee": "agy", "instructions": "README", "depends_on": ["t2"],
             "owns": [], "acceptance": "", "verify": "", "round": 2, "status": "skipped", "agent_used": "",
             "summary": "", "review_note": "", "attempts": [], "commit": "", "duration_sec": 0.0},
        ],
    }


class ExportBase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="omni-export-home-"))
        self._old_home = os.environ.get("OMNICLI_HOME")
        os.environ["OMNICLI_HOME"] = str(self.home)
        self.tmp = Path(tempfile.mkdtemp(prefix="omni-export-")).resolve()
        # git must never treat a parent directory as the repo of our fixture projects
        self._old_ceiling = os.environ.get("GIT_CEILING_DIRECTORIES")
        os.environ["GIT_CEILING_DIRECTORIES"] = str(self.tmp)
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        self.now = time.time()
        self.goal = "Build a todo app " + XSS

    def tearDown(self):
        os.environ["OMNICLI_HOME"] = self._old_home or ""
        if self._old_ceiling is None:
            os.environ.pop("GIT_CEILING_DIRECTORIES", None)
        else:
            os.environ["GIT_CEILING_DIRECTORIES"] = self._old_ceiling
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- fixtures
    def write_run(self, project: Path = None, register: bool = True, **kw) -> Path:
        project = project or self.proj
        state = fixture_state(kw.pop("goal", self.goal), self.now, **kw)
        sd = paths.project_state_dir(project)
        (sd / "logs").mkdir(parents=True, exist_ok=True)
        (sd / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        if register:
            runs.register_run({"dir": str(project), "goal": state["goal"], "project_name": state["project_name"],
                               "phase": state["phase"], "lead": "claude", "workers": state["workers"],
                               "started_at": state["started_at"], "pid": state["pid"]})
        return sd

    def add_ledger(self, project: Path = None):
        run_dir = str((project or self.proj).resolve())
        rec = ledger.record
        rec(ledger.make_entry("claude", "readonly", {"kind": "plan", "run_dir": run_dir}, 12.0, 0, True, False,
                              "p" * 4000, "o" * 800))
        rec(ledger.make_entry("claude", "readonly", {"kind": "review", "run_dir": run_dir}, 8.0, 0, True, False,
                              "p" * 2000, "o" * 400))
        rec(ledger.make_entry("codex", "edit", {"kind": "task", "run_dir": run_dir, "task_id": "t1"}, 61.0, 0, True,
                              False, "p" * 8000, "o" * 12000))
        rec(ledger.make_entry("agy", "edit", {"kind": "task", "run_dir": run_dir, "task_id": "t2"}, 5.0, 3, False,
                              False, "p" * 400, ""))
        # another project's call and an old call from a previous run in this dir: both excluded
        rec(ledger.make_entry("claude", "readonly", {"kind": "plan", "run_dir": str(self.tmp / "other")}, 1.0, 0,
                              True, False, "x" * 999, "y" * 999))
        old = ledger.make_entry("muse", "edit", {"kind": "task", "run_dir": run_dir}, 1.0, 0, True, False, "z", "z")
        old["ts"] = self.now - 50000
        rec(old)

    def make_git(self, project: Path = None):
        project = project or self.proj
        env = dict(os.environ, GIT_AUTHOR_NAME="Test Bot", GIT_AUTHOR_EMAIL="bot@example.invalid",
                   GIT_COMMITTER_NAME="Test Bot", GIT_COMMITTER_EMAIL="bot@example.invalid",
                   GIT_CONFIG_NOSYSTEM="1")

        def git(*a):
            subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *a],
                           cwd=str(project), env=env, check=True, capture_output=True)

        git("init", "-q")
        (project / "README.md").write_text("hello\n")
        git("add", "README.md")
        git("commit", "-q", "-m", "omnicli: baseline before team run")
        (project / "app.py").write_text("print('hi')\n")
        git("add", "app.py")
        git("commit", "-q", "-m", "t1: Build API | backend [codex]\n\nOmniCLI team checkpoint.")

    def run_export(self, *argv, cwd: Path = None):
        parser = argparse.ArgumentParser(prog="omnicli")
        sub = parser.add_subparsers(dest="subcommand")
        ex.add_parser(sub)
        args = parser.parse_args(["export", *[str(a) for a in argv]])
        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        try:
            os.chdir(str(cwd or self.tmp))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = args.func(args)
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()


def table_rows(md: str):
    return [ln for ln in md.splitlines() if ln.startswith("|")]


def cells(row: str):
    # split on pipes that are not escaped with a backslash
    return re.split(r"(?<!\\)\|", row)[1:-1]


class TestMarkdown(ExportBase):
    def test_markdown_has_every_section(self):
        self.write_run()
        self.add_ledger()
        if HAS_GIT:
            self.make_git()
        code, out, err = self.run_export("-d", self.proj)
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("# todo-app - OmniCLI team run"))
        for needle in [
            "> Build a todo app", "COMPLETE (phase: complete) after 2 round(s)", "**Lead:** claude",
            "**Workers:** codex, agy", "1 of 3 done, 1 failed, 1 skipped", "12m0", "## Approach",
            "Split the work into an API | a UI.", "## What was built", "Built a small todo app",
            "## How to run", "```\npip install -r requirements.txt\npython app.py\n```",
            "| ID | Title | Worker | Status | Tries | Time | Commit |", "## Task details", "### t1 - Build API | backend",
            "**Instructions**", "Create app.py with a Flask API.", "**Acceptance**", "GET /todos returns JSON",
            "**Verify:** `python -m pytest -q`", "**codex** (first try) - revise", "Add input validation for | titles",
            "**codex** (revision 1) - approved", "verify exit 1", "agent error: exit code 3", "UI never loads",
            "**Review note:** verify still failing after 2 attempts", "**Depends on:** t1",
            "## Remaining issues", "- No auth | yet", "## Follow-up tasks proposed by the lead", "- Add login",
            "## Usage (estimated tokens)", "estimates", "## Git checkpoints",
        ]:
            self.assertIn(needle, out, needle)
        self.assertNotIn("Output log", out)          # logs only with --include-logs
        if HAS_GIT:
            self.assertIn("omnicli: baseline before team run", out)
            self.assertIn("Test Bot", out)

    def test_table_cells_escape_pipes(self):
        self.write_run()
        self.add_ledger()
        if HAS_GIT:
            self.make_git()
        _, out, _ = self.run_export("-d", self.proj)
        self.assertIn("Build API \\| backend", out)
        rows = table_rows(out)
        self.assertTrue(rows)
        # every row has exactly as many real (unescaped) cells as its table's header
        task_rows = [r for r in rows if r.startswith("| t")]
        self.assertEqual(len(task_rows), 3)
        for r in task_rows:
            self.assertEqual(len(cells(r)), 7, r)
        if HAS_GIT:
            git_row = next(r for r in rows if "t1: Build API" in r)
            self.assertEqual(len(cells(git_row)), 4, git_row)
            self.assertIn("t1: Build API \\| backend [codex]", git_row)

    def test_usage_is_this_run_only_and_labelled_estimated(self):
        self.write_run()
        self.add_ledger()
        data = ex.gather(self.proj)
        u = data["usage"]
        self.assertTrue(u["estimated"])
        self.assertEqual(set(u["by_agent"]), {"claude", "codex", "agy"})   # not muse (old), not other dir
        self.assertEqual(u["totals"]["calls"], 4)
        self.assertEqual(u["totals"]["failed"], 1)
        self.assertEqual(u["by_agent"]["claude"]["calls"], 2)
        self.assertEqual(u["totals"]["est_tokens_in"], (4000 + 2000 + 8000 + 400) // 4)
        self.assertEqual(set(u["by_kind"]), {"plan", "review", "task"})
        md = ex.render_md(data)
        self.assertIn("| codex | 1 | 1 | 0 | 1m01s | ~2,000 | ~3,000 |", md)
        self.assertIn("| **Total** | **4** |", md)
        self.assertIn("Calls by kind: plan 1, review 1, task 2.", md)

    def test_fence_survives_backticks(self):
        sd = self.write_run()
        state = json.loads((sd / "state.json").read_text())
        state["final"]["how_to_run"] = "```bash\nmake run\n```"
        (sd / "state.json").write_text(json.dumps(state))
        _, out, _ = self.run_export("-d", self.proj)
        self.assertIn("````\n```bash\nmake run\n```\n````", out)

    def test_running_run_is_marked_as_snapshot(self):
        self.write_run(phase="running", complete=False, pid=os.getpid())
        data = ex.gather(self.proj)
        self.assertTrue(data["alive"])
        md = ex.render_md(data)
        self.assertIn("INCOMPLETE (phase: running, still running)", md)
        self.assertIn("snapshot", md)
        self.assertIn('class="pill run">Running<', ex.render_html(data))


class TestJson(ExportBase):
    def test_json_round_trips_full_model(self):
        self.write_run()
        self.add_ledger()
        if HAS_GIT:
            self.make_git()
        code, out, err = self.run_export("-d", self.proj, "--format", "json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["goal"], self.goal)
        self.assertEqual(data["dir"], str(self.proj))
        self.assertEqual(data["outcome"], "complete")
        self.assertEqual([t["id"] for t in data["tasks"]], ["t1", "t2", "t3"])
        self.assertEqual(data["tasks"][0]["tries"], 2)
        self.assertEqual(data["tasks"][0]["attempts"][0]["feedback"], "Add input validation for | titles")
        self.assertEqual(data["counts"], {"total": 3, "done": 1, "failed": 1, "skipped": 1, "pending": 0,
                                          "running": 0})
        self.assertTrue(data["usage"]["estimated"])
        self.assertIn("estimates", data["usage"]["note"])
        self.assertEqual(json.loads(json.dumps(data)), data)
        model = ex.gather(self.proj)
        self.assertEqual(json.loads(ex.render_json(model)), json.loads(json.dumps(model, default=str)))
        if HAS_GIT:
            self.assertTrue(data["git"]["repo"])
            self.assertEqual([c["subject"] for c in data["git"]["commits"]],
                             ["t1: Build API | backend [codex]", "omnicli: baseline before team run"])
            self.assertEqual(data["git"]["commits"][0]["task_id"], "t1")
            self.assertEqual(data["git"]["commits"][1]["task_id"], "")
            self.assertEqual(data["git"]["commits"][0]["author"], "Test Bot")


class TestHtml(ExportBase):
    def html(self, *extra):
        code, out, err = self.run_export("-d", self.proj, "--format", "html", "-o", "-", *extra)
        self.assertEqual(code, 0, err)
        return out

    def test_html_escapes_ai_text(self):
        self.write_run()
        self.add_ledger()
        page = self.html()
        self.assertNotIn("<script", page.lower())
        self.assertIn("Build a todo app " + XSS_ESCAPED, page)          # goal
        self.assertIn("Built a small todo app " + XSS_ESCAPED, page)    # final summary
        self.assertIn("Build UI " + XSS_ESCAPED, page)                  # task title
        self.assertNotIn(EXTERNAL_IMG, page)
        self.assertIn("&lt;img src=&quot;https://evil.example/x.png&quot;&gt;", page)

    def test_html_is_self_contained_responsive_and_themed(self):
        self.write_run()
        self.add_ledger()
        if HAS_GIT:
            self.make_git()
        page = self.html()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn('<meta charset="utf-8">', page)
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', page)
        self.assertIn("@media (prefers-color-scheme: dark)", page)
        self.assertIn(".table-wrap{overflow-x:auto", page)
        self.assertNotRegex(page, r"""(?i)\b(src|href|action|poster|srcset)\s*=\s*["']?\s*(https?:)?//""")
        self.assertNotRegex(page, r"(?i)<(script|link|iframe|object|embed|img)\b")
        self.assertNotIn("@import", page)
        self.assertNotIn("url(", page)
        # every table sits inside a horizontal-scroll wrapper
        self.assertEqual(page.count("<table>"), page.count('<div class="table-wrap"><table>'))
        self.assertGreaterEqual(page.count("<table>"), 2)
        # collapsible task details, failed task opened by default, colored status pills
        self.assertEqual(page.count('<details class="task"'), 3)
        self.assertIn('<details class="task" id="task-t2" open>', page)
        self.assertIn('<span class="pill ok">done</span>', page)
        self.assertIn('<span class="pill bad">failed</span>', page)
        self.assertIn('<span class="pill idle">skipped</span>', page)
        self.assertIn('<span class="pill warn">revise</span>', page)
        self.assertIn('<span class="pill bad">agent error</span>', page)
        self.assertIn("(estimated tokens)", page)

    def test_html_external_url_in_ai_text_is_inert(self):
        self.write_run()
        page = self.html()
        # the only https:// that may appear is inside escaped text, never as a live attribute
        for m in re.finditer(r"https?://", page):
            before = page[max(0, m.start() - 20):m.start()]
            self.assertIn("&quot;", before)

    def test_html_default_path(self):
        self.write_run()
        code, out, err = self.run_export("-d", self.proj, "--format", "html")
        self.assertEqual(code, 0, err)
        target = paths.project_state_dir(self.proj) / "report.html"
        self.assertTrue(target.is_file())
        self.assertIn(str(target), out)
        self.assertIn("<html", target.read_text(encoding="utf-8"))


# The fixture's only URL is the escaped <img> in t1's summary; without it the page has no URL at all.
def _strip_urls(state_dir: Path):
    st = json.loads((state_dir / "state.json").read_text())
    st["tasks"][0]["summary"] = "API done"
    (state_dir / "state.json").write_text(json.dumps(st))


class TestHtmlStrict(ExportBase):
    def test_no_url_at_all_when_ai_text_has_none(self):
        _strip_urls(self.write_run())
        code, out, err = self.run_export("-d", self.proj, "--format", "html", "-o", "-")
        self.assertEqual(code, 0, err)
        self.assertNotRegex(out, r"(?i)https?://")


class TestOutputAndLogs(ExportBase):
    def test_output_file_creates_parents_for_each_format(self):
        self.write_run()
        for fmt, probe in (("md", "# todo-app"), ("json", '"goal"'), ("html", "<!doctype html>")):
            target = self.tmp / "out" / fmt / "nested" / f"run.{fmt}"
            code, out, err = self.run_export("-d", self.proj, "--format", fmt, "-o", target)
            self.assertEqual(code, 0, err)
            self.assertTrue(target.is_file(), fmt)
            self.assertIn(probe, target.read_text(encoding="utf-8"))
            self.assertIn(str(target.resolve()), out)
            self.assertNotIn(probe, out)              # content went to the file, not stdout
        json.loads((self.tmp / "out" / "json" / "nested" / "run.json").read_text())

    def test_output_into_existing_directory(self):
        self.write_run()
        dest = self.tmp / "exports"
        dest.mkdir()
        code, out, err = self.run_export("-d", self.proj, "-o", dest)
        self.assertEqual(code, 0, err)
        self.assertTrue((dest / "todo-app-omnicli-run.md").is_file())

    def write_logs(self, sd: Path):
        big = "HEAD-MARKER\n" + ("a" * 15000) + "MIDDLE-MARKER" + ("b" * 15000) + "\nTAIL-MARKER " + XSS + "\n"
        (sd / "logs" / "t1-codex-1.md").write_text(big, encoding="utf-8")
        (sd / "logs" / "t1-codex-1.prompt.md").write_text("PROMPT-SHOULD-NOT-APPEAR", encoding="utf-8")
        (sd / "logs" / "t1-codex-2.md").write_text("small log for revision 1\n=== SUMMARY ===\nok", encoding="utf-8")
        (sd / "logs" / "t2-agy-1.md").write_text("starting...", encoding="utf-8")
        return len(big)

    def test_include_logs_truncates_with_note(self):
        sd = self.write_run()
        size = self.write_logs(sd)
        data = ex.gather(self.proj, include_logs=True)
        log1 = data["tasks"][0]["attempts"][0]["log"]
        self.assertTrue(log1["found"])
        self.assertTrue(log1["truncated"])
        self.assertEqual(log1["chars"], size)
        self.assertLessEqual(len(log1["text"]), ex.LOG_LIMIT + 400)
        self.assertIn("HEAD-MARKER", log1["text"])
        self.assertIn("TAIL-MARKER", log1["text"])
        self.assertNotIn("MIDDLE-MARKER", log1["text"])
        self.assertIn("characters omitted", log1["text"])
        log2 = data["tasks"][0]["attempts"][1]["log"]
        self.assertFalse(log2["truncated"])
        self.assertIn("small log for revision 1", log2["text"])
        self.assertTrue(data["tasks"][1]["attempts"][0]["log"]["found"])
        self.assertFalse(data["tasks"][1]["attempts"][1]["log"]["found"])     # t2-codex-2.md never written
        self.assertTrue(data["export"]["include_logs"])

        code, md, err = self.run_export("-d", self.proj, "--include-logs")
        self.assertEqual(code, 0, err)
        self.assertIn(f"**Output log - attempt 1 (codex, truncated from {size:,} chars)**", md)
        self.assertIn("small log for revision 1", md)
        self.assertIn("characters omitted", md)
        self.assertIn("_Output log for attempt 2 (codex) not found (t2-codex-2.md)._", md)
        self.assertNotIn("PROMPT-SHOULD-NOT-APPEAR", md)

        code, page, err = self.run_export("-d", self.proj, "--include-logs", "--format", "html", "-o", "-")
        self.assertEqual(code, 0, err)
        self.assertIn('<details class="log"><summary>Output log (truncated from', page)
        self.assertIn("TAIL-MARKER " + XSS_ESCAPED, page)
        self.assertNotIn("<script", page.lower())

        _, plain, _ = self.run_export("-d", self.proj)
        self.assertNotIn("HEAD-MARKER", plain)

    def test_log_reader_stays_inside_logs_dir(self):
        sd = self.write_run()
        (sd / "secret-x-1.md").write_text("TOP SECRET", encoding="utf-8")
        info = ex.read_attempt_log(sd / "logs", "../secret", "x", 1)
        self.assertFalse(info["found"])
        self.assertEqual(info["text"], "")

    def test_log_found_under_other_agent_name(self):
        sd = self.write_run()
        (sd / "logs" / "t9-muse-1.md").write_text("reassigned output", encoding="utf-8")
        info = ex.read_attempt_log(sd / "logs", "t9", "claude", 1)
        self.assertTrue(info["found"])
        self.assertEqual(info["text"], "reassigned output")


class TestNoGitAndResolution(ExportBase):
    def test_export_without_git(self):
        self.write_run()
        data = ex.gather(self.proj)
        self.assertFalse(data["git"]["repo"])
        self.assertEqual(data["git"]["commits"], [])
        code, md, err = self.run_export("-d", self.proj)
        self.assertEqual(code, 0, err)
        self.assertIn("not a git repository", md)
        code, page, err = self.run_export("-d", self.proj, "--format", "html", "-o", "-")
        self.assertIn("not a git repository", page)

    def test_git_repo_without_commits(self):
        if not HAS_GIT:
            self.skipTest("git not installed")
        self.write_run()
        subprocess.run(["git", "init", "-q"], cwd=str(self.proj), check=True)
        data = ex.gather(self.proj)
        self.assertTrue(data["git"]["repo"])
        self.assertEqual(data["git"]["commits"], [])
        self.assertIn("No commits yet", ex.render_md(data))

    def test_nothing_to_export(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        code, out, err = self.run_export(cwd=empty)
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Nothing to export", err)
        self.assertIn("omnicli team", err)

    def test_dir_without_run(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        code, out, err = self.run_export("-d", empty)
        self.assertEqual(code, 1)
        self.assertIn("No OmniCLI team run in", err)
        self.assertIn("omnicli team", err)

    def test_corrupt_state(self):
        sd = paths.project_state_dir(self.proj)
        sd.mkdir(parents=True)
        (sd / "state.json").write_text("{not json")
        code, out, err = self.run_export("-d", self.proj)
        self.assertEqual(code, 1)
        self.assertIn("Could not read", err)

    def test_ref_by_name_index_and_path(self):
        self.write_run()
        for ref in ("todo", "1", str(self.proj)):
            code, out, err = self.run_export(ref, "--format", "json")
            self.assertEqual(code, 0, f"{ref}: {err}")
            self.assertEqual(json.loads(out)["dir"], str(self.proj))
        code, out, err = self.run_export("no-such-run")
        self.assertEqual(code, 1)
        self.assertIn("omnicli runs", err)

    def test_dir_flag_beats_ref(self):
        self.write_run()
        other = self.tmp / "other"
        other.mkdir()
        self.write_run(project=other, register=False, goal="Other goal")
        code, out, _ = self.run_export("todo", "-d", other, "--format", "json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["goal"], "Other goal")

    def test_cwd_run_then_most_recent(self):
        self.write_run()                                   # registered
        other = self.tmp / "other"
        other.mkdir()
        self.write_run(project=other, register=False, goal="Cwd goal")
        code, out, _ = self.run_export("--format", "json", cwd=other)
        self.assertEqual(json.loads(out)["goal"], "Cwd goal")      # cwd wins
        empty = self.tmp / "empty"
        empty.mkdir()
        code, out, _ = self.run_export("--format", "json", cwd=empty)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["dir"], str(self.proj))    # falls back to most recent run


class TestRegistration(ExportBase):
    def test_plugin_loader_registers_export(self):
        from omnicli.commands import register_all
        parser = argparse.ArgumentParser(prog="omnicli")
        sub = parser.add_subparsers(dest="subcommand")
        with contextlib.redirect_stderr(io.StringIO()):
            names = register_all(sub)
        self.assertIn("export", names)
        args = parser.parse_args(["export", "2", "--format", "html", "-o", "x.html", "--include-logs"])
        self.assertIs(args.func, ex.handler)
        self.assertEqual((args.ref, args.format, args.output, args.include_logs), ("2", "html", "x.html", True))

    def test_bad_format_rejected_by_argparse(self):
        parser = argparse.ArgumentParser(prog="omnicli")
        ex.add_parser(parser.add_subparsers(dest="subcommand"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["export", "--format", "pdf"])


class TestHelpers(unittest.TestCase):
    def test_duration_and_cells(self):
        self.assertEqual(ex.fmt_duration(0), "0s")
        self.assertEqual(ex.fmt_duration(45), "45s")
        self.assertEqual(ex.fmt_duration(725), "12m05s")
        self.assertEqual(ex.fmt_duration(3725), "1h02m05s")
        self.assertEqual(ex._cell("a | b\nc"), "a \\| b c")
        self.assertEqual(ex._cell(""), "-")
        self.assertEqual(ex._code_span("a`b"), "``a`b``")


if __name__ == "__main__":
    unittest.main()
