import os
import tempfile
import unittest

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-skills-test-")

from omnicli import ledger
from omnicli.conductor import classify_task, normalize_tasks


class SkillHistoryTests(unittest.TestCase):
    def setUp(self):
        os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-skills-case-")

    def test_category_inference_and_lead_category_override(self):
        self.assertEqual(classify_task("Add API endpoints"), "backend")
        self.assertEqual(classify_task("Write unit tests"), "tests")
        tasks = normalize_tasks([{"id": "t1", "title": "general task", "category": "frontend"}], ["codex"])
        self.assertEqual(tasks[0].category, "frontend")

    def test_rankings_use_latest_outcome_per_task_and_minimum_sample(self):
        for index in range(3):
            ledger.record_task_outcome({"ts": index + 1, "run_dir": "/p", "task_id": "t{}".format(index),
                                        "round": 1, "category": "backend", "agent": "codex",
                                        "status": "done", "approved_first_try": index != 0,
                                        "revisions": 0 if index else 1, "duration_sec": 60})
        ledger.record_task_outcome({"ts": 5, "run_dir": "/p", "task_id": "t0", "round": 1,
                                    "category": "backend", "agent": "codex", "status": "done",
                                    "approved_first_try": True, "revisions": 0, "duration_sec": 30})
        rows = ledger.skill_rankings()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tasks"], 3)
        self.assertEqual(rows[0]["first_try"], 3)
        recommendation = ledger.skill_recommendations(min_tasks=3)
        self.assertEqual(recommendation["backend"]["agent"], "codex")


if __name__ == "__main__":
    unittest.main()
