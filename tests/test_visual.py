import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-visual-test-")

from omnicli.commands import visual


class VisualTests(unittest.TestCase):
    def test_local_url_validation(self):
        self.assertEqual(visual._local_url("http://localhost:8000"), "http://localhost:8000")
        self.assertEqual(visual._local_url("http://127.0.0.1:9000"), "http://127.0.0.1:9000")
        with self.assertRaises(ValueError):
            visual._local_url("https://example.com")

    @mock.patch("omnicli.commands.visual.subprocess.run")
    @mock.patch("omnicli.commands.visual.shutil.which", return_value="/usr/sbin/screencapture")
    def test_capture_uses_system_screenshot_tool(self, _which, run):
        output = Path(tempfile.mkdtemp()) / "shot.png"

        def capture(command, **kwargs):
            output.write_bytes(b"image")
            return __import__("subprocess").CompletedProcess(command, 0, "", "")

        run.side_effect = capture
        self.assertEqual(visual.capture_screen(output), output)
        self.assertEqual(run.call_args.args[0], ["/usr/sbin/screencapture", "-x", str(output)])


if __name__ == "__main__":
    unittest.main()
