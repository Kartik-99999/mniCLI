"""Compare saved team runs."""

import json
import subprocess
import sys

from omnicli import runs

NAMES = ["compare"]


def add_parser(subparsers):
    parser = subparsers.add_parser("compare", help="Compare task outcomes from two team runs")
    parser.add_argument("run_a", help="Run number, project path, or name/goal fragment")
    parser.add_argument("run_b", help="Second run number, project path, or name/goal fragment")
    parser.add_argument("--json", action="store_true", help="Print a JSON comparison")
    parser.set_defaults(func=handler)


def _state(ref):
    rec = runs.find_run(ref)
    if not rec:
        raise ValueError("no run matches {!r}; see `omnicli runs`".format(ref))
    data = runs.load_state(rec["dir"])
    if not isinstance(data, dict):
        raise ValueError("could not read saved state for {!r}".format(ref))
    return rec, data


def compare(ref_a, ref_b):
    rec_a, state_a = _state(ref_a)
    rec_b, state_b = _state(ref_b)
    tasks_a = {str(t.get("id")): t for t in state_a.get("tasks", []) if isinstance(t, dict)}
    tasks_b = {str(t.get("id")): t for t in state_b.get("tasks", []) if isinstance(t, dict)}
    rows = []
    for task_id in sorted(set(tasks_a) | set(tasks_b)):
        a, b = tasks_a.get(task_id, {}), tasks_b.get(task_id, {})
        rows.append({
            "id": task_id,
            "title": b.get("title") or a.get("title") or "",
            "status_a": a.get("status", "missing"),
            "status_b": b.get("status", "missing"),
            "worker_a": a.get("agent_used") or a.get("assignee") or "",
            "worker_b": b.get("agent_used") or b.get("assignee") or "",
            "commit_a": a.get("commit", ""),
            "commit_b": b.get("commit", ""),
            "verify_exit_a": _verify_exit(a),
            "verify_exit_b": _verify_exit(b),
            "diff_a": _commit_summary(rec_a["dir"], a.get("commit", "")),
            "diff_b": _commit_summary(rec_b["dir"], b.get("commit", "")),
            "summary_a": a.get("summary", ""),
            "summary_b": b.get("summary", ""),
        })
    return {
        "run_a": {"dir": rec_a["dir"], "phase": state_a.get("phase"), "goal": state_a.get("goal", ""),
                  "plan_summary": state_a.get("plan_summary", "")},
        "run_b": {"dir": rec_b["dir"], "phase": state_b.get("phase"), "goal": state_b.get("goal", ""),
                  "plan_summary": state_b.get("plan_summary", "")},
        "tasks": rows,
    }


def _verify_exit(task):
    attempts = task.get("attempts") or []
    for attempt in reversed(attempts):
        if isinstance(attempt, dict) and "verify_exit" in attempt:
            return attempt.get("verify_exit")
    return None


def _commit_summary(folder, commit):
    from omnicli.gitutil import safe_rev
    commit = safe_rev(commit)
    if not commit:  # missing, or not a plain commit id (never pass state values to git as options)
        return ""
    result = subprocess.run(["git", "show", "--stat", "--format=", "--end-of-options", commit], cwd=folder,
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def handler(args):
    try:
        result = compare(args.run_a, args.run_b)
    except (ValueError, KeyError, OSError) as ex:
        print("Compare error: {}".format(ex), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    a, b = result["run_a"], result["run_b"]
    print("Run A: {} ({})".format(a["dir"], a.get("phase") or "unknown"))
    print("Run B: {} ({})".format(b["dir"], b.get("phase") or "unknown"))
    if a.get("goal") != b.get("goal"):
        print("Goals differ:")
        print("  A: {}".format(a.get("goal", "")))
        print("  B: {}".format(b.get("goal", "")))
    if a.get("plan_summary") != b.get("plan_summary"):
        print("Plan A: {}".format(a.get("plan_summary", "") or "(none)"))
        print("Plan B: {}".format(b.get("plan_summary", "") or "(none)"))
    if not result["tasks"]:
        print("No task results are saved in these runs.")
        return 0
    print("\n{:<12} {:<10} {:<10} {:<8} {:<8} {:<14} {:<14} {}".format(
        "Task", "Run A", "Run B", "Verify A", "Verify B", "Worker A", "Worker B", "Title"))
    for row in result["tasks"]:
        print("{:<12} {:<10} {:<10} {:<8} {:<8} {:<14} {:<14} {}".format(
            row["id"][:12], row["status_a"], row["status_b"], str(row["verify_exit_a"]), str(row["verify_exit_b"]),
            row["worker_a"][:14], row["worker_b"][:14], row["title"]))
        for label in ("a", "b"):
            if row["diff_" + label]:
                print("  Diff {}: {}".format(label.upper(), " | ".join(row["diff_" + label].splitlines())))
    return 0
