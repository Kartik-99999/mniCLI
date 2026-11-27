import os
import random
import tempfile
import unittest

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-arena-test-")

from omnicli.commands.arena import anonymize_candidates, blind_labels


class ArenaTests(unittest.TestCase):
    def test_candidates_get_anonymous_labels(self):
        pairs = blind_labels(["claude", "codex", "muse"], random.Random(7))
        self.assertEqual({label for label, _agent in pairs}, {"A", "B", "C"})
        self.assertEqual({agent for _label, agent in pairs}, {"claude", "codex", "muse"})
        self.assertNotEqual(pairs, [("A", "claude"), ("B", "codex"), ("C", "muse")])

    def test_blind_candidate_map_keeps_results_without_leaking_names_in_labels(self):
        rows = [{"agent": "claude", "diff": "one"}, {"agent": "codex", "diff": "two"}]
        labels, blind = anonymize_candidates(rows, random.Random(4))
        self.assertEqual(set(blind), {label for label, _agent in labels})
        self.assertEqual({row["diff"] for row in blind.values()}, {"one", "two"})
        self.assertNotIn("claude", blind)
        self.assertEqual(list(blind), ["A", "B"])
        self.assertEqual(blind["A"]["agent"], "codex")


if __name__ == "__main__":
    unittest.main()
