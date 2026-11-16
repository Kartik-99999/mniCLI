"""
Data models for OmniCLI: The Universal CLI Orchestrator.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class CliCategory(str, Enum):
    SCM = "scm"
    RUNTIME = "runtime"
    PACKAGE_MANAGER = "package_manager"
    CONTAINER = "container"
    BUILD = "build"
    LINTER_TEST = "linter_test"
    AI = "ai"
    NETWORK_UTILITY = "network_utility"
    SYSTEM = "system"
    OTHER = "other"


@dataclass
class CliTool:
    name: str
    path: str
    version: Optional[str] = None
    category: str = CliCategory.OTHER.value
    available: bool = True
    description: str = ""
    alternatives: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "version": self.version,
            "category": self.category,
            "available": self.available,
            "description": self.description,
            "alternatives": self.alternatives,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CliTool":
        return cls(
            name=data.get("name", ""),
            path=data.get("path", ""),
            version=data.get("version"),
            category=data.get("category", CliCategory.OTHER.value),
            available=data.get("available", True),
            description=data.get("description", ""),
            alternatives=data.get("alternatives", []),
        )


@dataclass
class Step:
    id: str
    name: str
    cli: str
    command: str
    cwd: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    condition: Optional[str] = None
    fallback_cli: Optional[str] = None
    fallback_command: Optional[str] = None
    capture_output: bool = True
    allow_failure: bool = False
    timeout_sec: int = 300
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "cli": self.cli,
            "command": self.command,
            "cwd": self.cwd,
            "env": self.env,
            "depends_on": self.depends_on,
            "condition": self.condition,
            "fallback_cli": self.fallback_cli,
            "fallback_command": self.fallback_command,
            "capture_output": self.capture_output,
            "allow_failure": self.allow_failure,
            "timeout_sec": self.timeout_sec,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Step":
        return cls(
            id=data.get("id", ""),
            name=data.get("name") or data.get("id", "step"),
            cli=data.get("cli", ""),
            command=data.get("command", ""),
            cwd=data.get("cwd"),
            env=data.get("env", {}),
            depends_on=data.get("depends_on", []),
            condition=data.get("condition"),
            fallback_cli=data.get("fallback_cli"),
            fallback_command=data.get("fallback_command"),
            capture_output=data.get("capture_output", True),
            allow_failure=data.get("allow_failure", False),
            timeout_sec=data.get("timeout_sec", 300),
            description=data.get("description"),
        )


@dataclass
class StepResult:
    step_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    success: bool
    skipped: bool = False
    error_message: Optional[str] = None
    command_executed: str = ""
    output_file: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_sec": round(self.duration_sec, 3),
            "success": self.success,
            "skipped": self.skipped,
            "error_message": self.error_message,
            "command_executed": self.command_executed,
            "output_file": self.output_file,
        }


@dataclass
class Pipeline:
    name: str
    description: str = ""
    steps: List[Step] = field(default_factory=list)
    variables: Dict[str, Any] = field(default_factory=dict)
    working_dir: Optional[str] = None
    on_failure: str = "abort"  # abort, continue, prompt
    parallel: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "steps": [s.to_dict() for s in self.steps],
            "variables": self.variables,
            "working_dir": self.working_dir,
            "on_failure": self.on_failure,
            "parallel": self.parallel,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Pipeline":
        steps_data = data.get("steps", [])
        steps = [Step.from_dict(s) for s in steps_data]
        return cls(
            name=data.get("name", "pipeline"),
            description=data.get("description", ""),
            steps=steps,
            variables=data.get("variables", {}),
            working_dir=data.get("working_dir"),
            on_failure=data.get("on_failure", "abort"),
            parallel=data.get("parallel", False),
        )


@dataclass
class PipelineResult:
    pipeline_name: str
    success: bool
    total_duration_sec: float
    step_results: Dict[str, StepResult] = field(default_factory=dict)
    executed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pipeline_name": self.pipeline_name,
            "success": self.success,
            "total_duration_sec": round(self.total_duration_sec, 3),
            "executed_count": self.executed_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "step_results": {k: v.to_dict() for k, v in self.step_results.items()},
        }
