"""
Run registry: an index of every `omnicli team` run on this machine (~/.omnicli/runs.json).

The source of truth for a run is always its own <project>/.omnicli/team/state.json;
the registry only lets commands like `resume`, `continue`, `runs` and `status` find
runs without being told the directory.

Record fields:
  dir            str    absolute project directory (the key)
  goal           str
  project_name   str
  phase          str    planning | planned | running | stopped | complete | incomplete | failed
  lead           str
  workers        [str]
  tasks_total    int
  tasks_done     int
  tasks_failed   int
  round          int
  started_at     float  epoch
  updated_at     float  epoch
  pid            int    process id while phase == "running", else 0
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from omnicli.paths import project_state_dir, runs_registry_path

_lock = threading.Lock()

UNFINISHED_PHASES = ("planning", "planned", "running", "stopped", "incomplete", "failed")


def _load() -> Dict[str, Dict]:
    try:
        data = json.loads(runs_registry_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: Dict[str, Dict]) -> None:
    path = runs_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def register_run(record: Dict) -> None:
    """Insert or update a run (keyed by its absolute dir). Never raises."""
    try:
        d = str(Path(record["dir"]).expanduser().resolve())
        with _lock:
            data = _load()
            merged = dict(data.get(d, {}), **record)
            merged["dir"] = d
            merged["updated_at"] = time.time()
            data[d] = merged
            _save(data)
    except Exception:
        pass


def forget_run(project_dir) -> bool:
    d = str(Path(project_dir).expanduser().resolve())
    with _lock:
        data = _load()
        if d in data:
            del data[d]
            _save(data)
            return True
    return False


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def list_runs(include_missing: bool = False) -> List[Dict]:
    """Runs newest-first. Each record gets 'exists' (state.json present) and 'alive' (pid running)."""
    out = []
    for rec in _load().values():
        rec = dict(rec)
        rec["exists"] = (project_state_dir(rec["dir"]) / "state.json").exists()
        rec["alive"] = rec.get("phase") == "running" and pid_alive(rec.get("pid", 0))
        if rec["exists"] or include_missing:
            out.append(rec)
    out.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
    return out


def most_recent_run(unfinished_only: bool = False) -> Optional[Dict]:
    for rec in list_runs():
        if unfinished_only and rec.get("phase") not in UNFINISHED_PHASES:
            continue
        return rec
    return None


def find_run(ref: str) -> Optional[Dict]:
    """Resolve a user reference: a 1-based index from `omnicli runs`, a directory path,
    or a case-insensitive substring of the project name / goal (most recent match wins)."""
    runs = list_runs()
    ref = (ref or "").strip()
    if not ref:
        return runs[0] if runs else None
    if ref.isdigit():
        i = int(ref)
        return runs[i - 1] if 1 <= i <= len(runs) else None
    p = Path(ref).expanduser()
    if p.exists():
        d = str(p.resolve())
        for r in runs:
            if r["dir"] == d:
                return r
        if (project_state_dir(d) / "state.json").exists():
            return {"dir": d, "exists": True, "alive": False}
    low = ref.lower()
    for r in runs:
        if low in str(r.get("project_name", "")).lower() or low in str(r.get("goal", "")).lower() \
                or low in Path(r["dir"]).name.lower():
            return r
    return None


def load_state(project_dir) -> Optional[Dict]:
    """Read a run's state.json (None if missing/corrupt)."""
    try:
        return json.loads((project_state_dir(project_dir) / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return None
