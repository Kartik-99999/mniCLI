"""Show a task-by-task flight recorder for a saved team run."""

import json
import subprocess
from pathlib import Path

from omnicli import runs
from omnicli.ui import c_bold, c_cyan, c_dim, c_green, c_red

NAMES = ["timeline"]


def add_parser(subparsers):
    parser = subparsers.add_parser("timeline", help="Replay the saved task and review timeline of a run")
    parser.add_argument("ref", nargs="?", help="Run index, project path, or name (defaults to the latest run)")
    parser.add_argument("--json", action="store_true", help="Print timeline events as JSON")
    parser.add_argument("--diff", action="store_true", help="Show checkpoint diffs after the timeline")
    parser.set_defaults(func=handler)


def timeline_events(state):
    events = []
    for task in state.get("tasks", []):
        task_id = task.get("id", "?")
        for index, attempt in enumerate(task.get("attempts", []), 1):
            events.append({"at": attempt.get("at", ""), "task": task_id, "type": "attempt",
                           "agent": attempt.get("agent", ""), "result": attempt.get("result", ""),
                           "feedback": attempt.get("feedback", ""), "attempt": index,
                           "summary": attempt.get("summary", "")})
        if task.get("commit"):
            last_at = task.get("attempts", [])[-1].get("at", "") if task.get("attempts") else ""
            events.append({"at": last_at, "task": task_id, "type": "checkpoint",
                           "commit": task.get("commit"), "agent": task.get("agent_used", ""),
                           "summary": task.get("summary", "")})
        if not task.get("attempts"):
            events.append({"at": "", "task": task_id, "type": "task", "result": task.get("status", "pending"),
                           "agent": task.get("assignee", ""), "summary": task.get("title", "")})
    events.sort(key=lambda row: row.get("at", ""))
    return events


def _run_ref(ref):
    rec = runs.find_run(ref or "")
    if not rec:
        return None, None
    return rec, runs.load_state(rec["dir"])


def handler(args):
    rec, state = _run_ref(getattr(args, "ref", None))
    if not rec or not state:
        print("Team run not found or its state is unreadable. Run `omnicli runs` to find a saved run.")
        return 1
    events = timeline_events(state)
    if getattr(args, "json", False):
        print(json.dumps({"run": rec, "events": events}, indent=2, ensure_ascii=False))
    else:
        print(f"{c_bold('Run:')} {state.get('project_name') or Path(rec['dir']).name}  {c_dim(rec['dir'])}")
        for event in events:
            at = event.get("at") or "time unavailable"
            label = event.get("type", "task")
            result = event.get("result") or event.get("commit") or ""
            color = c_green if "approv" in str(result).lower() or label == "checkpoint" else (
                c_red if "fail" in str(result).lower() or "revise" in str(result).lower() else c_cyan)
            print("{}  {}  {}  {}  {}".format(c_dim(at), c_bold(event.get("task", "?")),
                                                event.get("agent", ""), label, color(str(result))))
            detail = event.get("feedback") or event.get("summary")
            if detail:
                print("    " + c_dim(str(detail).replace("\n", " ")[:240]))
        if not events:
            print("No task attempts have been recorded yet.")
        print(c_dim("Open prompts and outputs with `omnicli logs <task> --run {}`.".format(rec["dir"])))
    if getattr(args, "diff", False):
        directory = Path(rec["dir"])
        for task in state.get("tasks", []):
            commit = task.get("commit")
            if not commit:
                continue
            result = subprocess.run(["git", "show", "--format=fuller", "--stat", "--patch", commit],
                                    cwd=str(directory), capture_output=True, text=True)
            if result.returncode == 0:
                print("\n" + result.stdout)
            else:
                print("{}: checkpoint {} is unavailable in this project".format(task.get("id", "?"), commit))
    return 0
