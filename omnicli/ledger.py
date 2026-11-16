"""
Usage ledger: one JSON line per AI agent call, appended to ~/.omnicli/ledger.jsonl.

Powers `omnicli usage`. AI CLIs don't report token counts in a common way, so the
ledger stores exact character counts plus *estimated* tokens (chars / 4); anything
that shows token numbers must label them as estimates.

Entry fields (all written by agents.run_agent):
  ts            float  epoch seconds when the call finished
  agent         str    adapter id (claude, codex, agy, muse, ...)
  mode          str    "readonly" (lead) or "edit" (worker)
  kind          str    "plan" | "review" | "final" | "task" | "ping" | "project-review" | "other"
  run_dir       str    project directory of the team run ("" for pings)
  task_id       str    task id for kind=="task" ("" otherwise)
  duration_sec  float
  exit_code     int
  ok            bool
  timed_out     bool
  prompt_chars  int
  output_chars  int
  est_tokens_in  int   prompt_chars // 4
  est_tokens_out int   output_chars // 4
"""

import json
import threading
import time
from typing import Dict, Iterable, List, Optional

from omnicli.paths import ledger_path, quota_resets_path, skill_outcomes_path, team_memory_path

_lock = threading.Lock()

SKILL_CATEGORIES = ("frontend", "backend", "tests", "docs", "devops", "design", "other")


def record(entry: Dict) -> None:
    """Append one entry. Never raises - usage tracking must not break a run."""
    try:
        entry = dict(entry)
        entry.setdefault("ts", time.time())
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


def read(since: float = 0.0, agent: Optional[str] = None, run_dir: Optional[str] = None) -> List[Dict]:
    """All entries (oldest first), optionally filtered. Corrupt lines are skipped."""
    path = ledger_path()
    if not path.exists():
        return []
    out: List[Dict] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(e, dict) or e.get("ts", 0) < since:
                continue
            if agent and e.get("agent") != agent:
                continue
            if run_dir and e.get("run_dir") != run_dir:
                continue
            out.append(e)
    return out


def make_entry(agent: str, mode: str, meta: Optional[Dict], duration: float, exit_code: int, ok: bool,
               timed_out: bool, prompt: str, output: str) -> Dict:
    meta = meta or {}
    return {
        "ts": time.time(),
        "agent": agent,
        "mode": mode,
        "kind": meta.get("kind", "other"),
        "run_dir": meta.get("run_dir", ""),
        "task_id": meta.get("task_id", ""),
        "duration_sec": round(duration, 2),
        "exit_code": exit_code,
        "ok": ok,
        "timed_out": timed_out,
        "prompt_chars": len(prompt),
        "output_chars": len(output),
        "est_tokens_in": len(prompt) // 4,
        "est_tokens_out": len(output) // 4,
    }


def summarize(entries: Iterable[Dict], key: str = "agent") -> Dict[str, Dict]:
    """Group entries by `key` -> {calls, ok, failed, seconds, est_tokens_in, est_tokens_out}."""
    groups: Dict[str, Dict] = {}
    for e in entries:
        g = groups.setdefault(str(e.get(key, "")), {"calls": 0, "ok": 0, "failed": 0, "seconds": 0.0,
                                                    "est_tokens_in": 0, "est_tokens_out": 0})
        g["calls"] += 1
        g["ok" if e.get("ok") else "failed"] += 1
        g["seconds"] += float(e.get("duration_sec", 0) or 0)
        g["est_tokens_in"] += int(e.get("est_tokens_in", 0) or 0)
        g["est_tokens_out"] += int(e.get("est_tokens_out", 0) or 0)
    return groups


def record_task_outcome(entry: Dict) -> None:
    """Append a compact task result; never persist prompts, diffs, or source content here."""
    try:
        path = skill_outcomes_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(dict(entry), ensure_ascii=False) + "\n")
    except Exception:
        pass


def task_outcomes() -> List[Dict]:
    path = skill_outcomes_path()
    if not path.exists():
        return []
    latest = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict) or not item.get("agent"):
                    continue
                key = (item.get("run_dir", ""), item.get("task_id", ""), item.get("round", 1))
                if key not in latest or float(item.get("ts", 0) or 0) >= float(latest[key].get("ts", 0) or 0):
                    latest[key] = item
    except OSError:
        return []
    return list(latest.values())


def skill_rankings(min_tasks: int = 1) -> List[Dict]:
    """Local per-category agent outcomes, best first within each category."""
    groups = {}
    for item in task_outcomes():
        category, agent = item.get("category", "other"), item.get("agent", "")
        if category not in SKILL_CATEGORIES or not agent:
            continue
        group = groups.setdefault((category, agent), {"category": category, "agent": agent, "tasks": 0,
                                                       "first_try": 0, "revisions": 0, "seconds": 0.0})
        group["tasks"] += 1
        group["first_try"] += int(bool(item.get("approved_first_try")))
        group["revisions"] += int(item.get("revisions", 0) or 0)
        group["seconds"] += float(item.get("duration_sec", 0) or 0)
    rows = []
    for row in groups.values():
        if row["tasks"] < min_tasks:
            continue
        count = row["tasks"]
        row["first_try_rate"] = round(row["first_try"] / count, 3)
        row["avg_revisions"] = round(row["revisions"] / count, 2)
        row["avg_seconds"] = round(row["seconds"] / count, 1)
        rows.append(row)
    rows.sort(key=lambda row: (row["category"], -row["first_try_rate"], row["avg_revisions"], row["avg_seconds"], row["agent"]))
    return rows


def skill_recommendations(min_tasks: int = 3) -> Dict[str, Dict]:
    """Best measured agent per category; sparse history is not used for routing."""
    recommendations = {}
    for row in skill_rankings(min_tasks=min_tasks):
        recommendations.setdefault(row["category"], row)
    return recommendations


def record_quota_reset(agent: str, reset_at: float, reason: str = "") -> None:
    """Remember the most recent plan-cap reset notice for one agent."""
    try:
        path = quota_resets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                current = {}
            if not isinstance(current, dict):
                current = {}
            old = current.get(agent) or {}
            if float(reset_at) >= float(old.get("reset_at", 0) or 0):
                current[agent] = {"reset_at": float(reset_at), "reason": str(reason or "")[:240],
                                  "observed_at": time.time()}
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                tmp.replace(path)
    except Exception:
        pass


def active_quota_resets(now: Optional[float] = None) -> Dict[str, Dict]:
    path = quota_resets_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    current = time.time() if now is None else float(now)
    return {agent: item for agent, item in data.items()
            if isinstance(item, dict) and float(item.get("reset_at", 0) or 0) > current}


def record_team_lesson(lesson: str, category: str = "other", source: str = "") -> None:
    """Store a short reusable lesson without retaining prompts, diffs, or logs."""
    text = " ".join(str(lesson or "").split())[:300]
    if not text:
        return
    item = {"ts": time.time(), "category": category if category in SKILL_CATEGORIES else "other",
            "lesson": text, "source": str(source or "")[:80]}
    try:
        path = team_memory_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            known = {row["lesson"].lower() for row in read_team_memory()}
            if text.lower() in known:
                return
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_team_memory(category: Optional[str] = None) -> List[Dict]:
    path = team_memory_path()
    if not path.exists():
        return []
    items = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("lesson") and (not category or row.get("category") == category):
                    items.append(row)
    except OSError:
        return []
    return items
