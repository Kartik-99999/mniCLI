"""
Recipe Catalog and Template Manager for OmniCLI.
Includes built-in multi-tool project templates and allows users to save/load custom recipes.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

from omnicli.models import Pipeline, Step

USER_RECIPES_DIR = Path.home() / ".omnicli" / "recipes"
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


BUILTIN_RECIPES: Dict[str, Dict] = {
    "ai-trio-work": {
        "name": "ai-trio-work",
        "description": "AI CLI diagnostics: runs muse, codex and agy health/version checks in parallel and writes a report (for real multi-agent building use `omnicli team`)",
        "parallel": True,
        "steps": [
            {
                "id": "muse_audit",
                "name": "Muse CLI Skill Audit",
                "cli": "muse",
                "command": "muse skills list --enabled-only",
                "description": "Muse: Scans and audits enabled agent skills and coding capabilities",
                "allow_failure": True,
            },
            {
                "id": "codex_doctor",
                "name": "Codex CLI System Diagnostics",
                "cli": "codex",
                "command": "codex doctor",
                "description": "Codex: Performs full runtime diagnostics and sandbox verification",
                "allow_failure": True,
            },
            {
                "id": "agy_probe",
                "name": "Antigravity CLI Engine Verification",
                "cli": "agy",
                "command": "agy --version",
                "description": "Antigravity: Probes AGY core engine version and runtime bridge",
                "allow_failure": True,
            },
            {
                "id": "synthesis_work",
                "name": "Synthesize AI Trio Collaborative Report",
                "cli": "python3",
                "command": f"PYTHONPATH='{PACKAGE_ROOT}' python3 -m omnicli.ai_ensemble",
                "description": "Synthesizes outputs into AI_TRIO_REPORT.md and ensemble configuration",
                "depends_on": ["muse_audit", "codex_doctor", "agy_probe"],
            },
            {
                "id": "git_commit",
                "name": "Git Version Control Checkpoint",
                "cli": "git",
                "command": "git init && git add AI_TRIO_REPORT.md agent_ensemble_config.json && git commit -m 'feat: AI Trio (muse, codex, agy) collaborative project run'",
                "description": "Git: Checkpoints the collaborative work into version control",
                "depends_on": ["synthesis_work"],
                "allow_failure": True,
            },
        ],
    },
    "python-service": {
        "name": "python-service",
        "description": "Production Python service: venv -> pip install -> unit tests -> git init -> dockerfile",
        "variables": {
            "app_name": "my_service",
            "python_bin": "python3",
        },
        "steps": [
            {
                "id": "scaffold",
                "name": "Scaffold Directories & Files",
                "cli": "python3",
                "command": "python3 -c \"import os; os.makedirs('src', exist_ok=True); os.makedirs('tests', exist_ok=True); open('src/app.py', 'w').write('def run(): return True\\n'); open('tests/test_app.py', 'w').write('from src.app import run\\ndef test_run(): assert run() is True\\n'); open('requirements.txt', 'w').write('pytest>=8.0.0\\n');\"",
                "description": "Create src, tests and requirements.txt",
            },
            {
                "id": "venv",
                "name": "Create Virtual Environment",
                "cli": "python3",
                "command": "python3 -m venv .venv",
                "description": "Initialize Python virtualenv",
                "depends_on": ["scaffold"],
            },
            {
                "id": "pip_install",
                "name": "Install Dependencies",
                "cli": ".venv/bin/pip",
                "command": ".venv/bin/pip install -r requirements.txt",
                "fallback_command": "pip3 install -r requirements.txt",
                "description": "Install dependencies",
                "depends_on": ["venv"],
                "allow_failure": True,
            },
            {
                "id": "test",
                "name": "Run Pytest Suite",
                "cli": ".venv/bin/pytest",
                "command": ".venv/bin/pytest tests/",
                "fallback_command": "python3 -m unittest discover tests/",
                "description": "Execute automated test verification",
                "depends_on": ["pip_install"],
                "allow_failure": True,
            },
            {
                "id": "git",
                "name": "Git Repository Setup",
                "cli": "git",
                "command": "git init && echo '.venv/\n__pycache__/\n' > .gitignore && git add . && git commit -m 'Initial commit from OmniCLI'",
                "description": "Initialize git repository and commit initial code",
                "depends_on": ["scaffold"],
                "allow_failure": True,
            },
        ],
    },
    "static-landing-page": {
        "name": "static-landing-page",
        "description": "Modern responsive static site: HTML/CSS scaffolding -> git init -> browser preview check",
        "steps": [
            {
                "id": "scaffold",
                "name": "Scaffold HTML & CSS",
                "cli": "python3",
                "command": (
                    "python3 -c \""
                    "open('index.html', 'w').write('<!DOCTYPE html><html><head><title>Project</title><style>body{font-family:sans-serif;padding:2rem;background:#0d1117;color:#fff;}h1{color:#58a6ff;}</style></head><body><h1>Welcome to OmniCLI Project</h1><p>Orchestrated smoothly.</p></body></html>'); "
                    "open('README.md', 'w').write('# Static Site\\nBuilt with OmniCLI\\n');\""
                ),
                "description": "Create modern index.html and styles",
            },
            {
                "id": "git_init",
                "name": "Git Init & Commit",
                "cli": "git",
                "command": "git init && git add . && git commit -m 'Initial static site scaffold'",
                "description": "Version control initialization",
                "depends_on": ["scaffold"],
                "allow_failure": True,
            },
            {
                "id": "test_server",
                "name": "Verify HTTP Server Availability",
                "cli": "python3",
                "command": "python3 -c \"import http.server, socket; s=socket.socket(); s.bind(('', 0)); port=s.getsockname()[1]; s.close(); print(f'HTTP test port available: {port}')\"",
                "description": "Test port and server runtime readiness",
                "depends_on": ["scaffold"],
            },
        ],
    },
    "docker-container": {
        "name": "docker-container",
        "description": "Automated Containerization: generate Dockerfile -> docker build -> docker test",
        "steps": [
            {
                "id": "dockerfile",
                "name": "Generate Dockerfile",
                "cli": "python3",
                "command": "python3 -c \"open('Dockerfile', 'w').write('FROM alpine:latest\\nRUN echo \\\"OmniCLI Container Ready\\\" > /msg.txt\\nCMD cat /msg.txt\\n')\"",
                "description": "Scaffold Dockerfile",
            },
            {
                "id": "docker_build",
                "name": "Docker Image Build",
                "cli": "docker",
                "command": "docker build -t omni-container:latest .",
                "description": "Build container image with Docker CLI",
                "depends_on": ["dockerfile"],
            },
            {
                "id": "docker_run",
                "name": "Docker Verification Run",
                "cli": "docker",
                "command": "docker run --rm omni-container:latest",
                "description": "Verify container runs and outputs message",
                "depends_on": ["docker_build"],
            },
        ],
    },
}


class RecipeManager:
    """Loads, saves, and lists recipes."""

    def __init__(self, user_dir: Optional[Path] = None):
        self.user_dir = user_dir or USER_RECIPES_DIR
        self.user_dir.mkdir(parents=True, exist_ok=True)

    def list_recipes(self) -> List[Dict[str, str]]:
        recipes = []
        for name, data in BUILTIN_RECIPES.items():
            recipes.append({
                "name": name,
                "type": "builtin",
                "description": data.get("description", ""),
                "steps_count": len(data.get("steps", [])),
            })

        # Scan user custom recipes
        for file in self.user_dir.glob("*.json"):
            try:
                data = json.loads(file.read_text(encoding="utf-8"))
                recipes.append({
                    "name": file.stem,
                    "type": "custom",
                    "description": data.get("description", ""),
                    "steps_count": len(data.get("steps", [])),
                })
            except Exception:
                pass

        return recipes

    def get_pipeline(self, name: str) -> Optional[Pipeline]:
        # Check builtins
        if name in BUILTIN_RECIPES:
            return Pipeline.from_dict(BUILTIN_RECIPES[name])

        # Check user dir
        custom_file = self.user_dir / f"{name}.json"
        if custom_file.exists():
            try:
                data = json.loads(custom_file.read_text(encoding="utf-8"))
                return Pipeline.from_dict(data)
            except Exception:
                return None

        return None

    def save_recipe(self, pipeline: Pipeline) -> Path:
        target = self.user_dir / f"{pipeline.name}.json"
        target.write_text(json.dumps(pipeline.to_dict(), indent=2), encoding="utf-8")
        return target
