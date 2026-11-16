"""
End-to-end arena run with fake agents and a real git repo (no AI CLI is called).
Regression for two live-run crashes: the judge prompt's JSON braces (KeyError) and the
result record looping over (label, name) pairs instead of candidate rows (TypeError).
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

from omnicli import paths  # noqa: E402

FAKE = str(Path(__file__).with_name("fake_agent.py"))


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout.strip()


class TestArenaEndToEnd(unittest.TestCase):
    def setUp(self):
        home = paths.omni_home()
        home.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="omni-arena-"))
        judge = tmp / "judge.py"
        judge.write_text('print(\'Candidate A is better.\\n{"winner": "A", "reason": "passes"}\')\n')
        os.environ["FAKE_STATE"] = str(tmp / "fake-state")

        def worker(mode):
            return {"name": "Fake " + mode, "bin": sys.executable, "strengths": "tests", "vendor": mode,
                    "readonly": [FAKE, "good", "{prompt_file}"], "edit": [FAKE, "good", "{prompt_file}"]}
        agents_cfg = {
            "agents": {"w1": worker("one"), "w2": worker("two"),
                       "judge": {"name": "Judge", "bin": sys.executable, "strengths": "judging", "vendor": "j",
                                 "readonly": [str(judge)], "edit": [str(judge)]}},
            "disabled": ["claude", "codex", "agy", "muse"],
        }
        paths.agents_config_path().write_text(json.dumps(agents_cfg))
        now = time.time()
        paths.health_cache_path().write_text(json.dumps(
            {n: {"ok": True, "reason": "ok", "checked_at": now} for n in ("w1", "w2", "judge")}))

        self.proj = tmp / "proj"
        self.proj.mkdir()
        git(self.proj, "init", "-q")
        git(self.proj, "config", "user.name", "t")
        git(self.proj, "config", "user.email", "t@t")
        (self.proj / ".gitignore").write_text(".omnicli/\n")
        (self.proj / "README.md").write_text("base\n")
        git(self.proj, "add", "-A")
        git(self.proj, "commit", "-q", "-m", "base")
        (self.proj / "a").mkdir()
        (self.proj / "a" / "a.txt").write_text("first version\n")
        git(self.proj, "add", "-A")
        git(self.proj, "commit", "-q", "-m", "t1: Part A")
        t1_commit = git(self.proj, "rev-parse", "--short", "HEAD")
        st = self.proj / ".omnicli" / "team"
        st.mkdir(parents=True)
        (st / "state.json").write_text(json.dumps({
            "version": 1, "goal": "fake goal", "project_name": "arena-demo", "lead": "judge",
            "workers": ["w1", "w2"], "round": 1, "phase": "complete", "pid": 0, "final": {},
            "tasks": [{"id": "t1", "title": "Part A", "assignee": "w1", "instructions": "make a/a.txt",
                       "owns": ["a"], "verify": "test -f a/a.txt", "status": "done", "agent_used": "w1",
                       "attempts": [], "commit": t1_commit}]}))
        from omnicli import runs
        runs.register_run({"dir": str(self.proj), "goal": "fake goal", "phase": "complete"})

    def test_full_arena_run(self):
        from omnicli.commands.arena import run_arena
        record = run_arena(str(self.proj), "t1", ["w1", "w2"], "judge")
        self.assertIn(record["winner"], ("w1", "w2"))
        self.assertEqual(record["blind_label"], "A")
        self.assertEqual(sorted(c["label"] for c in record["candidates"]), ["A", "B"])
        saved = list((self.proj / ".omnicli" / "team" / "arena-results").glob("*.json"))
        self.assertEqual(len(saved), 1)
        # the winner's change is staged for inspection, not committed
        self.assertIn("a/a.txt", git(self.proj, "diff", "--cached", "--name-only"))
        self.assertEqual(git(self.proj, "log", "--format=%s", "-1"), "t1: Part A")
        # temporary arena worktrees are cleaned up
        self.assertEqual(len(git(self.proj, "worktree", "list").splitlines()), 1)

    def _later_commit(self, rel, text, msg):
        (self.proj / rel).write_text(text)
        git(self.proj, "add", "-A")
        git(self.proj, "commit", "-q", "-m", msg)

    def test_later_work_on_other_files_is_kept(self):
        from omnicli.commands.arena import run_arena
        self._later_commit("README.md", "edited by a later task\n", "t2: docs")
        run_arena(str(self.proj), "t1", ["w1", "w2"], "judge")
        self.assertEqual((self.proj / "README.md").read_text(), "edited by a later task\n")
        self.assertEqual((self.proj / "a" / "a.txt").read_text(), "t1 by good\n")  # the winner's version
        # the project now equals exactly the winner's verified snapshot
        winner_tree = git(self.proj, "write-tree")
        record = json.loads(sorted((self.proj / ".omnicli" / "team" / "arena-results").glob("*.json"))[-1].read_text())
        self.assertEqual(winner_tree, record["winner_tree"])

    def test_refuses_when_later_work_touched_the_same_lines(self):
        from omnicli.commands.arena import run_arena
        self._later_commit("a/a.txt", "rewritten by a later task\n", "t2: rewrite a")
        head_before = git(self.proj, "rev-parse", "HEAD")
        with self.assertRaises(ValueError) as cm:
            run_arena(str(self.proj), "t1", ["w1", "w2"], "judge")
        self.assertIn("can't redo t1", str(cm.exception))
        self.assertEqual(git(self.proj, "rev-parse", "HEAD"), head_before)
        self.assertEqual(git(self.proj, "status", "--porcelain"), "")
        self.assertEqual(len(git(self.proj, "worktree", "list").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
