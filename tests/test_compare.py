import os
import tempfile
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-compare-test-")

from omnicli import runs
from omnicli.commands.compare import compare
from omnicli.paths import project_state_dir


class CompareTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="omnicli-compare-case-")
        os.environ["OMNICLI_HOME"] = self.home
        self.a = Path(self.home) / "run-a"
        self.b = Path(self.home) / "run-b"
        self._write(self.a, "Build a tool", [{"id": "t1", "title": "API", "status": "done",
                                                "assignee": "claude", "agent_used": "claude", "commit": "abc",
                                                "attempts": [{"verify_exit": 0}]}])
        self._write(self.b, "Build a tool", [{"id": "t1", "title": "API", "status": "failed",
                                                "assignee": "codex", "agent_used": "codex"},
                                               {"id": "t2", "title": "Docs", "status": "pending"}])

    def _write(self, folder, goal, tasks):
        folder.mkdir()
        state_dir = project_state_dir(folder)
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text(__import__("json").dumps({
            "goal": goal, "phase": "complete", "tasks": tasks,
        }), encoding="utf-8")
        runs.register_run({"dir": str(folder), "goal": goal, "project_name": folder.name,
                           "phase": "complete", "tasks_total": len(tasks), "tasks_done": 0,
                           "tasks_failed": 0, "round": 1, "started_at": 1})

    def test_compares_matching_and_missing_tasks(self):
        result = compare(str(self.a), str(self.b))
        rows = {row["id"]: row for row in result["tasks"]}
        self.assertEqual(rows["t1"]["status_a"], "done")
        self.assertEqual(rows["t1"]["status_b"], "failed")
        self.assertEqual(rows["t1"]["commit_a"], "abc")
        self.assertEqual(rows["t1"]["verify_exit_a"], 0)
        self.assertEqual(rows["t2"]["status_a"], "missing")


if __name__ == "__main__":
    unittest.main()
