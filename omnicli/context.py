"""
Execution context and variable interpolation engine for OmniCLI.
Passes data, stdout, exit codes, and environment variables across ordered CLI steps.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from omnicli.models import StepResult


class ExecutionContext:
    """Stores variables, step results, and evaluates dynamic template substitutions."""

    def __init__(self, working_dir: Optional[str] = None, variables: Optional[Dict[str, Any]] = None):
        self.working_dir = Path(working_dir or os.getcwd()).resolve()
        self.variables: Dict[str, Any] = dict(variables or {})
        self.step_results: Dict[str, StepResult] = {}
        self.last_step_id: Optional[str] = None

    def register_step_result(self, result: StepResult):
        self.step_results[result.step_id] = result
        self.last_step_id = result.step_id

    def set_var(self, key: str, value: Any):
        self.variables[key] = value

    def get_var(self, key: str, default: Any = None) -> Any:
        return self.variables.get(key, default)

    def render_template(self, text: str) -> str:
        """Interpolates {{...}} placeholders in text with live context values."""
        if not text:
            return ""

        # Special shorthand: {prev} or {{prev}} -> last step stdout trimmed
        if self.last_step_id and self.last_step_id in self.step_results:
            last_out = self.step_results[self.last_step_id].stdout.strip()
            text = text.replace("{prev}", last_out).replace("{{prev}}", last_out)

        # Pattern for {{ expression }}
        pattern = re.compile(r"\{\{\s*([a-zA-Z0-9_\.\-\[\]]+)\s*\}\}")

        def replace_match(match):
            expr = match.group(1).strip()
            val = self._resolve_expression(expr)
            return str(val) if val is not None else match.group(0)

        rendered = pattern.sub(replace_match, text)

        # Also support $VAR / ${VAR} from env if not already matched
        return os.path.expandvars(rendered)

    def _resolve_expression(self, expr: str) -> Optional[Any]:
        # 1. Project variables
        if expr == "project.dir" or expr == "cwd":
            return str(self.working_dir)
        if expr == "project.name":
            return self.variables.get("project_name", self.working_dir.name)

        # 2. Environment variables: env.VAR_NAME
        if expr.startswith("env."):
            env_key = expr[4:]
            return os.environ.get(env_key, "")

        # 3. Direct variables: var.NAME or just NAME
        if expr.startswith("var."):
            var_key = expr[4:]
            return self.variables.get(var_key, "")
        if expr in self.variables:
            return self.variables[expr]

        # 4. Step outputs: steps.<step_id>.<attr>
        if expr.startswith("steps."):
            parts = expr.split(".", 2)
            if len(parts) >= 2:
                step_id = parts[1]
                attr = parts[2] if len(parts) > 2 else "stdout"

                if step_id not in self.step_results:
                    return ""

                res = self.step_results[step_id]

                if attr == "stdout":
                    return res.stdout
                if attr == "stdout_trimmed" or attr == "output":
                    return res.stdout.strip()
                if attr in ["file", "output_file", "path"]:
                    return res.output_file or ""
                if attr == "stderr":
                    return res.stderr
                if attr == "exit_code":
                    return res.exit_code
                if attr == "success":
                    return res.success
                if attr == "line0" or attr == "first_line":
                    lines = res.stdout.strip().split("\n")
                    return lines[0].strip() if lines else ""

                # JSON parsing: steps.<step_id>.json.<field>
                if attr.startswith("json."):
                    json_key = attr[5:]
                    try:
                        data = json.loads(res.stdout)
                        return self._get_nested_key(data, json_key)
                    except Exception:
                        return ""

        return None

    def _get_nested_key(self, data: Any, key_path: str) -> Any:
        keys = key_path.split(".")
        current = data
        for k in keys:
            if isinstance(current, dict):
                current = current.get(k)
            else:
                return ""
        return current

    def evaluate_condition(self, condition: Optional[str]) -> bool:
        """Evaluates simple conditions for steps."""
        if not condition:
            return True

        cond = condition.strip()

        # Negation
        is_negated = False
        if cond.startswith("not "):
            is_negated = True
            cond = cond[4:].strip()

        result = True

        # file_exists('path')
        m_file = re.match(r"file_exists\(['\"](.*)['\"]\)", cond)
        if m_file:
            path = self.working_dir / m_file.group(1)
            result = path.is_file()

        # dir_exists('path')
        m_dir = re.match(r"dir_exists\(['\"](.*)['\"]\)", cond)
        if m_dir:
            path = self.working_dir / m_dir.group(1)
            result = path.is_dir()

        # step_succeeded('step_id')
        m_step = re.match(r"step_succeeded\(['\"](.*)['\"]\)", cond)
        if m_step:
            sid = m_step.group(1)
            result = bool(self.step_results.get(sid) and self.step_results[sid].success)

        return (not result) if is_negated else result
