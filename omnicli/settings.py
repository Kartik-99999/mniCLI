"""
User defaults for OmniCLI (~/.omnicli/config.json), like Claude Code's settings.

Any `omnicli team` flag the user doesn't pass falls back to these. Explicit
command-line flags always win.

    omnicli config set lead claude
    omnicli config set jobs 3
    omnicli config set models.claude opus
"""

import json
from typing import Any, Dict, Optional

from omnicli.paths import settings_path

# key -> (type, default, help). Dict-typed keys hold per-agent values: "models.claude".
SCHEMA: Dict[str, tuple] = {
    "lead": (str, None, "AI CLI that plans and reviews (default: best available)"),
    "workers": (list, None, "AI CLIs that do the work, e.g. claude,agy,muse (default: all ready)"),
    "jobs": (int, 2, "tasks that may run at the same time"),
    "rounds": (int, 2, "max work rounds including lead follow-ups"),
    "retries": (int, 1, "revision attempts per task after a lead review"),
    "max_tasks": (int, 10, "most tasks the lead may plan per round"),
    "task_timeout": (float, 25.0, "minutes before a worker is stopped"),
    "lead_timeout": (float, 10.0, "minutes before a lead call is stopped"),
    "review": (bool, True, "lead reviews each task"),
    "git": (bool, True, "git checkpoint after each task"),
    "auto_approve": (bool, False, "start without confirming the plan (like -y)"),
    "max_calls": (int, 0, "stop a run after this many AI calls (0 = no limit)"),
    "max_minutes": (float, 0.0, "stop a run after this many minutes (0 = no limit)"),
    "models": (dict, {}, "per-agent model, e.g. models.claude=opus"),
    "effort": (dict, {}, "per-agent reasoning effort, e.g. effort.agy=high"),
}

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


class SettingsError(ValueError):
    pass


def defaults() -> Dict[str, Any]:
    return {k: (dict(v[1]) if isinstance(v[1], dict) else v[1]) for k, v in SCHEMA.items()}


def load_raw() -> Dict[str, Any]:
    try:
        data = json.loads(settings_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load() -> Dict[str, Any]:
    """Defaults merged with the user's saved values (unknown keys are ignored)."""
    merged = defaults()
    for k, v in load_raw().items():
        if k in SCHEMA:
            merged[k] = v
    return merged


def get(key: str, fallback: Any = None) -> Any:
    top, _, sub = key.partition(".")
    value = load().get(top, fallback)
    if sub:
        return value.get(sub, fallback) if isinstance(value, dict) else fallback
    return value


def coerce(key: str, raw: Any) -> Any:
    """Convert a CLI string to the key's type; raise SettingsError if invalid."""
    top, _, sub = key.partition(".")
    if top not in SCHEMA:
        raise SettingsError(f"unknown setting '{top}'. Known: {', '.join(sorted(SCHEMA))}")
    typ = SCHEMA[top][0]
    if typ is dict:
        if not sub:
            raise SettingsError(f"'{top}' is per-agent: use {top}.<agent>, e.g. {top}.claude")
        return str(raw)
    if sub:
        raise SettingsError(f"'{top}' has no sub-keys")
    if not isinstance(raw, str):
        return raw
    s = raw.strip()
    if typ is bool:
        if s.lower() in _TRUE:
            return True
        if s.lower() in _FALSE:
            return False
        raise SettingsError(f"'{top}' must be true or false")
    if typ is int:
        try:
            return int(s)
        except ValueError:
            raise SettingsError(f"'{top}' must be a whole number")
    if typ is float:
        try:
            return float(s)
        except ValueError:
            raise SettingsError(f"'{top}' must be a number")
    if typ is list:
        return [x.strip() for x in s.split(",") if x.strip()]
    return s


def _write(data: Dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def set_value(key: str, raw: Any) -> Any:
    value = coerce(key, raw)
    top, _, sub = key.partition(".")
    data = load_raw()
    if sub:
        d = data.get(top) if isinstance(data.get(top), dict) else {}
        d[sub] = value
        data[top] = d
    else:
        data[top] = value
    _write(data)
    return value


def unset(key: str) -> bool:
    top, _, sub = key.partition(".")
    data = load_raw()
    if sub:
        d = data.get(top)
        if isinstance(d, dict) and sub in d:
            del d[sub]
            _write(data)
            return True
        return False
    if top in data:
        del data[top]
        _write(data)
        return True
    return False


def pick(cli_value: Optional[Any], key: str) -> Any:
    """CLI flag if given (not None), else the saved/default setting."""
    return cli_value if cli_value is not None else get(key)
