"""
Pipeline Execution and Orchestration Engine for OmniCLI.
Runs multi-CLI steps in order, passing context, streaming outputs,
and managing fallbacks and error handling.
"""

import concurrent.futures
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from omnicli.context import ExecutionContext
from omnicli.models import Pipeline, PipelineResult, Step, StepResult
from omnicli.scanner import CliScanner
from omnicli.ui import (
    BADGE_FAILED,
    BADGE_RUNNING,
    BADGE_SKIP,
    BADGE_SUCCESS,
    BADGE_WARN,
    c_bold,
    c_cyan,
    c_dim,
    c_green,
    c_magenta,
    c_red,
    c_yellow,
    print_panel,
    print_step_command,
    print_step_header,
    print_step_output_line,
    print_table,
)


class ExecutionEngine:
    """Orchestrates and executes multi-CLI pipelines."""

    def __init__(
        self,
        scanner: Optional[CliScanner] = None,
        verbose: bool = True,
        on_step_start: Optional[Callable[[Step, int, int], None]] = None,
        on_step_finish: Optional[Callable[[StepResult], None]] = None,
    ):
        self.scanner = scanner or CliScanner()
        self.verbose = verbose
        self.on_step_start = on_step_start
        self.on_step_finish = on_step_finish
        self._print_lock = threading.Lock()

    def validate_pipeline(self, pipeline: Pipeline) -> Tuple[bool, List[str]]:
        """Pre-flight check: verifies that required CLIs are available or have alternatives."""
        errors = []
        for step in pipeline.steps:
            cli = step.cli.strip().split()[0] if step.cli else ""
            if not cli:
                # Derive from command
                parts = shlex.split(step.command) if step.command else []
                cli = parts[0] if parts else ""

            if not cli:
                errors.append(f"Step '{step.id}' has no CLI binary or command specified.")
                continue

            # Skip pre-flight check if CLI is a local relative or generated path (e.g., .venv/bin/pip or ./bin/app)
            if cli.startswith(".") or "/" in cli:
                continue

            if not self.scanner.is_available(cli):
                alt = self.scanner.find_alternative(cli)
                if alt:
                    if self.verbose:
                        print(f"  {BADGE_WARN} CLI '{cli}' not found, will attempt alternative '{alt}' for step '{step.id}'")
                elif not step.allow_failure and not step.fallback_command:
                    errors.append(f"Required CLI tool '{cli}' for step '{step.id}' is not installed on this device.")

        return len(errors) == 0, errors

    def execute_step(
        self, step: Step, context: ExecutionContext, index: int = 1, total: int = 1
    ) -> StepResult:
        """Executes a single CLI step."""
        # 1. Condition check
        if step.condition and not context.evaluate_condition(step.condition):
            if self.verbose:
                print_step_header(index, total, step.id, step.cli, step.description or step.name)
                print(f"  {BADGE_SKIP} Condition not met ({step.condition}), skipping step.")
            res = StepResult(
                step_id=step.id,
                exit_code=0,
                stdout="",
                stderr="",
                duration_sec=0.0,
                success=True,
                skipped=True,
                command_executed="",
            )
            context.register_step_result(res)
            return res

        # 2. Resolve working directory
        cwd = Path(step.cwd).resolve() if step.cwd else context.working_dir
        cwd.mkdir(parents=True, exist_ok=True)

        # 3. Resolve command template
        cmd_str = context.render_template(step.command)

        # 4. CLI check & fallback substitution
        cli_name = step.cli or (shlex.split(cmd_str)[0] if cmd_str else "")
        active_cmd = cmd_str

        if cli_name and not self.scanner.is_available(cli_name):
            alt = self.scanner.find_alternative(cli_name)
            if alt and step.fallback_command:
                active_cmd = context.render_template(step.fallback_command)
                cli_name = alt
            elif alt:
                # Replace binary name
                parts = active_cmd.split(None, 1)
                active_cmd = f"{alt} {parts[1]}" if len(parts) > 1 else alt
                cli_name = alt
            elif step.fallback_command:
                active_cmd = context.render_template(step.fallback_command)

        if self.verbose:
            print_step_header(index, total, step.id, cli_name, step.description or step.name)
            print_step_command(active_cmd)

        if self.on_step_start:
            self.on_step_start(step, index, total)

        # 5. Environment
        step_env = os.environ.copy()
        if step.env:
            for k, v in step.env.items():
                step_env[k] = context.render_template(str(v))

        # 6. Execute process
        start_time = time.time()
        stdout_lines: List[str] = []
        stderr_lines: List[str] = []

        try:
            process = subprocess.Popen(
                active_cmd,
                shell=True,
                cwd=str(cwd),
                env=step_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            # Stream output live
            if process.stdout:
                for line in iter(process.stdout.readline, ""):
                    stdout_lines.append(line)
                    if self.verbose:
                        with self._print_lock:
                            print_step_output_line(cli_name, line)

            # Wait for completion
            _, err_out = process.communicate(timeout=step.timeout_sec)
            if err_out:
                stderr_lines.append(err_out)
                if self.verbose:
                    with self._print_lock:
                        for err_line in err_out.strip().split("\n"):
                            if err_line:
                                print_step_output_line(cli_name, f"{c_red(err_line)}")

            duration = time.time() - start_time
            exit_code = process.returncode
            success = (exit_code == 0)

            # Check if fallback needed on failure
            if not success and step.fallback_command and active_cmd != step.fallback_command:
                if self.verbose:
                    print(f"  {BADGE_WARN} Step failed with exit code {exit_code}. Attempting fallback...")
                fb_cmd = context.render_template(step.fallback_command)
                return self._run_command_fallback(step, fb_cmd, cwd, step_env, cli_name, context, start_time)

            badge = BADGE_SUCCESS if success else BADGE_FAILED
            status_msg = f"completed in {duration:.2f}s" if success else f"failed with exit code {exit_code} ({duration:.2f}s)"
            if self.verbose:
                print(f"  {badge} {status_msg}")

            # Save step output to file for inter-CLI file references
            out_file_path = None
            try:
                runs_dir = Path.home() / ".omnicli" / "runs"
                runs_dir.mkdir(parents=True, exist_ok=True)
                step_file = runs_dir / f"{step.id}.stdout"
                step_file.write_text("".join(stdout_lines), encoding="utf-8")
                out_file_path = str(step_file)
            except Exception:
                pass

            result = StepResult(
                step_id=step.id,
                exit_code=exit_code,
                stdout="".join(stdout_lines),
                stderr="".join(stderr_lines),
                duration_sec=duration,
                success=success,
                skipped=False,
                error_message=None if success else f"Command exited with code {exit_code}",
                command_executed=active_cmd,
                output_file=out_file_path,
            )

        except subprocess.TimeoutExpired:
            process.kill()
            duration = time.time() - start_time
            if self.verbose:
                print(f"  {BADGE_FAILED} Timed out after {step.timeout_sec} seconds")
            result = StepResult(
                step_id=step.id,
                exit_code=-1,
                stdout="".join(stdout_lines),
                stderr="Timeout expired",
                duration_sec=duration,
                success=False,
                skipped=False,
                error_message=f"Timeout after {step.timeout_sec}s",
                command_executed=active_cmd,
            )

        except Exception as ex:
            duration = time.time() - start_time
            if self.verbose:
                print(f"  {BADGE_FAILED} Execution error: {ex}")
            result = StepResult(
                step_id=step.id,
                exit_code=-1,
                stdout="".join(stdout_lines),
                stderr=str(ex),
                duration_sec=duration,
                success=False,
                skipped=False,
                error_message=str(ex),
                command_executed=active_cmd,
            )

        context.register_step_result(result)
        if self.on_step_finish:
            self.on_step_finish(result)

        return result

    def _run_command_fallback(
        self,
        step: Step,
        cmd: str,
        cwd: Path,
        env: Dict[str, str],
        cli_name: str,
        context: ExecutionContext,
        start_time: float,
    ) -> StepResult:
        """Executes a fallback command."""
        if self.verbose:
            print_step_command(cmd)

        try:
            res = subprocess.run(
                cmd,
                shell=True,
                cwd=str(cwd),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=step.timeout_sec,
            )
            duration = time.time() - start_time
            success = (res.returncode == 0)
            if self.verbose:
                badge = BADGE_SUCCESS if success else BADGE_FAILED
                print(f"  {badge} Fallback completed in {duration:.2f}s (code {res.returncode})")

            result = StepResult(
                step_id=step.id,
                exit_code=res.returncode,
                stdout=res.stdout,
                stderr=res.stderr,
                duration_sec=duration,
                success=success,
                skipped=False,
                command_executed=cmd,
            )
        except Exception as ex:
            duration = time.time() - start_time
            result = StepResult(
                step_id=step.id,
                exit_code=-1,
                stdout="",
                stderr=str(ex),
                duration_sec=duration,
                success=False,
                skipped=False,
                error_message=f"Fallback failed: {ex}",
                command_executed=cmd,
            )

        context.register_step_result(result)
        return result

    def _build_execution_layers(self, steps: List[Step]) -> List[List[Step]]:
        """Groups steps into concurrent topological execution layers based on dependencies."""
        step_map = {s.id: s for s in steps}
        completed: Set[str] = set()
        remaining = list(steps)
        layers: List[List[Step]] = []

        while remaining:
            current_layer: List[Step] = []
            for step in remaining:
                if all(dep in completed for dep in step.depends_on):
                    current_layer.append(step)

            if not current_layer:
                # If there are unresolved or circular dependencies, fallback remaining sequentially
                for step in remaining:
                    layers.append([step])
                break

            layers.append(current_layer)
            for s in current_layer:
                completed.add(s.id)
                remaining.remove(s)

        return layers

    def run(
        self,
        pipeline: Pipeline,
        working_dir: Optional[str] = None,
        parallel: Optional[bool] = None,
    ) -> PipelineResult:
        """Runs an entire pipeline from start to finish with sequential or parallel execution."""
        target_dir = working_dir or pipeline.working_dir or os.getcwd()
        context = ExecutionContext(working_dir=target_dir, variables=pipeline.variables)

        # Pre-flight check
        valid, errors = self.validate_pipeline(pipeline)
        if not valid:
            print(f"\n{BADGE_FAILED} Pipeline Pre-flight Validation Failed:")
            for err in errors:
                print(f"  • {c_red(err)}")
            return PipelineResult(
                pipeline_name=pipeline.name,
                success=False,
                total_duration_sec=0.0,
                failed_count=len(errors),
            )

        start_time = time.time()
        step_results: Dict[str, StepResult] = {}
        executed = 0
        failed = 0
        skipped = 0

        total_steps = len(pipeline.steps)
        overall_success = True
        is_parallel = parallel if parallel is not None else pipeline.parallel

        if is_parallel:
            layers = self._build_execution_layers(pipeline.steps)
            if self.verbose:
                print(f"  {c_cyan('⚡ Concurrent Mode Activated')} ({len(layers)} execution layers)")

            step_counter = 0
            for layer_idx, layer in enumerate(layers, start=1):
                if len(layer) == 1:
                    step = layer[0]
                    step_counter += 1
                    res = self.execute_step(step, context, index=step_counter, total=total_steps)
                    step_results[step.id] = res
                    if res.skipped:
                        skipped += 1
                    elif res.success:
                        executed += 1
                    else:
                        failed += 1
                        if not step.allow_failure:
                            overall_success = False
                            if pipeline.on_failure == "abort":
                                break
                else:
                    # Execute multiple steps concurrently in thread pool
                    if self.verbose:
                        layer_names = ", ".join(f"<{s.cli}>" for s in layer)
                        print(f"\n{c_bold(c_magenta('--- Concurrent Layer:'))} {layer_names} {c_magenta('---')}")

                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(layer), 8)) as executor:
                        future_to_step = {}
                        for idx, step in enumerate(layer, start=step_counter + 1):
                            f = executor.submit(self.execute_step, step, context, idx, total_steps)
                            future_to_step[f] = step
                        step_counter += len(layer)

                        for future in concurrent.futures.as_completed(future_to_step):
                            res = future.result()
                            step_results[res.step_id] = res
                            if res.skipped:
                                skipped += 1
                            elif res.success:
                                executed += 1
                            else:
                                failed += 1
                                if not future_to_step[future].allow_failure:
                                    overall_success = False

                if not overall_success and pipeline.on_failure == "abort":
                    if self.verbose:
                        print(f"\n{BADGE_FAILED} Aborting pipeline due to failure in layer {layer_idx}.")
                    break
        else:
            for idx, step in enumerate(pipeline.steps, start=1):
                res = self.execute_step(step, context, index=idx, total=total_steps)
                step_results[step.id] = res

                if res.skipped:
                    skipped += 1
                elif res.success:
                    executed += 1
                else:
                    failed += 1
                    if not step.allow_failure:
                        overall_success = False
                        if pipeline.on_failure == "abort":
                            if self.verbose:
                                print(f"\n{BADGE_FAILED} Aborting pipeline due to step '{step.id}' failure.")
                            break

        total_duration = time.time() - start_time

        # Print summary
        if self.verbose:
            self._print_pipeline_summary(pipeline.name, overall_success, total_duration, step_results)

        return PipelineResult(
            pipeline_name=pipeline.name,
            success=overall_success,
            total_duration_sec=total_duration,
            step_results=step_results,
            executed_count=executed,
            failed_count=failed,
            skipped_count=skipped,
        )

    def _print_pipeline_summary(
        self, name: str, success: bool, duration: float, results: Dict[str, StepResult]
    ):
        status_text = c_green("COMPLETED SUCCESSFULLY") if success else c_red("FAILED")
        print(f"\n{c_bold('Pipeline Summary:')} {c_cyan(name)} ─ {status_text} in {duration:.2f}s\n")

        headers = ["Step ID", "Status", "Exit Code", "Duration", "Executed Command"]
        rows = []
        for sid, res in results.items():
            if res.skipped:
                st = c_dim("SKIPPED")
            elif res.success:
                st = c_green("PASSED")
            else:
                st = c_red("FAILED")

            cmd_short = res.command_executed
            if len(cmd_short) > 40:
                cmd_short = cmd_short[:37] + "..."

            rows.append([
                sid,
                st,
                str(res.exit_code),
                f"{res.duration_sec:.2f}s",
                cmd_short or "-",
            ])

        print_table(headers, rows)
