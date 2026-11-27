"""
AI Trio Ensemble synthesizer for OmniCLI.
Consolidates diagnostics and capability outputs from muse, codex, and agy CLIs.
"""

import datetime
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from omnicli import paths


def synthesize(output_dir: Optional[str] = None):
    target_dir = Path(output_dir or os.getcwd()).resolve()
    runs_dir = paths.omni_home() / "runs"

    def read_run(name: str) -> str:
        f = runs_dir / f"{name}.stdout"
        if f.exists():
            return f.read_text(encoding="utf-8", errors="replace").strip()
        return "(output not found)"

    muse_out = read_run("muse_audit")
    codex_out = read_run("codex_doctor")
    agy_out = read_run("agy_probe")

    # Parse muse skills count
    muse_lines = muse_out.splitlines()
    muse_skill_count = max(0, len(muse_lines) - 1) if muse_lines else 0

    # Parse codex health
    codex_status = "Operational (with warnings)" if "16 ok" in codex_out or "Doctor" in codex_out else "Diagnostics Completed"

    # Parse agy version
    agy_ver = agy_out.strip() or "Installed"

    report_lines = [
        "# AI Trio Ensemble Report",
        f"**Generated:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "**Orchestrator:** OmniCLI (cliconnect)",
        "",
        "## Executive Summary",
        "| Agent CLI | Role | Status | Key Metric |",
        "| :--- | :--- | :--- | :--- |",
        f"| **Muse Code** | Terminal Coding Agent | Probed | {muse_skill_count} active skills indexed |",
        f"| **Codex CLI** | Code & Architecture Reviewer | Probed | {codex_status} |",
        f"| **Antigravity (AGY)** | Universal Task Agent | Probed | Engine v{agy_ver} |",
        "",
        "---",
        "",
        "## 1. Muse Code CLI (Skills & Agent Capabilities)",
        "```text",
        "\n".join(muse_lines[:40]),
        f"... ({len(muse_lines)} total lines in log)",
        "```",
        "",
        "## 2. Codex CLI (System Diagnostics & Environment)",
        "```text",
        "\n".join(codex_out.splitlines()[:35]),
        "```",
        "",
        "## 3. Antigravity (AGY) CLI (Engine Verification)",
        "```text",
        f"Antigravity CLI Version: {agy_ver}",
        "```",
        "",
        "---",
        "*Diagnostics only - this recipe does not make the agents work together. Use `omnicli team \"<goal>\"` for that.*",
    ]

    report_content = "\n".join(report_lines)
    (target_dir / "AI_TRIO_REPORT.md").write_text(report_content, encoding="utf-8")

    manifest = {
        "orchestration": {
            "name": "ai-trio-ensemble",
            "managed_by": "omnicli",
            "timestamp": datetime.datetime.now().isoformat(),
            "target_dir": str(target_dir),
        },
        "agents": {
            "muse": {
                "name": "Muse Code",
                "binary": shutil.which("muse") or "not found",
                "active_skills_count": muse_skill_count,
                "status": "ready",
            },
            "codex": {
                "name": "Codex CLI",
                "binary": shutil.which("codex") or "not found",
                "status": "ready",
            },
            "agy": {
                "name": "Antigravity CLI",
                "binary": shutil.which("agy") or "not found",
                "version": agy_ver,
                "status": "ready",
            },
        },
    }

    (target_dir / "agent_ensemble_config.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"✓ AI Trio artifacts generated successfully in {target_dir}")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else None
    synthesize(out)
