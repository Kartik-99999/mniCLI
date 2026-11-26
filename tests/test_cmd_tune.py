"""Exercise tune through the CLI with local subprocesses and isolated settings."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-test-home-")

from omnicli import cli, paths, settings
from omnicli.commands import tune


class TestTune(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        env = mock.patch.dict(os.environ, {"OMNICLI_HOME": folder.name})
        env.start()
        self.addCleanup(env.stop)
        self.log = self.root / "calls.jsonl"
        script = self.root / "trial.py"
        script.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "prompt = Path(sys.argv[1]).read_text()\n"
            "flags = sys.argv[2:]\n"
            "with open({!r}, 'a') as log:\n"
            "    log.write(json.dumps([flags, prompt, os.getcwd()]) + '\\n')\n"
            "if 'broken' in flags: sys.exit(5)\n"
            "print(json.dumps({{'answers': {{'case': 'held-out answer' if 'high' in flags else None}}}}))\n"
            .format(str(self.log)), encoding="utf-8")
        custom = {"bin": sys.executable, "readonly": [str(script), "{prompt_file}"],
                  "effort_args": ["--tier", "{effort}"], "effort_levels": ["low", "high", "broken"],
                  "model_args": ["--model", "{model}"]}
        paths.agents_config_path().write_text(json.dumps({"agents": {"testcli": custom},
            "disabled": ["claude", "codex", "agy", "muse"]}))
        self.cases = self.root / "examples.json"
        self.cases.write_text(json.dumps({"case": {"prompt": "Give the sample answer.", "expected": "held-out answer"}}))

    def run_cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            rc = cli.main(["--no-banner", "tune", "--cases", str(self.cases)] + list(args))
        return rc, output.getvalue()

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_dry_run_scores_shared_roles_once_without_exposing_answers(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("CLI default: 0/1", out)
        self.assertIn("high: 1/1", out)
        calls = self.calls()
        self.assertEqual([call[0] for call in calls], [[], ["--tier", "high"]])
        self.assertEqual(calls[0][1], calls[1][1])
        self.assertNotIn("held-out answer", calls[0][1])
        self.assertNotEqual(calls[0][2], calls[1][2])
        self.assertFalse(settings.settings_path().exists())

    def test_apply_improvement_preserves_lead_inheritance_and_ignores_failed_candidate(self):
        rc, out = self.run_cli("--efforts", "broken,high,high", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("exit code 5", out)
        self.assertEqual(settings.get("effort.testcli"), "high")
        self.assertIsNone(settings.get("lead_effort.testcli"))

    def test_separate_models_are_compared_independently_and_ties_keep_the_baseline(self):
        settings.set_value("models.testcli", "worker")
        settings.set_value("lead_models.testcli", "planner")
        settings.set_value("effort.testcli", "low")
        rc, out = self.run_cli("testcli", "--efforts", "low", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual([c[0] for c in self.calls()], [["--model", "planner"], ["--model", "planner", "--tier", "low"]])
        self.assertEqual(settings.get("effort.testcli"), "low")
        self.assertIsNone(settings.get("lead_effort.testcli"))
        rc, out = self.run_cli("--efforts", "high", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(settings.get("effort.testcli"), "high")
        self.assertEqual(settings.get("lead_effort.testcli"), "high")

    def test_default_lead_model_effort_remains_independent(self):
        settings.set_value("lead_models.testcli", "default")
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(settings.get("lead_effort.testcli"), "high")
        self.assertEqual(len(self.calls()), 2)

    def test_catalog_models_are_scored_without_overwriting_the_selected_model(self):
        settings.set_value("models.testcli", "chosen")
        catalog = {"models": [{"id": "other"}, {"id": "chosen"}], "cli_default": "other"}
        with mock.patch.object(tune.model_catalog, "discover", return_value=catalog):
            rc, out = self.run_cli("--all-models", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("other (catalog model)", out)
        self.assertEqual([argv[:2] for argv, _, _ in self.calls()],
                         [["--model", "chosen"]] * 2 + [["--model", "other"]] * 2)
        self.assertEqual(settings.get("models.testcli"), "chosen")
        self.assertEqual(settings.get("effort.testcli"), "high")

    def test_failure_to_measure_baseline_never_saves_a_tier(self):
        settings.set_value("effort.testcli", "broken")
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 1, out)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(settings.get("effort.testcli"), "broken")

    def test_invalid_input_and_unsupported_tier_consume_no_calls(self):
        self.assertEqual(self.run_cli("unknown")[0], 1)
        self.assertEqual(self.run_cli("--efforts", "turbo")[0], 1)
        self.cases.write_text('{"case": {"prompt": "missing answer"}}')
        self.assertEqual(self.run_cli()[0], 1)
        self.assertFalse(self.log.exists())

    def test_scoring_missing_answers_and_json_types(self):
        cases = {"a": {"expected": None}, "b": {"expected": 1}, "c": {"expected": {"x": 1, "y": 2}}}
        self.assertEqual(tune._score('{"answers":{"b":true,"c":{"y":2,"x":1}}}', cases), 1)
        self.assertEqual(tune._score('{"answers":{"a":null}}', cases), 1)
        self.assertEqual(tune._score("unstructured", cases), 0)
