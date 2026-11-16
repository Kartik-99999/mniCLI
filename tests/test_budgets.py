import os
import tempfile
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-budget-test-")

from omnicli.budgets import read_control, write_control
from omnicli.conductor import Conductor, TeamConfig


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="omnicli-budget-case-")
        os.environ["OMNICLI_HOME"] = self.home
        self.project = Path(self.home) / "project"

    def test_live_budget_override_is_read_by_conductor(self):
        c = Conductor(TeamConfig(goal="g", project_dir=self.project, max_calls=0), specs={}, health={}, verbose=False)
        c.calls = 2
        write_control(self.project, {"max_calls": 2, "max_minutes": 0})
        self.assertEqual(c._budget_hit(), "2/2 AI calls")
        write_control(self.project, {"max_calls": 0})
        self.assertEqual(c._budget_hit(), "")
        self.assertEqual(read_control(self.project)["max_calls"], 0)


if __name__ == "__main__":
    unittest.main()
