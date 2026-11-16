import os
import tempfile
import unittest

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-memory-test-")

from pathlib import Path

from omnicli import ledger
from omnicli.conductor import Task, build_worker_prompt


class TeamMemoryTests(unittest.TestCase):
    def setUp(self):
        os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-memory-case-")

    def test_lessons_are_deduplicated_and_filtered(self):
        ledger.record_team_lesson("Run unittest from the repository root", "tests", "t1")
        ledger.record_team_lesson("  Run unittest from the repository root  ", "tests", "t2")
        ledger.record_team_lesson("Document the configuration", "docs", "t3")
        self.assertEqual(len(ledger.read_team_memory()), 2)
        self.assertEqual(len(ledger.read_team_memory("tests")), 1)

    def test_relevant_lessons_are_added_to_worker_prompt(self):
        prompt = build_worker_prompt(Task("t1", "Add tests", "codex", "Write tests", category="tests"),
                                     "Improve project", Path("/project"), "Codex", "Claude", [],
                                     team_memory=[{"lesson": "Use unittest, not pytest."}])
        self.assertIn("TEAM MEMORY", prompt)
        self.assertIn("Use unittest, not pytest.", prompt)

    def test_lessons_stay_in_their_project_unless_global(self):
        from omnicli import settings
        a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
        ledger.record_team_lesson("Use the repo's logger", "backend", "t1", project=a)
        self.assertEqual(len(ledger.read_team_memory("backend", project=a)), 1)
        self.assertEqual(ledger.read_team_memory("backend", project=b), [])  # no cross-project leak
        settings.set_value("memory_scope", "global")
        self.assertEqual(len(ledger.read_team_memory("backend", project=b)), 1)
        settings.unset("memory_scope")

    def test_lessons_are_sanitized(self):
        ledger.record_team_lesson("Read /home/example/secret-proj/config.yaml and mail bob@example.invalid "
                                  "with key fake_0123456789abcdefghijklmnopqrstuvwx", "other", "t1", project="/p")
        text = ledger.read_team_memory()[0]["lesson"]
        self.assertNotIn("/Users/", text)
        self.assertNotIn("bob@example.invalid", text)
        self.assertNotIn("0123456789abcdef", text)
        self.assertIn("<path>", text)


if __name__ == "__main__":
    unittest.main()
