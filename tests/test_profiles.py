import os
import tempfile
import unittest

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-profiles-test-")

from omnicli import profiles, settings


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="omnicli-profile-case-")
        os.environ["OMNICLI_HOME"] = self.home

    def test_save_and_apply_round_trip(self):
        settings.set_value("lead", "codex")
        settings.set_value("workers", "claude,muse")
        settings.set_value("jobs", "3")
        profiles.save("frontend")

        settings.set_value("lead", "claude")
        settings.set_value("jobs", "1")
        applied = profiles.apply("frontend")

        self.assertEqual(applied["lead"], "codex")
        self.assertEqual(settings.get("lead"), "codex")
        self.assertEqual(settings.get("workers"), ["claude", "muse"])
        self.assertEqual(settings.get("jobs"), 3)

    def test_list_and_delete(self):
        profiles.save("daily")
        self.assertIn("daily", profiles.load_all())
        self.assertTrue(profiles.delete("daily"))
        self.assertFalse(profiles.delete("daily"))

    def test_rejects_unsafe_or_missing_names(self):
        with self.assertRaises(profiles.ProfileError):
            profiles.save("../outside")
        with self.assertRaises(profiles.ProfileError):
            profiles.get("missing")


if __name__ == "__main__":
    unittest.main()
