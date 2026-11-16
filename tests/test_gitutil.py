"""
Revisions read from state.json must never reach git as options (security review 2026-11-16).
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

from omnicli.gitutil import safe_rev  # noqa: E402


class TestSafeRev(unittest.TestCase):
    def test_only_hex_commit_ids_pass(self):
        self.assertEqual(safe_rev("be7d264"), "be7d264")
        self.assertEqual(safe_rev(" 0bd8a2b47c01af6b4c809b927c41aff8a099a39a "), "0bd8a2b47c01af6b4c809b927c41aff8a099a39a")
        for bad in ("--output=/tmp/x", "-p", "HEAD", "main", "abc", "be7d264;rm", "../x", "", None, 123):
            self.assertIsNone(safe_rev(bad), bad)


class TestStateCommitsCannotInjectGitOptions(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="omni-git-"))
        run = lambda *a: subprocess.run(["git", *a], cwd=self.repo, capture_output=True, text=True, check=True)
        run("init", "-q")
        (self.repo / "f.txt").write_text("x\n")
        run("add", "f.txt")
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c1")
        self.evil = self.repo / "pwned.txt"

    def test_compare_ignores_malicious_commit(self):
        from omnicli.commands import compare
        self.assertEqual(compare._commit_summary(str(self.repo), f"--output={self.evil}"), "")
        self.assertFalse(self.evil.exists())

    def test_timeline_diff_ignores_malicious_commit(self):
        import argparse
        import contextlib
        import io
        from omnicli import runs
        from omnicli.commands import timeline
        st = self.repo / ".omnicli" / "team"
        st.mkdir(parents=True)
        (st / "state.json").write_text(json.dumps({
            "goal": "g", "phase": "complete", "pid": 0, "tasks": [
                {"id": "t1", "title": "x", "status": "done", "commit": f"--output={self.evil}", "attempts": []}]}))
        runs.register_run({"dir": str(self.repo), "goal": "g", "phase": "complete"})
        handler = getattr(timeline, "handler", None) or getattr(timeline, "run", None)
        self.assertIsNotNone(handler)
        args = argparse.Namespace(ref=str(self.repo), run=str(self.repo), dir=str(self.repo), diff=True, json=False)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                handler(args)
            except SystemExit:
                pass
        self.assertFalse(self.evil.exists())


if __name__ == "__main__":
    unittest.main()
