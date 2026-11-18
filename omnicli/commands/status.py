"""
`omnicli status` - a snapshot of one team run, like Claude Code's /status.

    omnicli status                    current dir's run, else the most recent run
    omnicli status 2                  run #2 from `omnicli runs` (or a dir / name / goal fragment)
    omnicli status -d ~/code/app      the run in that project directory
    omnicli status --watch [SECONDS]  live agent dashboard: tasks, output, blockers and completed work
    omnicli status --json             machine-readable snapshot

Everything comes from the run's own <dir>/.omnicli/team/state.json plus its logs/;
nothing here calls an AI CLI.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import re
import shlex
import shutil
import sys
import textwrap
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from omnicli import runs
from omnicli.paths import project_state_dir
from omnicli.ui import Colors, c_bold, c_cyan, c_dim, c_green, c_magenta, c_red, c_yellow, colorize

NAMES = ["status"]

DEFAULT_WATCH_SECONDS = 3.0
MIN_WATCH_SECONDS = 0.2
TAIL_LINES = 3
SUMMARY_CHARS = 300
BAR_WIDTH = 20
COUNT_KEYS = ("done", "failed", "running", "pending", "skipped")

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_CLEAR_SCREEN = "\033[H\033[2J"

# phase -> (badge text, styles)
_PHASES = {
    "planning": ("… PLANNING", (Colors.BOLD, Colors.BRIGHT_BLUE)),
    "planned": ("◆ PLANNED", (Colors.BOLD, Colors.BRIGHT_CYAN)),
    "running": ("⚡ RUNNING", (Colors.BOLD, Colors.BRIGHT_YELLOW)),
    "stopped": ("■ STOPPED", (Colors.BOLD, Colors.BRIGHT_YELLOW)),
    "complete": ("✓ COMPLETE", (Colors.BOLD, Colors.BRIGHT_GREEN)),
    "incomplete": ("◐ INCOMPLETE", (Colors.BOLD, Colors.BRIGHT_YELLOW)),
    "failed": ("✗ FAILED", (Colors.BOLD, Colors.BRIGHT_RED)),
}

_STATUS_COLOR: Dict[str, Callable[[str], str]] = {
    "done": c_green,
    "failed": c_red,
    "running": c_yellow,
    "pending": c_dim,
    "skipped": c_cyan,
}


# --------------------------------------------------------------------------- parser

def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "status",
        help="Show the live team dashboard: agents, tasks, output, blockers and completed work",
        description="Show progress of a team run. Picks -d DIR, else REF, else the run in the current "
                    "directory, else the most recent run.",
    )
    p.add_argument("ref", nargs="?", default=None,
                   help="Which run: its number in `omnicli runs`, a directory, or part of its name/goal")
    p.add_argument("-d", "--dir", default=None, help="Project directory of the run")
    # no type= so `omnicli status --watch myproj` can treat 'myproj' as REF instead of erroring
    p.add_argument("--watch", nargs="?", const=DEFAULT_WATCH_SECONDS, default=None, metavar="SECONDS",
                   help=f"Redraw every SECONDS (default {DEFAULT_WATCH_SECONDS:g}) until the run stops; Ctrl-C to quit")
    p.add_argument("--json", action="store_true", help="Print a JSON snapshot instead of the dashboard")
    p.set_defaults(func=handler)


def handler(args) -> int:
    return run(args)


# --------------------------------------------------------------------------- small helpers

def _q(path) -> str:
    return shlex.quote(str(path))


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _float(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clean(text: str) -> str:
    """Strip ANSI escapes / control chars so agent output can't garble the terminal."""
    text = _ANSI_RE.sub("", str(text)).replace("\t", "    ")
    return _CTRL_RE.sub("", text)


def _vlen(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _vlen(s))


def _trunc(s: str, width: int) -> str:
    s = " ".join(str(s).split())
    if width <= 1:
        return s[:max(width, 0)]
    return s if len(s) <= width else s[: width - 1] + "…"


