import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-timeline-test-")

from omnicli.commands.timeline import timeline_events
from omnicli.commands.sessions import prepare_fork
from omnicli.paths import project_state_dir
from omnicli import runs


class TimelineTests(unittest.TestCase):
    def test_events_are_ordered_and_include_revisions_and_checkpoints(self):
        state = {"tasks": [{"id": "t1", "agent_used": "codex", "commit": "abc123", "summary": "Added API",
                             "attempts": [{"at": "2026-11-16T10:00:00", "agent": "codex", "result": "revise",
                                           "feedback": "Add a test."},
                                          {"at": "2026-11-16T10:02:00", "agent": "codex", "result": "approved"}]}]}
        events = timeline_events(state)
        self.assertEqual([event["type"] for event in events], ["attempt", "attempt", "checkpoint"])
        self.assertEqual(events[-1]["commit"], "abc123")
        self.assertEqual(events[0]["feedback"], "Add a test.")

    def test_fork_rewinds_to_task_and_changes_its_assignee(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=str(source), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=str(source), check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(source), check=True)
            (source / "base.txt").write_text("base\n")
            subprocess.run(["git", "add", "base.txt"], cwd=str(source), check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=str(source), check=True)
            (source / "first.txt").write_text("first\n")
            subprocess.run(["git", "add", "first.txt"], cwd=str(source), check=True)
            subprocess.run(["git", "commit", "-qm", "t1"], cwd=str(source), check=True)
            first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(source), capture_output=True,
                                   text=True, check=True).stdout.strip()
            (source / "second.txt").write_text("second\n")
            subprocess.run(["git", "add", "second.txt"], cwd=str(source), check=True)
            subprocess.run(["git", "commit", "-qm", "t2"], cwd=str(source), check=True)
            second = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(source), capture_output=True,
                                    text=True, check=True).stdout.strip()
            tasks = [{"id": "t1", "status": "done", "commit": first, "assignee": "codex", "attempts": []},
                     {"id": "t2", "status": "done", "commit": second, "assignee": "codex", "attempts": []}]
            state = {"dir": str(source), "goal": "test fork", "project_name": "fork-test", "phase": "complete",
                     "lead": "claude", "workers": ["codex"], "round": 1, "tasks": tasks}
            state_path = project_state_dir(source) / "state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps(state))
            runs.register_run({"dir": str(source), "project_name": "fork-test", "phase": "complete"})
            target, fork_state, workers = prepare_fork(str(source), "t2", "muse", str(root / "forked"))
            self.assertTrue((target / "first.txt").exists())
            self.assertFalse((target / "second.txt").exists())
            self.assertEqual([task["status"] for task in fork_state["tasks"]], ["done", "pending"])
            self.assertEqual(fork_state["tasks"][1]["assignee"], "muse")
            self.assertIn("muse", workers)


if __name__ == "__main__":
    unittest.main()
