"""Persistent live budget overrides for a team run."""

import json

from omnicli.paths import project_state_dir


def control_path(project_dir):
    return project_state_dir(project_dir) / "budget.json"


def read_control(project_dir):
    path = control_path(project_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_control(project_dir, values):
    path = control_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = read_control(project_dir)
    data.update(values)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return data
