import datetime
import os
import tempfile
import unittest
from pathlib import Path

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-quota-test-")

from omnicli import agents, ledger
from omnicli.conductor import Conductor, TeamConfig


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="omnicli-quota-case-")
        os.environ["OMNICLI_HOME"] = self.home

    def test_parses_clock_reset_time(self):
        now = datetime.datetime(2026, 1, 1, 12, 0).timestamp()
        reset = agents.parse_quota_reset("You've hit your 5-hour limit · resets 2:20am", now=now)
        expected = datetime.datetime(2026, 1, 2, 2, 20).timestamp()
        self.assertEqual(reset, expected)
        self.assertIsNone(agents.parse_quota_reset("rate limit reached", now=now))

    def test_persists_active_quota_reset(self):
        ledger.record_quota_reset("claude", 200, "limit reached")
        self.assertIn("claude", ledger.active_quota_resets(now=100))
        self.assertNotIn("claude", ledger.active_quota_resets(now=200))

    def test_scheduler_avoids_capped_agent(self):
        c = Conductor(TeamConfig(goal="g", project_dir=Path(self.home)), specs={}, health={}, verbose=False)
        c.workers = ["claude", "codex"]
        c.quota_resets = {"claude": {"reset_at": 9999999999}}
        self.assertEqual(c._pick_worker([]), "codex")


if __name__ == "__main__":
    unittest.main()
