"""
Comprehensive test suite for OmniCLI.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from omnicli.context import ExecutionContext
from omnicli.engine import ExecutionEngine
from omnicli.models import CliCategory, CliTool, Pipeline, Step, StepResult
from omnicli.planner import ProjectPlanner, b64_write_cmd
from omnicli.recipes import RecipeManager
from omnicli.scanner import CliScanner


class TestOmniCLIModels(unittest.TestCase):
    def test_clitool_serialization(self):
        tool = CliTool(
            name="git",
            path="/usr/bin/git",
            version="git version 2.50.1",
            category=CliCategory.SCM.value,
            available=True,
            description="Git SCM",
            alternatives=["gh"],
        )
        d = tool.to_dict()
        self.assertEqual(d["name"], "git")
        restored = CliTool.from_dict(d)
        self.assertEqual(restored.name, "git")
        self.assertEqual(restored.category, "scm")
        self.assertTrue(restored.available)

    def test_pipeline_serialization(self):
        pipe = Pipeline(
            name="test-pipe",
            description="Testing pipeline",
            steps=[
                Step(id="s1", name="Step 1", cli="echo", command="echo 'hi'"),
                Step(id="s2", name="Step 2", cli="echo", command="echo 'bye'", depends_on=["s1"]),
            ],
            variables={"env": "prod"},
        )
        d = pipe.to_dict()
        self.assertEqual(len(d["steps"]), 2)
        restored = Pipeline.from_dict(d)
        self.assertEqual(restored.name, "test-pipe")
        self.assertEqual(restored.steps[1].depends_on, ["s1"])


class TestOmniCLIContext(unittest.TestCase):
    def test_variable_interpolation(self):
        ctx = ExecutionContext(variables={"project_name": "superapp", "port": 8080})
        # Project var
        res = ctx.render_template("app: {{project.name}}, port: {{var.port}}")
        self.assertEqual(res, "app: superapp, port: 8080")

    def test_step_output_interpolation(self):
        ctx = ExecutionContext()
        step_res = StepResult(
            step_id="build_id",
            exit_code=0,
            stdout="release-998\n",
            stderr="",
            duration_sec=0.5,
            success=True,
        )
        ctx.register_step_result(step_res)

        # {prev}
        res_prev = ctx.render_template("Deploying {prev} now")
        self.assertEqual(res_prev, "Deploying release-998 now")

        # {{steps.build_id.stdout_trimmed}}
        res_ref = ctx.render_template("Image: tag-{{steps.build_id.stdout_trimmed}}")
        self.assertEqual(res_ref, "Image: tag-release-998")

    def test_conditions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ctx = ExecutionContext(working_dir=tmpdir)
            (Path(tmpdir) / "ready.flag").write_text("ok")

            self.assertTrue(ctx.evaluate_condition("file_exists('ready.flag')"))
            self.assertFalse(ctx.evaluate_condition("file_exists('missing.flag')"))
            self.assertTrue(ctx.evaluate_condition("not file_exists('missing.flag')"))


class TestOmniCLIScanner(unittest.TestCase):
    def test_scanner_detection(self):
        scanner = CliScanner()
        tools = scanner.scan()
        self.assertGreater(len(tools), 10)
        # git and python3 should be available
        self.assertTrue(scanner.is_available("git") or scanner.is_available("python3"))

    def test_scanner_alternatives(self):
        scanner = CliScanner()
        scanner.scan()
        # npm has yarn/pnpm alternatives
        alt = scanner.find_alternative("npm")
        # either None or one of yarn/pnpm/bun
        self.assertTrue(alt is None or isinstance(alt, str))


class TestOmniCLIPlanner(unittest.TestCase):
    def test_chain_notation_parsing(self):
        planner = ProjectPlanner()
        chain_cmd = "git rev-parse HEAD -> echo 'Hash: {prev}' -> docker build -t app:{prev} ."
        pipe = planner.parse_chain_notation(chain_cmd, "my-chain")
        self.assertEqual(len(pipe.steps), 3)
        self.assertEqual(pipe.steps[0].cli, "git")
        self.assertEqual(pipe.steps[1].cli, "echo")
        self.assertEqual(pipe.steps[2].cli, "docker")
        self.assertEqual(pipe.steps[1].depends_on, ["step_1_git"])

    def test_project_intent_planning(self):
        planner = ProjectPlanner()
        pipe = planner.plan_project_intent("setup python service with pytest and git", project_name="svc-test")
        self.assertEqual(pipe.name, "python-svc-test")
        step_ids = [s.id for s in pipe.steps]
        self.assertIn("mkdir", step_ids)
        self.assertIn("venv", step_ids)
        self.assertIn("git", step_ids)

    def test_b64_writer(self):
        cmd = b64_write_cmd("/tmp/omni_test_file.txt", "Sample Content 123\n")
        self.assertIn("base64", cmd)
        self.assertIn("python3", cmd)


class TestOmniCLIEngine(unittest.TestCase):
    def test_execution_engine_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = Pipeline(
                name="engine-test",
                steps=[
                    Step(id="s1", name="Create File", cli="python3", command="python3 -c \"open('out.txt', 'w').write('hello')\""),
                    Step(id="s2", name="Read File", cli="python3", command="python3 -c \"print(open('out.txt').read())\""),
                ],
                working_dir=tmpdir,
            )
            engine = ExecutionEngine(verbose=False)
            res = engine.run(pipeline, working_dir=tmpdir)
            self.assertTrue(res.success)
            self.assertEqual(res.executed_count, 2)
            self.assertEqual(res.step_results["s2"].stdout.strip(), "hello")

    def test_fallback_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = Pipeline(
                name="fallback-test",
                steps=[
                    Step(
                        id="f1",
                        name="Fallback Step",
                        cli="python3",
                        command="exit 1",
                        fallback_command="echo 'Recovered via fallback'",
                        allow_failure=True,
                    )
                ],
                working_dir=tmpdir,
            )
            engine = ExecutionEngine(verbose=False)
            res = engine.run(pipeline, working_dir=tmpdir)
            self.assertTrue(res.step_results["f1"].success)
            self.assertIn("Recovered", res.step_results["f1"].stdout)

    def test_parallel_execution(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pipeline = Pipeline(
                name="parallel-test",
                steps=[
                    Step(id="p1", name="Task 1", cli="echo", command="echo 't1'"),
                    Step(id="p2", name="Task 2", cli="echo", command="echo 't2'"),
                    Step(id="p3", name="Merge", cli="echo", command="echo 'done'", depends_on=["p1", "p2"]),
                ],
                parallel=True,
                working_dir=tmpdir,
            )
            engine = ExecutionEngine(verbose=False)
            res = engine.run(pipeline, working_dir=tmpdir, parallel=True)
            self.assertTrue(res.success)
            self.assertEqual(res.executed_count, 3)


class TestOmniCLIRecipes(unittest.TestCase):
    def test_recipe_manager(self):
        rm = RecipeManager()
        recipes = rm.list_recipes()
        self.assertGreater(len(recipes), 0)
        pipe = rm.get_pipeline("static-landing-page")
        self.assertIsNotNone(pipe)
        self.assertEqual(pipe.name, "static-landing-page")


if __name__ == "__main__":
    unittest.main()
