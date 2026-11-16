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


if __name__ == "__main__":
    unittest.main()