def fmt_duration(seconds) -> str:
    s = max(0, int(seconds or 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60:02d}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def fmt_ago(ts, now: float) -> str:
    t = _float(ts)
    if not t:
        return "unknown"
    delta = now - t
    if delta < 5:
        return "just now"
    return fmt_duration(delta) + " ago"


def _term_width() -> int:
    return max(60, min(140, shutil.get_terminal_size((100, 24)).columns))


def _has_state(d: Path) -> bool:
    return (project_state_dir(d) / "state.json").is_file()


# --------------------------------------------------------------------------- which run

def resolve_run(ref: Optional[str] = None, dir_opt: Optional[str] = None,
                cwd: Optional[str] = None) -> Tuple[Optional[Path], str]:
    """(project dir, "") or (None, friendly error). Order: -d DIR, REF, current dir, most recent run."""
    if dir_opt:
        d = Path(dir_opt).expanduser().resolve()
        if not d.is_dir():
            return None, f"Directory {d} does not exist.\n  List known runs with: omnicli runs"
        if _has_state(d):
            return d, ""
        return None, (f"No team run in {d} (no .omnicli/team/state.json).\n"
                      f"  Start one there: omnicli team \"<goal>\" -d {_q(d)}\n"
                      f"  Or list runs:    omnicli runs")
    if ref:
        rec = runs.find_run(ref)
        if rec and rec.get("dir"):
            return Path(rec["dir"]), ""
        return None, f"No team run matches '{ref}'.\n  List runs with: omnicli runs"
    try:
        here = Path(cwd).expanduser().resolve() if cwd else Path.cwd()
    except OSError:
        here = None
    if here is not None and _has_state(here):
        return here, ""
    rec = runs.most_recent_run()
    if rec and rec.get("dir"):
        return Path(rec["dir"]), ""
    return None, ("No team runs found yet.\n"
                  "  Start one:  omnicli team \"<goal>\" -d DIR\n"
                  "  List runs:  omnicli runs")


# --------------------------------------------------------------------------- logs

def find_task_log(logs_dir: Path, task_id: str, known_agents=None) -> Optional[Dict]:
    """Newest attempt log of a task: {agent, n, started, md, stderr} or None.

    Worker logs are <taskid>-<agent>-<n>.md with .prompt.md / .stderr.log siblings; the prompt
    file is written when the attempt starts, so its mtime is the attempt's start time.
    `known_agents` filters out look-alikes (task 't1' must not pick up 't1-2-claude-1.md').
    """
    try:
        entries = list(Path(logs_dir).iterdir())
    except OSError:
        return None
    pat = re.compile(r"^" + re.escape(task_id) + r"-(?P<agent>.+)-(?P<n>\d+)(?P<ext>\.prompt\.md|\.stderr\.log|\.md)$")
    groups: Dict[Tuple[str, int], Dict] = {}
    for p in entries:
        m = pat.match(p.name)
        if not m:
            continue
        agent = m.group("agent")
        if known_agents and agent not in known_agents:
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        g = groups.setdefault((agent, int(m.group("n"))), {"agent": agent, "n": int(m.group("n")),
                                                          "started": None, "first_seen": mtime,
                                                          "md": None, "stderr": None})
        g["first_seen"] = min(g["first_seen"], mtime)
        ext = m.group("ext")
        if ext == ".prompt.md":
            g["started"] = mtime
        elif ext == ".md":
            g["md"] = p
        else:
            g["stderr"] = p
    if not groups:
        return None
    for g in groups.values():
        if g["started"] is None:
            g["started"] = g["first_seen"]
        del g["first_seen"]
    return max(groups.values(), key=lambda g: (g["started"], g["n"]))


def find_latest_agent_log(logs_dir: Path, agent: str, task_ids=None) -> Optional[Path]:
    """Newest worker output log for an agent, including completed tasks."""
    try:
        entries = list(Path(logs_dir).iterdir())
    except OSError:
        return None
    pat = re.compile(r"^(?P<task>.+)-" + re.escape(agent) + r"-\d+\.md$")
    allowed = set(str(tid) for tid in task_ids or [])
    candidates = []
    for path in entries:
        match = pat.match(path.name)
        if not match:
            continue
        task_id = match.group("task")
        if task_ids is not None and task_id not in allowed:
            continue
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def find_active_review(logs_dir: Path, tasks: List[Dict], alive: bool, known_agents=None) -> Optional[Dict]:
    """Find the newest lead-review prompt for a still-running task, if any."""
    if not alive:
        return None
    try:
        entries = list(Path(logs_dir).iterdir())
    except OSError:
        return None
    pattern = re.compile(r"^lead-\d+-review-(?P<agent>.+)\.prompt\.md$")
    by_id = {str(t.get("id") or "?"): t for t in tasks if isinstance(t, dict)}
    candidates = []
    for prompt in entries:
        match = pattern.match(prompt.name)
        if not match:
            continue
        try:
            started = prompt.stat().st_mtime
            contents = prompt.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        task_match = re.search(r"^TASK\s+([^\s:]+):", contents, re.M)
        if not task_match:
            continue
        task_id = task_match.group(1)
        task = by_id.get(task_id)
        if not task or task.get("status") != "running":
            continue
        worker_log = find_task_log(Path(logs_dir), task_id, known_agents)
        if worker_log and worker_log.get("started") and worker_log["started"] > started:
            continue
        output = prompt.with_name(prompt.name[:-len(".prompt.md")] + ".md")
        candidates.append((started, {"agent": match.group("agent"), "task_id": task_id,
                                     "task_title": str(task.get("title") or task_id),
                                     "builder": str(task.get("agent_used") or task.get("assignee") or ""),
                                     "output": output}))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _task_blockers(task: Dict, statuses: Dict[str, str], interrupted: bool) -> List[str]:
    status = str(task.get("status") or "pending")
    note = str(task.get("review_note") or "").strip()
    if status == "failed":
        if not note:
            attempts = task.get("attempts") if isinstance(task.get("attempts"), list) else []
            last = attempts[-1] if attempts and isinstance(attempts[-1], dict) else {}
            result = str(last.get("result") or "")
            note = result if result and result != "approved" else "Task failed; inspect its logs"
        return [note]
    if status == "skipped":
        return [note or "Task was skipped"]
    if status == "running" and interrupted:
        return ["Run was interrupted while this task was running"]
    if status == "pending":
        deps = task.get("depends_on") if isinstance(task.get("depends_on"), list) else []
        waiting = [str(dep) for dep in deps if statuses.get(str(dep)) != "done"]
        if waiting:
            return ["Waiting for " + ", ".join(waiting)]
    return []


def tail_lines(path: Optional[Path], count: int = TAIL_LINES, max_bytes: int = 16384) -> List[str]:
    """Last `count` non-empty lines of a (possibly huge) text file, cleaned of escape codes."""
    if not path:
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return []
    lines = [_clean(l).rstrip() for l in data.decode("utf-8", errors="replace").splitlines()]
    return [l for l in lines if l.strip()][-count:]


# --------------------------------------------------------------------------- snapshot

def build_snapshot(project_dir, state: Dict, now: Optional[float] = None) -> Dict:
    """Everything `status` shows, as a dict. Keys starting with '_' are display-only (not in --json)."""
    now = time.time() if now is None else now
    d = Path(project_dir).expanduser().resolve()
    sdir = project_state_dir(d)
    logs_dir = sdir / "logs"

    phase = str(state.get("phase") or "unknown")
    pid = _int(state.get("pid"))
    # the conductor records its pid from the start of planning, so a planning run is alive too
    alive = phase in ("planning", "running") and runs.pid_alive(pid)
    interrupted = phase in ("planning", "running") and not alive
    lead = str(state.get("lead") or "")
    workers = [str(w) for w in state.get("workers") or []] if isinstance(state.get("workers"), list) else []

    raw_tasks = [t for t in (state.get("tasks") or []) if isinstance(t, dict)] \
        if isinstance(state.get("tasks"), list) else []
    task_statuses = {str(t.get("id") or "?"): str(t.get("status") or "pending") for t in raw_tasks}
    raw_by_id = {str(t.get("id") or "?"): t for t in raw_tasks}
    known = set(workers) | ({lead} if lead else set())
    for t in raw_tasks:
        known.update(str(x) for x in (t.get("assignee"), t.get("agent_used")) if x)
        for a in t.get("attempts") or []:
            if isinstance(a, dict) and a.get("agent"):
                known.add(str(a["agent"]))

    counts = {k: 0 for k in COUNT_KEYS}
    tasks: List[Dict] = []
    live: Dict[str, Dict] = {}
    for t in raw_tasks:
        tid = str(t.get("id") or "?")
        status = str(t.get("status") or "pending")
        if status in counts:
            counts[status] += 1
        attempts = t.get("attempts") if isinstance(t.get("attempts"), list) else []
        worker = str(t.get("agent_used") or t.get("assignee") or "")
        duration = _float(t.get("duration_sec")) or 0.0
        if status == "running":
            log = find_task_log(logs_dir, tid, known)
            info = {"agent": worker, "attempt": len(attempts) + 1, "elapsed": None, "lines": [], "source": ""}
            if log:
                worker = log["agent"]
                info["agent"] = log["agent"]
                info["attempt"] = log["n"]
                if alive and log["started"]:
                    duration = max(0.0, now - log["started"])
                    info["elapsed"] = duration
                lines = tail_lines(log["md"])
                source = "output"
                if not lines:
                    lines = tail_lines(log["stderr"])
                    source = "stderr" if lines else ""
                info["lines"], info["source"] = lines, source
            live[tid] = info
        tasks.append({
            "id": tid,
            "title": str(t.get("title") or tid),
            "worker": worker,
            "status": status,
            "tries": len(attempts),
            "duration_sec": round(duration, 1),
            "commit": str(t.get("commit") or ""),
        })
    counts["total"] = len(tasks)
    active_review = find_active_review(logs_dir, raw_tasks, alive, known)

    # Agent summaries combine task state with the newest transcript line. Keep the
    # configured lead/workers first, then include agents found in task history.
    agent_order = list(dict.fromkeys(([lead] if lead else []) + workers + sorted(known)))
    agents: List[Dict] = []
    for agent in agent_order:
        assigned = [t for t in tasks if t["worker"] == agent]
        current = [t for t in assigned if t["status"] == "running"]
        done = [t for t in assigned if t["status"] == "done"]
        progress = {key: sum(1 for t in assigned if t["status"] == key)
                    for key in ("done", "running", "pending", "failed", "skipped")}
        progress["total"] = len(assigned)
        blockers = [{"task_id": t["id"], "reason": reason}
                    for t in assigned
                    for reason in _task_blockers(raw_by_id.get(t["id"], {}), task_statuses, interrupted)]

        latest_output = ""
        reviewing_task = None
        if active_review and active_review["agent"] == agent:
            reviewing_task = {key: active_review[key] for key in ("task_id", "task_title", "builder")}
            output_lines = tail_lines(active_review["output"], 1)
            latest_output = output_lines[-1] if output_lines else "Reviewing the worker's changes"
        elif current:
            live_info = live.get(current[-1]["id"], {})
            lines = live_info.get("lines") or []
            if lines:
                latest_output = lines[-1]
            elif agent == "human":
                latest_output = "Waiting for your input in the run terminal"
        if not latest_output:
            allowed_log_tasks = []
            for raw in raw_tasks:
                tid = str(raw.get("id") or "?")
                attempts = raw.get("attempts") if isinstance(raw.get("attempts"), list) else []
                ran_as_agent = any(isinstance(a, dict) and str(a.get("agent") or "") == agent for a in attempts)
                if ran_as_agent or (raw.get("status") == "running" and
                                    any(t["id"] == tid for t in assigned)):
                    allowed_log_tasks.append(tid)
            latest_log = find_latest_agent_log(logs_dir, agent, allowed_log_tasks)
            output_lines = tail_lines(latest_log, 1)
            latest_output = output_lines[-1] if output_lines else ""

        if current:
            agent_status = "working" if alive else "interrupted"
        elif reviewing_task:
            agent_status = "reviewing"
        elif agent == lead and alive and phase == "planning":
            agent_status = "planning"      # the lead is writing the plan
        elif agent == lead and alive and sum(counts.values()) and not counts.get("running") \
                and not counts.get("pending"):
            agent_status = "checking"      # every task finished: the lead runs the final project check
        elif blockers:
            agent_status = "blocked"
        elif progress["pending"]:
            agent_status = "queued"
        elif done:
            agent_status = "done"
        else:
            agent_status = "idle"

        agents.append({
            "id": agent,
            "role": "lead" if agent == lead else "worker",
            "status": agent_status,
            "progress": progress,
            "current_tasks": [{"id": t["id"], "title": t["title"]} for t in current],
            "latest_output": latest_output,
            "reviewing_task": reviewing_task,
            "blockers": blockers,
            "completed_work": [{"id": t["id"], "title": t["title"],
                                "summary": str(raw_by_id.get(t["id"], {}).get("summary") or "")}
                               for t in done],
        })

    final = state.get("final") if isinstance(state.get("final"), dict) and state.get("final") else None
    report = sdir / "REPORT.md"
    return {
        "dir": str(d),
        "project_name": str(state.get("project_name") or ""),
        "goal": str(state.get("goal") or ""),
        "phase": phase,
        "alive": alive,
        "interrupted": interrupted,
        "pid": pid,
        "lead": lead,
        "workers": workers,
        "round": _int(state.get("round"), 1),
        "started_at": _float(state.get("started_at")),
        "updated_at": _float(state.get("updated_at")),
        "counts": counts,
        "tasks": tasks,
        "agents": agents,
        "final": final,
        "report": str(report) if report.is_file() else None,
        "_live": live,
        "_logs": str(logs_dir),
    }


def to_json(snap: Dict) -> Dict:
    return {k: v for k, v in snap.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- rendering

def phase_badge(phase: str) -> str:
    text, styles = _PHASES.get(phase, (f"? {phase.upper()}", (Colors.BOLD,)))
    return colorize(f"[{text}]", *styles)


def progress_bar(counts: Dict, width: int = BAR_WIDTH, interrupted: bool = False) -> str:
    total, done = counts.get("total", 0), counts.get("done", 0)
    if total <= 0:
        return "[" + c_dim("-" * width) + "] no tasks planned yet"
    filled = int(round(width * done / float(total)))
    if done and not filled:
        filled = 1
    if done < total:
        filled = min(filled, width - 1)
    bar = "[" + c_green("#" * filled) + c_dim("-" * (width - filled)) + "]"
    parts = [f"{done}/{total} done"]
    if counts.get("failed"):
        parts.append(c_red(f"{counts['failed']} failed"))
    if counts.get("running"):
        parts.append(c_yellow(f"{counts['running']} {'interrupted' if interrupted else 'running'}"))
    if counts.get("skipped"):
        parts.append(c_dim(f"{counts['skipped']} skipped"))
    return bar + " " + " · ".join(parts)


def _table_lines(headers: List[str], rows: List[List[str]]) -> List[str]:
    """Like ui.print_table but measures visible width, so mixed colours stay aligned."""
    widths = [_vlen(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            widths[i] = max(widths[i], _vlen(cell))
    last = len(headers) - 1
    out = ["  " + "  ".join(colorize(h if i == last else _pad(h, widths[i]), Colors.BOLD, Colors.BRIGHT_WHITE)
                            for i, h in enumerate(headers)),
           colorize("  " + "  ".join("─" * w for w in widths), Colors.DIM)]
    for r in rows:
        out.append(("  " + "  ".join(_pad(cell, widths[i]) for i, cell in enumerate(r))).rstrip())
    return out


def _label(name: str) -> str:
    return c_dim(f"{name:<9}")


def _wrap(text: str, width: int, indent: int = 11, max_lines: int = 6) -> List[str]:
    text = " ".join(_clean(text).split())
    if not text:
        return []
    return textwrap.wrap(text, width=max(20, width - indent), max_lines=max_lines, placeholder=" …")


def render(snap: Dict, width: Optional[int] = None, now: Optional[float] = None) -> str:
    width = width or _term_width()
    now = time.time() if now is None else now
    d = snap["dir"]
    pad = " " * 11
    L: List[str] = [""]

    name = snap["project_name"] or Path(d).name
    L.append(f"  {c_bold(c_cyan(name))}  {c_dim(d)}")
    goal = _wrap(snap["goal"], width)
    if goal:
        L.append(f"  {_label('Goal')}{goal[0]}")
        L += [pad + g for g in goal[1:]]

    phase = snap["phase"]
    if snap["alive"]:
        liveness = c_green("● live") + f" (pid {snap['pid']})"
    elif snap["interrupted"]:
        liveness = c_red("interrupted") + " — continue with " + c_bold(f"omnicli resume {_q(d)}")
    elif phase in ("planned", "stopped"):
        liveness = c_dim(f"continue with `omnicli resume {_q(d)}`")
    else:
        liveness = ""
    L.append(f"  {_label('Phase')}{phase_badge(phase)}" + (f"  {liveness}" if liveness else ""))

    workers = ", ".join(c_cyan(w) for w in snap["workers"]) or c_dim("-")
    L.append(f"  {_label('Lead')}{c_magenta(snap['lead'] or '-')}   {c_dim('Workers')} {workers}"
             f"   {c_dim('Round')} {snap['round']}")
    L.append(f"  {_label('Time')}started {fmt_ago(snap['started_at'], now)} · "
             f"updated {fmt_ago(snap['updated_at'], now)}")
    L.append("")
    L.append(f"  {progress_bar(snap['counts'], interrupted=snap['interrupted'])}")

    tasks = snap["tasks"]
    if tasks:
        L.append("")
        cells = []
        for t in tasks:
            status = t["status"]
            if status == "running" and not snap["alive"]:
                status_txt = c_red("interrupted")
                time_txt = "-"
            else:
                status_txt = _STATUS_COLOR.get(status, lambda s: s)(status)
                time_txt = fmt_duration(t["duration_sec"]) if t["duration_sec"] else "-"
            cells.append([t["id"], t["title"], c_cyan(t["worker"] or "-"), status_txt, str(t["tries"]),
                          time_txt, t["commit"][:7] or "-"])
        other = sum(max(len(h), max(_vlen(r[i]) for r in cells)) for i, h in
                    enumerate(["ID", "", "Worker", "Status", "Tries", "Time", "Commit"]) if i != 1)
        title_w = max(12, min(60, width - other - 2 - 2 * 6))
        for r in cells:
            r[1] = _trunc(r[1], title_w)
        L += _table_lines(["ID", "Task", "Worker", "Status", "Tries", "Time", "Commit"], cells)

    agents = snap.get("agents") or []
    if agents:
        L.append("")
        L.append(f"  {c_bold('Agents')}")
        state_color = {"working": c_yellow, "reviewing": c_magenta, "interrupted": c_red, "blocked": c_red,
                       "queued": c_cyan, "done": c_green, "idle": c_dim}
        for agent in agents:
            progress = agent["progress"]
            progress_txt = f"{progress['done']}/{progress['total']} tasks done"
            if progress["running"]:
                progress_txt += f" · {progress['running']} active"
            if progress["pending"]:
                progress_txt += f" · {progress['pending']} queued"
            status_fn = state_color.get(agent["status"], c_dim)
            L.append(f"  {c_cyan(agent['id'])} ({agent['role']}) · {status_fn(agent['status'])} · "
                     f"{c_dim(progress_txt)}")

            current = agent["current_tasks"]
            if current:
                current_txt = ", ".join(f"{t['id']} {t['title']}" for t in current)
                L.append(f"    Current: {_trunc(current_txt, width - 15)}")
            if agent.get("reviewing_task"):
                review = agent["reviewing_task"]
                L.append(f"    Reviewing: {review['task_id']} {review['task_title']} by {review['builder'] or '?'}")
            elif agent["status"] == "queued":
                L.append("    Current: waiting for a task or dependency")

            if agent["latest_output"]:
                L.append(f"    Latest: {_trunc(agent['latest_output'], width - 13)}")
            if agent["blockers"]:
                for blocker in agent["blockers"]:
                    reason = _trunc(blocker["reason"], width - 21)
                    L.append(f"    Blocker ({blocker['task_id']}): {reason}")
            completed = agent["completed_work"]
            for item in completed[:3]:
                summary = " ".join(_clean(item["summary"]).split())
                title = _trunc(item["title"], width - len(item["id"]) - 22)
                detail = (" — " + _trunc(summary, width - len(item["id"]) - len(title) - 25)) if summary else ""
                L.append(f"    Done: {item['id']} {title}{detail}")
            if len(completed) > 3:
                L.append(f"    Done: and {len(completed) - 3} more task(s)")

    live = snap.get("_live") or {}
    for tid, info in live.items():
        L.append("")
        if info.get("elapsed") is not None:
            st = "running " + fmt_duration(info["elapsed"])
        else:
            st = "running" if snap["alive"] else "interrupted"
        head = f"  {c_yellow('▸')} {c_bold(tid)} · {c_cyan(info['agent'] or '?')} · attempt {info['attempt']} · {st}"
        if info.get("source") == "stderr":
            head += c_dim("  (stderr)")
        L.append(head)
        if info["lines"]:
            for line in info["lines"]:
                L.append("    " + c_dim("│ " + _trunc(line, width - 8)))
        else:
            L.append("    " + c_dim("│ (no output yet)"))

    final = snap.get("final")
    if final:
        L.append("")
        complete = bool(final.get("complete"))
        L.append(f"  {_label('Final')}" + (c_green("complete: yes ✓") if complete else c_yellow("complete: no")))
        summary = " ".join(_clean(str(final.get("summary") or "")).split())
        if summary:
            if len(summary) > SUMMARY_CHARS:
                summary = summary[:SUMMARY_CHARS - 1].rstrip() + "…"
            wrapped = _wrap(summary, width, max_lines=8)
            L.append(f"  {_label('Summary')}{wrapped[0]}")
            L += [pad + w for w in wrapped[1:]]
        issues = final.get("remaining_issues")
        n_issues = len(issues) if isinstance(issues, list) else (1 if issues else 0)
        L.append(f"  {_label('Issues')}" + (c_yellow(f"{n_issues} remaining") if n_issues else c_green("none remaining")))
        L.append(f"  {_label('Report')}" + (snap["report"] if snap.get("report") else c_dim("(not written yet)")))

    L.append("")
    L.append(c_dim(f"  Logs: {snap.get('_logs', '')}"))
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- command

def _parse_watch(args) -> Tuple[Optional[float], str]:
    """(seconds or None, error). A non-number after --watch is treated as REF."""
    raw = getattr(args, "watch", None)
    if raw is None:
        return None, ""
    secs = _float(raw)
    if secs is None:
        if isinstance(raw, str) and not getattr(args, "ref", None) and raw.lower() not in ("nan", "inf", "-inf"):
            args.ref = raw
            secs = DEFAULT_WATCH_SECONDS
        else:
            return None, f"--watch expects a number of seconds, got '{raw}'. Example: omnicli status --watch 5"
    if secs <= 0:
        return None, f"--watch needs a positive number of seconds, got '{raw}'. Example: omnicli status --watch 5"
    return max(secs, MIN_WATCH_SECONDS), ""


def run(args, out=None, err=None, max_frames: Optional[int] = None,
        sleep: Callable[[float], None] = time.sleep, cwd: Optional[str] = None,
        width: Optional[int] = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    interval, problem = _parse_watch(args)
    if problem:
        print(problem, file=err)
        return 2

    project_dir, problem = resolve_run(getattr(args, "ref", None), getattr(args, "dir", None), cwd=cwd)
    if project_dir is None:
        print(problem, file=err)
        return 1

    as_json = bool(getattr(args, "json", False))
    is_tty = bool(getattr(out, "isatty", lambda: False)())
    frames = 0
    try:
        while True:
            state = runs.load_state(project_dir)
            if state is None:
                print(f"Could not read {project_state_dir(project_dir) / 'state.json'} (missing or corrupt).\n"
                      f"  List runs with: omnicli runs", file=err)
                return 1
            snap = build_snapshot(project_dir, state)
            if as_json:
                # one object; with --watch, one compact object per line (JSON Lines)
                out.write(json.dumps(to_json(snap), indent=None if interval else 2) + "\n")
            elif interval and is_tty:
                out.write(_CLEAR_SCREEN + render(snap, width) +
                          c_dim(f"  watching every {interval:g}s · Ctrl-C to stop") + "\n")
            else:
                if frames:
                    stamp = datetime.datetime.now().strftime("%H:%M:%S")
                    w = width or _term_width()
                    out.write(c_dim(f"── {stamp} " + "─" * max(4, w - 12)) + "\n")
                out.write(render(snap, width))
            out.flush()
            frames += 1
            if not interval or not snap["alive"]:
                if interval and not as_json:
                    why = "the run was interrupted" if snap["interrupted"] else f"phase is {snap['phase']}"
                    out.write(c_dim(f"  watch ended: {why}") + "\n")
                return 0
            if max_frames is not None and frames >= max_frames:
                return 0
            sleep(interval)
    except KeyboardInterrupt:
        try:
            out.write("\n")
            out.flush()
        except Exception:
            pass
        return 0
