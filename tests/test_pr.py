import contextlib
import io
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-pr-test-")

from omnicli.commands import pr


class PullRequestConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.project = Path(tempfile.mkdtemp(prefix="omnicli-pr-project-"))
        self.state = {"goal": "Ship a small feature", "phase": "complete", "tasks": []}
        self.args = Namespace(ref=None, dir=str(self.project), base="main", title=None, body=None,
                              draft=False, yes=False)

    def run_handler(self, answer=None, raises=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with mock.patch.object(pr, "resolve", return_value=(self.project, self.state)), \
                    mock.patch.object(pr, "create_pr", return_value=mock.Mock(returncode=0, stdout="created", stderr="")) as create:
                if raises:
                    prompt = mock.patch("builtins.input", side_effect=raises)
                else:
                    prompt = mock.patch("builtins.input", return_value=answer)
                with prompt as input_mock:
                    code = pr.handler(self.args)
        return code, output.getvalue(), create, input_mock

    def test_declining_does_not_call_github(self):
        code, output, create, input_mock = self.run_handler("n")
        self.assertEqual(code, 0)
        self.assertIn("Create this PR? [y/N]", input_mock.call_args.args[0])
        self.assertIn("cancelled", output)
        create.assert_not_called()

    def test_confirmation_allows_creation(self):
        code, output, create, _input = self.run_handler("yes")
        self.assertEqual(code, 0)
        self.assertIn("created", output)
        create.assert_called_once()

    def test_yes_flag_skips_prompt(self):
        self.args.yes = True
        code, output, create, input_mock = self.run_handler()
        self.assertEqual(code, 0)
        create.assert_called_once()
        input_mock.assert_not_called()

    def test_end_of_input_cancels_safely(self):
        code, output, create, _input = self.run_handler(raises=EOFError)
        self.assertEqual(code, 0)
        self.assertIn("cancelled", output)
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
