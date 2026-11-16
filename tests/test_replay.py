import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-replay-test-")

from omnicli import runs
from omnicli.commands.sessions import prepare_replay
from omnicli.paths import project_state_dir


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="omnicli-replay-case-")
        os.environ["OMNICLI_HOME"] = self.home
        self.source = Path(self.home) / "source"
        state_dir = project_state_dir(self.source)
        state_dir.mkdir(parents=True)
        self.source_state = state_dir / "state.json"
        self.source_state.write_text(json.dumps({
            "goal": "Build a thing", "project_name": "thing", "phase": "complete", "round": 2,
            "tasks": [{"id": "t1", "title": "Make it", "status": "done", "agent_used": "claude",
                       "summary": "built", "attempts": [{"x": 1}], "commit": "deadbeef", "duration_sec": 5,
                       "workspace": "/old/run/worktree", "outcome_recorded": True}],
        }), encoding="utf-8")
        runs.register_run({"dir": str(self.source), "goal": "Build a thing", "project_name": "thing",
                           "phase": "complete", "tasks_total": 1, "tasks_done": 1, "tasks_failed": 0,
                           "round": 2, "started_at": 1})

    def test_replay_resets_results_and_preserves_source(self):
        target, state = prepare_replay(str(self.source), str(Path(self.home) / "replay"))
        task = state["tasks"][0]
        self.assertEqual(state["phase"], "planned")
        self.assertEqual(task["status"], "pending")
        self.assertEqual(task["attempts"], [])
        self.assertEqual(task["commit"], "")
        self.assertEqual(task["workspace"], "")
        self.assertFalse(task["outcome_recorded"])
        self.assertEqual(json.loads(self.source_state.read_text(encoding="utf-8"))["tasks"][0]["status"], "done")
        self.assertIsNotNone(runs.find_run(str(target)))

    def test_replay_refuses_nonempty_destination(self):
        target = Path(self.home) / "occupied"
        target.mkdir()
        (target / "keep.txt").write_text("keep", encoding="utf-8")
        with self.assertRaises(ValueError):
            prepare_replay(str(self.source), str(target))


if __name__ == "__main__":
    unittest.main()
