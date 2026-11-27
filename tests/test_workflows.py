import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(prefix="omnicli-workflows-")

from omnicli import workflows
from omnicli import agents
from omnicli.conductor import Conductor, Task, TeamConfig, build_worker_prompt


class TestWorkflowEvidence(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.project = Path(folder.name)
        self.task = Task("sheet", "Edit cells", "worker", "Implement formulas",
                         verify="node --test grid.test.js", status="done")
        self.workflow = {"scenario": "Change A1 from 4 to 8. C1 changes from 11 to 15.",
                         "kind": "interaction", "check_task": "sheet"}
        self.review = {"complete": True, "workflow_results": {
            "W1": {"status": "met", "evidence": "grid.test.js changed A1 and observed C1."}}}

    def test_plan_rejects_missing_task_and_file_checks_for_interactions(self):
        plans = [{"workflows": []}, {"workflows": [dict(self.workflow, check_task="unknown")]}]
        for plan in plans:
            with self.assertRaises(ValueError):
                workflows.from_plan(plan, [self.task])
        for command in ("test -f index.html && grep -q table index.html", "npm run build", "python3 -m compileall ."):
            self.task.verify = command
            with self.assertRaisesRegex(ValueError, "only inspects or builds files"):
                workflows.from_plan({"workflows": [self.workflow]}, [self.task])
        inspection = dict(self.workflow, scenario="Read the release notes.", kind="inspection", check_task="")
        self.assertEqual(workflows.from_plan({"workflows": [inspection]}, [self.task]), [inspection])

    def test_runtime_evidence_overrides_confident_review(self):
        cases = [([], "unverified"), ([(self.task.verify, (127, "", "runner unavailable"))], "unverified"),
                 ([(self.task.verify, (1, "C1 stayed at 11", ""))], "unmet"),
                 ([(self.task.verify, (0, "TAP version 13\n# tests 0\n# pass 0", ""))], "unverified"),
                 ([(self.task.verify, (0, "# tests 3\n# pass 0\n# skipped 2\n# todo 1", ""))], "unverified"),
                 ([(self.task.verify, (0, "# tests 3\n# pass 1\n# skipped 2", ""))], "met"),
                 ([(self.task.verify, (0, "OK", ""))], "met")]
        for checks, expected in cases:
            with self.subTest(expected=expected, checks=checks):
                row, = workflows.assess([self.workflow], [self.task], checks, self.review)
                self.assertEqual(row["status"], expected)
                self.assertEqual(row["scenario"], self.workflow["scenario"])
        self.assertTrue(workflows.final_reply_error(dict(self.review, complete="false"), [self.workflow]))
        self.assertTrue(workflows.final_reply_error(dict(self.review, workflow_results={}), [self.workflow]))
        self.assertIn("skipped", workflows.check_problem("python3 -m unittest",
                                                         (0, "Ran 1 test in 0.01s\n\nOK (skipped=1)", "")))

    def test_selection_preserves_working_features_and_retains_incomparable_options(self):
        def candidate(*checks, ok=True):
            return {"ok": ok, "passed": list(checks)}
        cases = {"regressed": candidate("slides", "sheet"), "original": candidate("word"),
                 "slides": candidate("word", "slides"), "sheet": candidate("word", "sheet"),
                 "same_coverage": candidate("word", "sheet"), "crashed": candidate(ok=False)}
        self.assertEqual(set(workflows.strongest_candidates(cases, {"word"})), {"slides", "sheet", "same_coverage"})
        cases["complete"] = candidate("word", "slides", "sheet")
        self.assertEqual(list(workflows.strongest_candidates(cases, {"word"})), ["complete"])
        self.assertFalse(workflows.strongest_candidates(cases, {"export"}))

    def test_failed_revision_preserves_earlier_feature_and_hands_off_within_budget(self):
        t = Task("format", "Add formatting", "first", "Format selected cells", verify="python3 format_test.py")
        commands = [self.task.verify, t.verify]
        journeys = [self.workflow, dict(self.workflow, check_task=t.id, scenario="Apply cell formatting.")]
        config = TeamConfig(goal="Build an editor", project_dir=self.project, review=False, max_calls=1)
        c = Conductor(config, specs={"first": {"id": "first"}, "second": {"id": "second"}}, health={}, verbose=False)
        c.workers = ["first", "second"]
        c.tasks, c.workflows = [self.task, t], journeys
        calls = []

        def build(spec, prompt, mode, cwd, **kwargs):
            calls.append(spec["id"])
            if spec["id"] == "second":
                self.assertIn("C1 stayed at 11", prompt)
                self.assertIn("REVISION #1", prompt)
            return agents.AgentRun(spec["id"], 0, "format implemented", "", 0.01)

        def verify(task, workspace):
            self.assertIn(task.verify, commands)
            if task.verify == self.task.verify and calls == ["first"]:
                return 1, "C1 stayed at 11", ""
            return 0, "passed", ""

        with mock.patch.object(agents, "run_agent", side_effect=build), mock.patch.object(c, "run_verify", side_effect=verify):
            c._run_task_safe(t)
        self.assertEqual((t.status, calls, c.calls), ("pending", ["first"], 1))
        self.assertEqual(t.attempts[0]["workflow_regressions"], [self.task.verify])
        resumed = Conductor(config, specs=c.specs, health={}, verbose=False)
        self.assertTrue(resumed.load_state())
        resumed.workers = c.workers
        current = resumed.tasks[1]
        with mock.patch.object(agents, "run_agent", side_effect=build), mock.patch.object(resumed, "run_verify", side_effect=verify):
            resumed._run_task_safe(current)
        self.assertEqual((current.status, calls, resumed.calls), ("done", ["first", "second"], 1))
        self.assertEqual([a["result"] for a in current.attempts], ["revise", "approved"])

    def test_followup_can_supply_initially_unavailable_interaction_check(self):
        flow = dict(self.workflow, check_task="")
        checks = [(self.task.verify, (0, "passed", ""))]
        row, = workflows.assess([flow], [self.task], checks, self.review)
        self.assertEqual(row["status"], "unverified")
        self.review["workflow_results"]["W1"]["check_task"] = "sheet"
        row, = workflows.assess([flow], [self.task], checks, self.review)
        self.assertEqual((row["status"], row["verify"]), ("met", self.task.verify))

    def test_workers_receive_scope_research_and_suite_examples(self):
        goal = "Make similar of Microsoft Word PPT Excel"
        prompt = build_worker_prompt(self.task, goal, self.project, "worker", "lead", [self.task],
                                     plan_summary="Use the existing Workbook API in src/model.js.",
                                     user_workflows=[self.workflow])
        for text in (self.workflow["scenario"], "src/model.js", "bold just 'draft'", "advance to Opening", "=A1+B1"):
            self.assertIn(text, prompt)
        self.assertFalse(workflows.editor_reference("Make a word counter for English text."))

    def test_integration_gap_requests_a_repair_and_survives_resume(self):
        config = TeamConfig(goal="Build a workbook", project_dir=self.project)
        conductor = Conductor(config, specs={"worker": {}}, health={}, verbose=False)
        conductor.workers = ["worker"]
        conductor.tasks, conductor.workflows = [self.task], [self.workflow]
        conductor.save_state()
        resumed = Conductor(config, specs={"worker": {}}, health={}, verbose=False)
        self.assertTrue(resumed.load_state())
        self.assertEqual(resumed.workflows, [self.workflow])
        resumed.workers = ["worker"]
        repair = {"tasks": [{"title": "Reconnect dependent cells", "assignee": "worker", "owns": ["grid.js"]}]}
        with mock.patch.object(resumed, "run_verify", return_value=(1, "C1 stayed at 11", "")), \
                mock.patch.object(resumed, "ask_lead", side_effect=[self.review, repair]) as calls, \
                mock.patch("omnicli.conductor.tool_report", return_value=(["node"], [])):
            result = resumed.final_check()
        self.assertFalse(result["complete"])
        self.assertEqual(result["followup_tasks"], repair["tasks"])
        self.assertIn("C1 stayed at 11", calls.call_args_list[1].args[1])

    def test_unavailable_final_review_does_not_certify_or_send_workers_to_rebuild(self):
        conductor = Conductor(TeamConfig(goal="Build a workbook", project_dir=self.project),
                              specs={}, health={}, verbose=False)
        conductor.tasks, conductor.workflows = [self.task], [self.workflow]
        with mock.patch.object(conductor, "run_verify", return_value=(0, "passed", "")), \
                mock.patch.object(conductor, "ask_lead", return_value=None) as ask:
            result = conductor.final_check()
        self.assertFalse(result["complete"])
        self.assertEqual(result["workflow_results"][0]["status"], "unverified")
        ask.assert_called_once()
