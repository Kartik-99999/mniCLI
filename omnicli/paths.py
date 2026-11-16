"""
Where OmniCLI keeps its files.

Everything lives under ~/.omnicli unless OMNICLI_HOME is set (tests point it at a temp
dir so they never touch the real one). Paths are computed on every call, so changing
the env var at runtime takes effect immediately.
"""

import os
from pathlib import Path


def omni_home() -> Path:
    return Path(os.environ.get("OMNICLI_HOME") or (Path.home() / ".omnicli")).expanduser()


def agents_config_path() -> Path:
    """User overrides for AI agent adapters (see omnicli/agents.py)."""
    return omni_home() / "agents.json"


def settings_path() -> Path:
    """User defaults for `omnicli team` and friends (see omnicli/settings.py)."""
    return omni_home() / "config.json"


def profiles_path() -> Path:
    """Named reusable team configurations."""
    return omni_home() / "profiles.json"


def health_cache_path() -> Path:
    return omni_home() / "agent_health.json"


def ledger_path() -> Path:
    """Append-only JSONL log of every AI agent call (see omnicli/ledger.py)."""
    return omni_home() / "ledger.jsonl"


def skill_outcomes_path() -> Path:
    """Task outcomes used for local agent skill rankings."""
    return omni_home() / "skill_outcomes.jsonl"


def quota_resets_path() -> Path:
    """Latest locally observed agent plan-cap reset times."""
    return omni_home() / "quota_resets.json"


def team_memory_path() -> Path:
    """Compact reusable lessons from prior team runs."""
    return omni_home() / "team_memory.jsonl"


def runs_registry_path() -> Path:
    """Index of every team run on this machine (see omnicli/runs.py)."""
    return omni_home() / "runs.json"


def project_state_dir(project_dir) -> Path:
    """Per-project team state: state.json, REPORT.md, logs/."""
    return Path(project_dir).expanduser().resolve() / ".omnicli" / "team"
