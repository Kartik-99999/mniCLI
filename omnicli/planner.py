"""
Single-Command Project Planner and Intent Composer for OmniCLI.
Translates high-level natural goals or chained commands into an ordered,
dependency-aware multi-CLI execution pipeline adapted to device capabilities.
"""

import base64
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from omnicli.models import Pipeline, Step
from omnicli.scanner import CliScanner


def b64_write_cmd(filepath: str, content: str) -> str:
    """Generates a shell-safe python one-liner to write file contents via base64."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    return (
        f"python3 -c \"import base64, pathlib; "
        f"p = pathlib.Path(r'{filepath}'); "
        f"p.parent.mkdir(parents=True, exist_ok=True); "
        f"p.write_bytes(base64.b64decode('{encoded}'))\""
    )


class ProjectPlanner:
    """Intelligently plans ordered CLI execution graphs from a single command or intent."""

    def __init__(self, scanner: Optional[CliScanner] = None):
        self.scanner = scanner or CliScanner()

    def parse_chain_notation(self, chain_str: str, pipeline_name: str = "custom-chain") -> Pipeline:
        """Parses inline chaining syntax: 'cli1 arg -> cli2 {prev} -> cli3'."""
        delimiter = " -> " if " -> " in chain_str else "->"
        raw_steps = [s.strip() for s in chain_str.split(delimiter) if s.strip()]

        steps: List[Step] = []
        for idx, cmd in enumerate(raw_steps, start=1):
            parts = cmd.split(None, 1)
            cli = parts[0] if parts else ""
            step_id = f"step_{idx}_{cli}"
            steps.append(
                Step(
                    id=step_id,
                    name=f"Run {cli}",
                    cli=cli,
                    command=cmd,
                    description=f"Step {idx}: Execute '{cmd}'",
                    depends_on=[steps[idx - 2].id] if idx > 1 else [],
                )
            )

        return Pipeline(
            name=pipeline_name,
            description=f"Chained execution of {len(steps)} CLI commands",
            steps=steps,
        )

    def plan_project_intent(
        self, intent: str, project_name: Optional[str] = None, target_dir: Optional[str] = None
    ) -> Pipeline:
        """Analyzes a high-level project command/intent and builds an ordered multi-CLI pipeline."""
        intent_lower = intent.lower()

        # If user passed explicit chain arrow notation
        if "->" in intent:
            return self.parse_chain_notation(intent, pipeline_name=project_name or "cli-chain")

        # Determine project name
        if not project_name:
            m = re.search(r"(?:name|project|called|app)\s+([a-zA-Z0-9_\-]+)", intent, re.IGNORECASE)
            project_name = m.group(1) if m else "my-omni-project"

        base_dir = Path(target_dir or os.getcwd()).resolve()
        proj_dir = base_dir / project_name if base_dir.name != project_name else base_dir

        # Identify requested stacks
        has_react = any(w in intent_lower for w in ["react", "vite", "frontend", "next", "ui"])
        has_python = any(w in intent_lower for w in ["python", "fastapi", "flask", "django", "pytest", "backend"])
        has_fullstack = ("fullstack" in intent_lower) or (has_react and has_python)
        has_docker = any(w in intent_lower for w in ["docker", "container", "containerize", "image"])
        has_git = any(w in intent_lower for w in ["git", "repo", "commit", "version control", "github"])
        has_node = any(w in intent_lower for w in ["node", "express", "npm", "typescript", "ts", "javascript"])

        # Select best available package manager
        node_pkg = "pnpm" if self.scanner.is_available("pnpm") else ("yarn" if self.scanner.is_available("yarn") else "npm")
        python_bin = "python3" if self.scanner.is_available("python3") else "python"
        git_avail = self.scanner.is_available("git")
        docker_avail = self.scanner.is_available("docker")

        # ==========================================
        # Scenario 1: Fullstack (React + FastAPI/Python)
        # ==========================================
        if has_fullstack:
            return self._plan_fullstack(
                project_name=project_name,
                proj_dir=proj_dir,
                node_pkg=node_pkg,
                python_bin=python_bin,
                include_git=has_git or git_avail,
                include_docker=has_docker or docker_avail,
            )

        # ==========================================
        # Scenario 2: Python / FastAPI Service
        # ==========================================
        if has_python:
            return self._plan_python_project(
                project_name=project_name,
                proj_dir=proj_dir,
                python_bin=python_bin,
                is_fastapi="fastapi" in intent_lower or "api" in intent_lower,
                include_git=has_git or git_avail,
                include_docker=has_docker or docker_avail,
            )

        # ==========================================
        # Scenario 3: React / Vite / Modern Web
        # ==========================================
        if has_react or has_node:
            return self._plan_node_react_project(
                project_name=project_name,
                proj_dir=proj_dir,
                node_pkg=node_pkg,
                has_tailwind="tailwind" in intent_lower,
                include_git=has_git or git_avail,
                include_docker=has_docker or docker_avail,
            )

        # ==========================================
        # Scenario 4: Dockerization of existing or new directory
        # ==========================================
        if has_docker:
            return self._plan_docker_project(
                project_name=project_name,
                proj_dir=proj_dir,
                include_git=has_git or git_avail,
            )

        # ==========================================
        # Default / Universal Scaffold Pipeline
        # ==========================================
        return self._plan_universal_project(
            project_name=project_name,
            proj_dir=proj_dir,
            intent=intent,
            include_git=has_git or git_avail,
        )

    def _plan_fullstack(
        self,
        project_name: str,
        proj_dir: Path,
        node_pkg: str,
        python_bin: str,
        include_git: bool,
        include_docker: bool,
    ) -> Pipeline:
        steps = []
        p_str = str(proj_dir)

        # 1. Directory creation
        steps.append(
            Step(
                id="init_dirs",
                name="Create Project Structure",
                cli="mkdir",
                command=f"mkdir -p '{p_str}/backend' '{p_str}/frontend'",
                description="Scaffold root, backend and frontend directories",
            )
        )

        # 2. Python backend virtualenv
        steps.append(
            Step(
                id="python_venv",
                name="Setup Python Virtual Environment",
                cli=python_bin,
                command=f"{python_bin} -m venv '{p_str}/backend/.venv'",
                description=f"Create virtual environment using {python_bin}",
                depends_on=["init_dirs"],
            )
        )

        # 3. Backend scaffolding (main.py + requirements.txt)
        backend_code = (
            "from fastapi import FastAPI\n\n"
            "app = FastAPI(title='OmniCLI Project')\n\n"
            "@app.get('/')\n"
            "def read_root():\n"
            "    return {'status': 'healthy', 'service': 'backend', 'managed_by': 'omnicli'}\n\n"
            "@app.get('/api/data')\n"
            "def get_data():\n"
            "    return {'items': ['Item 1', 'Item 2', 'Item 3'], 'total': 3}\n"
        )
        reqs = "fastapi>=0.110.0\nuvicorn>=0.28.0\npytest>=8.0.0\n"

        cmd_reqs = b64_write_cmd(f"{p_str}/backend/requirements.txt", reqs)
        cmd_app = b64_write_cmd(f"{p_str}/backend/main.py", backend_code)

        steps.append(
            Step(
                id="backend_scaffold",
                name="Generate FastAPI Application Code",
                cli="python3",
                command=f"{cmd_reqs} && {cmd_app}",
                description="Create FastAPI main.py and requirements.txt",
                depends_on=["python_venv"],
            )
        )

        # 4. Install backend dependencies
        steps.append(
            Step(
                id="pip_install",
                name="Install Python Backend Dependencies",
                cli=f"{p_str}/backend/.venv/bin/pip",
                command=f"'{p_str}/backend/.venv/bin/pip' install -r '{p_str}/backend/requirements.txt'",
                description="Install FastAPI, Uvicorn, and Pytest via pip",
                depends_on=["backend_scaffold"],
                allow_failure=True,
            )
        )

        # 5. Frontend Vite React setup
        steps.append(
            Step(
                id="frontend_scaffold",
                name="Initialize React Vite Frontend",
                cli=node_pkg,
                cwd=p_str,
                command="npx --yes create-vite@latest frontend --template react",
                description="Initialize React single-page app via Vite",
                depends_on=["init_dirs"],
                allow_failure=True,
            )
        )

        # 6. Frontend dependencies install
        steps.append(
            Step(
                id="frontend_install",
                name="Install Frontend Dependencies",
                cli=node_pkg,
                cwd=f"{p_str}/frontend",
                command=f"{node_pkg} install",
                description=f"Run {node_pkg} install for frontend packages",
                depends_on=["frontend_scaffold"],
                allow_failure=True,
            )
        )

        # 7. Git Version Control
        if include_git:
            gitignore_content = ".venv\nnode_modules\n__pycache__\n.DS_Store\ndist\nbuild\n.env\n"
            cmd_gi = b64_write_cmd(f"{p_str}/.gitignore", gitignore_content)
            steps.append(
                Step(
                    id="git_init",
                    name="Initialize Git Repository",
                    cli="git",
                    cwd=p_str,
                    command=f"git init && {cmd_gi} && git add . && git commit -m 'Initial fullstack project generated by OmniCLI'",
                    description="Initialize Git repo, create .gitignore, and commit initial files",
                    depends_on=["backend_scaffold", "frontend_scaffold"],
                    allow_failure=True,
                )
            )

        # 8. Docker Compose & Dockerfiles
        if include_docker:
            docker_compose = (
                "version: '3.8'\n"
                "services:\n"
                "  backend:\n"
                "    build: ./backend\n"
                "    ports:\n"
                "      - '8000:8000'\n"
                "  frontend:\n"
                "    build: ./frontend\n"
                "    ports:\n"
                "      - '3000:3000'\n"
            )
            backend_dockerfile = (
                "FROM python:3.11-slim\n"
                "WORKDIR /app\n"
                "COPY requirements.txt .\n"
                "RUN pip install --no-cache-dir -r requirements.txt\n"
                "COPY . .\n"
                "CMD [\"uvicorn\", \"main:app\", \"--host\", \"0.0.0.0\", \"--port\", \"8000\"]\n"
            )
            cmd_dc = b64_write_cmd(f"{p_str}/docker-compose.yml", docker_compose)
            cmd_df = b64_write_cmd(f"{p_str}/backend/Dockerfile", backend_dockerfile)
            steps.append(
                Step(
                    id="docker_setup",
                    name="Generate Docker Infrastructure",
                    cli="python3",
                    command=f"{cmd_dc} && {cmd_df}",
                    description="Create Docker Compose and backend Dockerfile",
                    depends_on=["init_dirs"],
                )
            )

        return Pipeline(
            name=f"fullstack-{project_name}",
            description=f"Fullstack React + FastAPI Project Orchestration ({project_name})",
            steps=steps,
            working_dir=p_str,
        )

    def _plan_python_project(
        self,
        project_name: str,
        proj_dir: Path,
        python_bin: str,
        is_fastapi: bool,
        include_git: bool,
        include_docker: bool,
    ) -> Pipeline:
        p_str = str(proj_dir)
        steps = []

        # 1. Directory creation
        steps.append(
            Step(
                id="mkdir",
                name="Create Project Directory",
                cli="mkdir",
                command=f"mkdir -p '{p_str}/src' '{p_str}/tests'",
                description="Scaffold src/ and tests/ directories",
            )
        )

        # 2. Virtual environment
        steps.append(
            Step(
                id="venv",
                name="Setup Virtual Environment",
                cli=python_bin,
                command=f"{python_bin} -m venv '{p_str}/.venv'",
                description=f"Initialize isolated virtualenv with {python_bin}",
                depends_on=["mkdir"],
            )
        )

        # 3. Source code generation
        if is_fastapi:
            main_code = (
                "from fastapi import FastAPI\n\n"
                "app = FastAPI(title='API Service')\n\n"
                "@app.get('/health')\n"
                "def health():\n"
                "    return {'status': 'healthy'}\n"
            )
            reqs = "fastapi>=0.110.0\nuvicorn>=0.28.0\npytest>=8.0.0\nhttpx>=0.27.0\n"
            test_code = (
                "from fastapi.testclient import TestClient\n"
                "from src.main import app\n\n"
                "client = TestClient(app)\n\n"
                "def test_health():\n"
                "    response = client.get('/health')\n"
                "    assert response.status_code == 200\n"
                "    assert response.json() == {'status': 'healthy'}\n"
            )
        else:
            main_code = (
                "def calculate(a: int, b: int) -> int:\n"
                "    return a + b\n\n"
                "if __name__ == '__main__':\n"
                "    print(f'Result: {calculate(5, 7)}')\n"
            )
            reqs = "pytest>=8.0.0\n"
            test_code = (
                "from src.main import calculate\n\n"
                "def test_calculate():\n"
                "    assert calculate(2, 3) == 5\n"
            )

        cmd_init1 = b64_write_cmd(f"{p_str}/src/__init__.py", "")
        cmd_main = b64_write_cmd(f"{p_str}/src/main.py", main_code)
        cmd_init2 = b64_write_cmd(f"{p_str}/tests/__init__.py", "")
        cmd_test = b64_write_cmd(f"{p_str}/tests/test_main.py", test_code)
        cmd_reqs = b64_write_cmd(f"{p_str}/requirements.txt", reqs)

        steps.append(
            Step(
                id="scaffold_code",
                name="Generate Source and Test Files",
                cli="python3",
                command=f"{cmd_init1} && {cmd_main} && {cmd_init2} && {cmd_test} && {cmd_reqs}",
                description="Write application entrypoint, tests, and requirements.txt",
                depends_on=["mkdir"],
            )
        )

        # 4. Install dependencies via pip
        steps.append(
            Step(
                id="pip_install",
                name="Install Requirements via Pip",
                cli=f"{p_str}/.venv/bin/pip",
                command=f"'{p_str}/.venv/bin/pip' install -r '{p_str}/requirements.txt'",
                description="Install dependencies in local virtualenv",
                depends_on=["venv", "scaffold_code"],
                allow_failure=True,
            )
        )

        # 5. Run tests via pytest
        steps.append(
            Step(
                id="pytest",
                name="Execute Automated Unit Tests",
                cli=f"{p_str}/.venv/bin/pytest",
                cwd=p_str,
                command=f"'{p_str}/.venv/bin/pytest' tests/",
                fallback_command="python3 -m unittest discover tests/",
                description="Run pytest suite to verify project code",
                depends_on=["pip_install"],
                allow_failure=True,
            )
        )

        # 6. Docker container setup
        if include_docker:
            dockerfile = (
                "FROM python:3.11-slim\n"
                "WORKDIR /app\n"
                "COPY requirements.txt .\n"
                "RUN pip install --no-cache-dir -r requirements.txt\n"
                "COPY . .\n"
                "CMD [\"python\", \"src/main.py\"]\n"
            )
            cmd_df = b64_write_cmd(f"{p_str}/Dockerfile", dockerfile)
            steps.append(
                Step(
                    id="dockerfile",
                    name="Generate Dockerfile",
                    cli="python3",
                    command=cmd_df,
                    description="Generate container Dockerfile",
                    depends_on=["mkdir"],
                )
            )

        # 7. Git repo
        if include_git:
            gitignore = ".venv\n__pycache__\n.pytest_cache\n*.pyc\n.DS_Store\n"
            cmd_gi = b64_write_cmd(f"{p_str}/.gitignore", gitignore)
            steps.append(
                Step(
                    id="git",
                    name="Git Initialization & First Commit",
                    cli="git",
                    cwd=p_str,
                    command=f"git init && {cmd_gi} && git add . && git commit -m 'Initial project setup via OmniCLI'",
                    description="Initialize git repo and make first commit",
                    depends_on=["scaffold_code"],
                    allow_failure=True,
                )
            )

        return Pipeline(
            name=f"python-{project_name}",
            description=f"Python Application Lifecycle Pipeline ({project_name})",
            steps=steps,
            working_dir=p_str,
        )

    def _plan_node_react_project(
        self,
        project_name: str,
        proj_dir: Path,
        node_pkg: str,
        has_tailwind: bool,
        include_git: bool,
        include_docker: bool,
    ) -> Pipeline:
        p_str = str(proj_dir)
        parent_dir = str(proj_dir.parent)
        base_name = proj_dir.name
        steps = []

        # 1. Scaffold React Vite
        steps.append(
            Step(
                id="create_app",
                name="Scaffold React Vite Application",
                cli="npx",
                cwd=parent_dir,
                command=f"npx --yes create-vite@latest '{base_name}' --template react",
                description="Initialize modern React + Vite application",
            )
        )

        # 2. Package installation
        steps.append(
            Step(
                id="install_deps",
                name=f"Install Dependencies ({node_pkg})",
                cli=node_pkg,
                cwd=p_str,
                command=f"{node_pkg} install",
                description=f"Run {node_pkg} install",
                depends_on=["create_app"],
            )
        )

        # 3. Build test
        steps.append(
            Step(
                id="build",
                name="Run Production Build Test",
                cli=node_pkg,
                cwd=p_str,
                command=f"{node_pkg} run build",
                description="Verify build compiles successfully",
                depends_on=["install_deps"],
            )
        )

        # 4. Git init
        if include_git:
            steps.append(
                Step(
                    id="git_init",
                    name="Initialize Git Repository",
                    cli="git",
                    cwd=p_str,
                    command="git init && git add . && git commit -m 'Initial commit by OmniCLI'",
                    description="Initialize Git repository and commit files",
                    depends_on=["create_app"],
                    allow_failure=True,
                )
            )

        return Pipeline(
            name=f"react-{project_name}",
            description=f"React / Node Project Pipeline ({project_name})",
            steps=steps,
            working_dir=p_str,
        )

    def _plan_docker_project(
        self,
        project_name: str,
        proj_dir: Path,
        include_git: bool,
    ) -> Pipeline:
        p_str = str(proj_dir)
        steps = []

        dockerfile_content = (
            "FROM alpine:latest\n"
            "WORKDIR /app\n"
            "RUN echo 'Hello from OmniCLI Container' > message.txt\n"
            "CMD [\"cat\", \"message.txt\"]\n"
        )
        cmd_df = b64_write_cmd(f"{p_str}/Dockerfile", dockerfile_content)

        steps.append(
            Step(
                id="ensure_dockerfile",
                name="Ensure Dockerfile Exists",
                cli="python3",
                cwd=p_str,
                command=f"test -f '{p_str}/Dockerfile' || ({cmd_df})",
                description="Check or scaffold Dockerfile",
            )
        )

        steps.append(
            Step(
                id="docker_build",
                name="Build Docker Container Image",
                cli="docker",
                cwd=p_str,
                command=f"docker build -t {project_name}:latest .",
                description="Compile and tag container image",
                depends_on=["ensure_dockerfile"],
            )
        )

        steps.append(
            Step(
                id="docker_test",
                name="Test Container Execution",
                cli="docker",
                cwd=p_str,
                command=f"docker run --rm {project_name}:latest",
                description="Run container and verify output",
                depends_on=["docker_build"],
            )
        )

        return Pipeline(
            name=f"docker-{project_name}",
            description=f"Docker Containerization Pipeline ({project_name})",
            steps=steps,
            working_dir=p_str,
        )

    def _plan_universal_project(
        self,
        project_name: str,
        proj_dir: Path,
        intent: str,
        include_git: bool,
    ) -> Pipeline:
        p_str = str(proj_dir)
        steps = []

        steps.append(
            Step(
                id="init_project",
                name="Create Project Workspace",
                cli="mkdir",
                command=f"mkdir -p '{p_str}'",
                description="Create root directory",
            )
        )

        readme_text = f"# {project_name}\n\nGenerated by OmniCLI from instruction:\n> {intent}\n"
        cmd_rm = b64_write_cmd(f"{p_str}/README.md", readme_text)
        steps.append(
            Step(
                id="scaffold_readme",
                name="Generate README and Metadata",
                cli="python3",
                command=cmd_rm,
                description="Create README.md documentation",
                depends_on=["init_project"],
            )
        )

        if include_git:
            steps.append(
                Step(
                    id="git_init",
                    name="Initialize Git Repository",
                    cli="git",
                    cwd=p_str,
                    command="git init && git add . && git commit -m 'Initial commit from OmniCLI'",
                    description="Set up version control and create first commit",
                    depends_on=["scaffold_readme"],
                    allow_failure=True,
                )
            )

        return Pipeline(
            name=f"project-{project_name}",
            description=f"Project Pipeline for '{intent}'",
            steps=steps,
            working_dir=p_str,
        )
