import os
import tempfile
import unittest
from unittest import mock

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-notifications-test-")

from omnicli import notifications


class NotificationTests(unittest.TestCase):
    def test_applescript_text_escapes_quotes_and_newlines(self):
        self.assertEqual(notifications._applescript_text('a "quote"\nnext'), 'a \\"quote\\" next')

    @mock.patch("omnicli.notifications.shutil.which", return_value=None)
    def test_returns_false_when_no_system_notifier_exists(self, _which):
        self.assertFalse(notifications.notify("OmniCLI", "finished"))

    @mock.patch("omnicli.notifications.subprocess.run")
    @mock.patch("omnicli.notifications.shutil.which", side_effect=lambda name: "/usr/bin/osascript" if name == "osascript" else None)
    def test_uses_osascript_without_a_shell(self, _which, run):
        run.return_value.returncode = 0
        self.assertTrue(notifications.notify("OmniCLI", "done"))
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/osascript")
        self.assertNotIn("shell", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
