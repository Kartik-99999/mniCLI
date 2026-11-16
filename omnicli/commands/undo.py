"""
`omnicli undo [TASK]` (alias `rewind`) - rewind one team task, like Claude Code's /rewind.

Every task an `omnicli team` run finishes is checkpointed as its own git commit. Undo
reverts that commit with `git revert` (history is kept - nothing is reset or forced),
then marks the task pending again in <dir>/.omnicli/team/state.json and sets the run to
"stopped", so `omnicli resume <dir>` redoes it (optionally with another agent or a note).

    omnicli undo [TASK] [-d DIR | --run REF] [--list] [--agent NAME] [--note TEXT] [-y] [--json]

Without TASK (or with --list) it shows the checkpoints that can be undone.

Safety: it refuses while the run is live, outside a git work tree, for unknown tasks,
for tasks without a checkpoint, for checkpoints not in HEAD's history, and when the
project has uncommitted changes outside .omnicli/. A revert conflict is aborted so the
repo and state are left exactly as they were.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from omnicli import paths, runs
from omnicli.ui import (
    BADGE_FAILED,
    BADGE_INFO,
    BADGE_SUCCESS,
    BADGE_WARN,
    c_bold,
    c_cyan,
    c_dim,
    c_green,
    c_red,
    c_yellow,
    print_table,
)

NAMES = ["undo", "rewind"]

# Same fallback identity conductor.py uses when the repo has none configured.
FALLBACK_IDENT = ["-c", "user.name=OmniCLI", "-c", "user.email=omnicli@localhost"]
NOTE_PREFIX = "\n\nNOTE FROM THE USER (after undo): "


# --------------------------------------------------------------------------- parser

def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "undo", aliases=["rewind"],
        help="Rewind a team task: git-revert its checkpoint and mark it pending to redo",
        description="Revert the git checkpoint of one `omnicli team` task (history is kept), mark the "
                    "task pending and the run stopped, so `omnicli resume <dir>` redoes it. "
                    "Without TASK, list the checkpoints.")
    p.add_argument("task", nargs="?", metavar="TASK", help="Task id to undo (omit to list checkpoints)")
    where = p.add_mutually_exclusive_group()
    where.add_argument("-d", "--dir", dest="dir", default=None, help="Project directory of the team run")
    where.add_argument("--run", dest="run", default=None, metavar="REF",
                       help="Run reference from `omnicli runs` (index, path, or name substring)")
    p.add_argument("--list", action="store_true", help="List task checkpoints (default when no TASK)")
    p.add_argument("--agent", default=None, metavar="NAME", help="Reassign the undone task to this agent for the redo")
    p.add_argument("--note", default=None, metavar="TEXT", help="Add a note to the task's instructions for the redo")
    p.add_argument("-y", "--yes", action="store_true", help="Don't ask for confirmation")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p.set_defaults(func=handler)


# --------------------------------------------------------------------------- helpers

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=env)
    except OSError as ex:  # git vanished / cwd missing
        return subprocess.CompletedProcess(["git", *args], 127, "", str(ex))


def _emit_json(data: Dict) -> None:
    print(json.dumps(data, indent=2))


def _fail(as_json: bool, msg: str, details: Optional[List[str]] = None, **extra) -> int:
    if as_json:
        _emit_json(dict({"ok": False, "error": msg}, **extra))
    else:
        print(f"{BADGE_FAILED} {msg}")
        for line in details or []:
            print(f"  {line}")
    return 1


def _quote(d: Path) -> str:
    return shlex.quote(str(d))


def _short(commit: str) -> str:
    return (commit or "")[:7]


def resolve_project(args) -> Tuple[Optional[Path], str]:
    """Which run: -d DIR, else --run REF, else cwd (if it has a team run), else the most recent run."""
    explicit = getattr(args, "dir", None)
    if explicit:
        d = Path(explicit).expanduser().resolve()
        if not (paths.project_state_dir(d) / "state.json").exists():
            return None, (f"No team run in {d} (no .omnicli/team/state.json). "
                          f"See your runs with: omnicli runs")
        return d, ""
    ref = getattr(args, "run", None)
    if ref:
        rec = runs.find_run(ref)
        if not rec:
            return None, f"No run matches '{ref}'. See your runs with: omnicli runs"
        return Path(rec["dir"]), ""
    cwd = Path.cwd().resolve()
    if (paths.project_state_dir(cwd) / "state.json").exists():
        return cwd, ""
    rec = runs.most_recent_run()
    if rec:
        return Path(rec["dir"]), ""
    return None, ("No team run found here or in the run registry. "
                  "Start one with: omnicli team \"<goal>\" -d DIR")


def is_live(state: Dict) -> bool:
    try:
        pid = int(state.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    return state.get("phase") == "running" and runs.pid_alive(pid)


def git_work_tree(d: Path) -> bool:
    if not shutil.which("git"):
        return False
    r = _git(d, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def resolve_commit(d: Path, commit: str) -> str:
    """Full hash of a stored (usually short) checkpoint hash, or '' if it is not in the repo."""
    from omnicli.gitutil import safe_rev
    commit = safe_rev(commit)
    if not commit:  # missing, or not a plain commit id
        return ""
    r = _git(d, "rev-parse", "--verify", "-q", f"{commit}^{{commit}}")
    return r.stdout.strip() if r.returncode == 0 else ""


def in_head(d: Path, full: str) -> bool:
    return bool(full) and _git(d, "merge-base", "--is-ancestor", full, "HEAD").returncode == 0


def dirty_files(d: Path) -> List[str]:
    """Uncommitted changes (incl. untracked) in the project dir, ignoring .omnicli/."""
    r = _git(d, "status", "--porcelain", "--untracked-files=all", "--", ".", ":(exclude).omnicli")
    if r.returncode == 0:
        return [ln for ln in r.stdout.splitlines() if ln.strip()]
    # Fallback for gits without :(exclude) pathspec magic: filter in Python.
    # Porcelain paths are relative to the repo root, so strip the project's prefix.
    prefix = _git(d, "rev-parse", "--show-prefix").stdout.strip()
    r = _git(d, "status", "--porcelain", "--untracked-files=all", "--", ".")
    out = []
    for ln in r.stdout.splitlines():
        if not ln.strip():
            continue
        path = ln[3:].split(" -> ")[-1].strip().strip('"')
        if prefix and path.startswith(prefix):
            path = path[len(prefix):]
        if path == ".omnicli" or path.startswith(".omnicli/"):
            continue
        out.append(ln)
    return out


def find_task(tasks: List[Dict], tid: str) -> Optional[Dict]:
    for t in tasks:
        if str(t.get("id")) == tid:
            return t
    low = tid.lower()
    for t in tasks:
        if str(t.get("id", "")).lower() == low:
            return t
    return None


def dependents(tasks: List[Dict], tid: str) -> List[Dict]:
    """Tasks depending on tid directly or transitively, as [{task, via}] in breadth-first order."""
    children: Dict[str, List[Dict]] = {}
    for t in tasks:
        for dep in t.get("depends_on") or []:
            children.setdefault(str(dep), []).append(t)
    seen = {tid}
    out: List[Dict] = []
    queue = [tid]
    while queue:
        cur = queue.pop(0)
        for ch in children.get(cur, []):
            cid = str(ch.get("id"))
            if cid in seen:
                continue
            seen.add(cid)
            out.append({"task": ch, "via": "" if cur == tid else cur})
            queue.append(cid)
    return out


def _known_agents() -> List[str]:
    try:
        from omnicli import agents as ag  # lazy: keep this command importable on its own
        with contextlib.redirect_stdout(sys.stderr):
            return list(ag.load_agent_specs().keys())
    except Exception:
        return []


def _status_colored(status: str) -> str:
    return {"done": c_green, "failed": c_red, "running": c_yellow,
            "skipped": c_dim}.get(status, lambda s: s)(status)


def _last_undo(task: Dict) -> Optional[Dict]:
    for a in reversed(task.get("attempts") or []):
        if isinstance(a, dict) and str(a.get("result", "")).startswith("undone"):
            return a
    return None


def _save_state(d: Path, state: Dict) -> None:
    state_path = paths.project_state_dir(d) / "state.json"
    tmp = state_path.with_name(f".state.undo-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(state_path)


def _register(d: Path, state: Dict) -> None:
    tasks = state.get("tasks") or []
    runs.register_run({
        "dir": str(d),
        "goal": state.get("goal", ""),
        "project_name": state.get("project_name", ""),
        "phase": state.get("phase", "stopped"),
        "lead": state.get("lead", ""),
        "workers": list(state.get("workers") or []),
        "tasks_total": len(tasks),
        "tasks_done": sum(1 for t in tasks if t.get("status") == "done"),
        "tasks_failed": sum(1 for t in tasks if t.get("status") == "failed"),
        "round": state.get("round", 1),
        "started_at": state.get("started_at", time.time()),
        "pid": 0,
    })


# --------------------------------------------------------------------------- list

def checkpoints(d: Path, state: Dict, git_ok: bool) -> List[Dict]:
    rows = []
    for t in state.get("tasks") or []:
        commit = str(t.get("commit") or "")
        full = resolve_commit(d, commit) if (git_ok and commit) else ""
        row = {
            "id": t.get("id", ""),
            "title": t.get("title", ""),
            "worker": t.get("agent_used") or t.get("assignee", ""),
            "status": t.get("status", ""),
            "commit": commit,
            "full_commit": full,
            "subject": _git(d, "log", "-1", "--format=%s", full).stdout.strip() if full else "",
            "exists": bool(full),
            "in_head": in_head(d, full) if full else False,
            "undoable": False,
        }
        row["undoable"] = row["in_head"]
        undo = _last_undo(t) if not commit else None
        if undo:
            row["undone"] = {"reverted_commit": undo.get("reverted_commit", ""),
                             "revert_commit": undo.get("revert_commit", ""), "at": undo.get("at", "")}
        rows.append(row)
    return rows


def cmd_list(d: Path, state: Dict, as_json: bool) -> int:
    git_ok = git_work_tree(d)
    rows = checkpoints(d, state, git_ok)
    live = is_live(state)
    if as_json:
        _emit_json({"ok": True, "dir": str(d), "project_name": state.get("project_name", ""),
                    "goal": state.get("goal", ""), "phase": state.get("phase", ""), "live": live,
                    "git": git_ok, "checkpoints": rows})
        return 0

    table = []
    for r in rows:
        if not r["commit"]:
            commit, head = c_dim("-"), c_dim("-")
            subject = ""
            if r.get("undone"):
                u = r["undone"]
                subject = c_dim(f"undone: {u['reverted_commit'] or '?'} reverted in {u['revert_commit'] or '?'}")
        elif not r["exists"]:
            commit, head, subject = r["commit"], c_red("missing"), c_dim("(not in this repo)")
        else:
            commit = _short(r["full_commit"])
            head = c_green("yes") if r["in_head"] else c_red("no")
            subject = r["subject"]
        table.append([c_bold(c_cyan(str(r["id"]))), str(r["title"])[:40], c_cyan(str(r["worker"])),
                      _status_colored(str(r["status"])), commit, subject[:60], head])
    name = state.get("project_name") or d.name
    print_table(["Task", "Title", "Worker", "Status", "Commit", "Subject", "In HEAD"], table,
                title=f"Task checkpoints - {name} ({state.get('phase', '?')})")
    if not git_ok:
        print(f"{BADGE_WARN} {d} is not a git work tree, so there are no checkpoints to undo.")
    if live:
        print(f"{BADGE_WARN} This run is still running (pid {state.get('pid')}); undo is blocked until it stops.")
    where = "" if d == Path.cwd().resolve() else f" -d {_quote(d)}"
    print(c_dim(f"Undo one with: omnicli undo <task>{where}"))
    return 0


# --------------------------------------------------------------------------- undo

def cmd_undo(d: Path, state: Dict, args, as_json: bool) -> int:
    tid = str(args.task).strip()

    # 1. never touch a live run
    if is_live(state):
        return _fail(as_json, f"The team run in {d} is still running (pid {state.get('pid')}). "
                              f"Stop it first (Ctrl-C in its terminal), then retry: omnicli undo {tid}")

    # 2. needs git checkpoints
    if not shutil.which("git"):
        return _fail(as_json, "git is not installed, so checkpoints cannot be reverted.")
    if not git_work_tree(d):
        return _fail(as_json, f"{d} is not a git work tree, so OmniCLI made no checkpoints there to undo.")

    # 3. the task
    tasks = state.get("tasks") or []
    task = find_task(tasks, tid)
    valid = [str(t.get("id")) for t in tasks]
    if task is None:
        return _fail(as_json, f"No task '{tid}' in this run.",
                     [f"Valid task ids: {', '.join(valid) or '(none)'}",
                      "See them with: omnicli undo --list"], valid_ids=valid)
    tid = str(task.get("id"))
    commit = str(task.get("commit") or "")
    if not commit:
        why = {"failed": "it failed before a checkpoint", "skipped": "it was skipped",
               "pending": "it has not run yet", "running": "it has not finished"}.get(
            str(task.get("status")), "it has no checkpoint")
        prev = _last_undo(task)
        if prev and task.get("status") == "pending":
            why = (f"it was already undone - {prev.get('reverted_commit') or '?'} reverted in "
                   f"{prev.get('revert_commit') or '?'}; redo it with: omnicli resume {_quote(d)}")
        return _fail(as_json, f"{tid} has no checkpoint commit, so there is nothing to undo ({why}).",
                     task=tid, status=task.get("status", ""))

    # 4. the checkpoint must be reachable from HEAD
    full = resolve_commit(d, commit)
    if not full:
        return _fail(as_json, f"Checkpoint {commit} for {tid} is not in this repository any more.",
                     task=tid, commit=commit)
    if not in_head(d, full):
        return _fail(as_json, f"Checkpoint {commit} for {tid} is not in HEAD's history - is another "
                              f"branch checked out? Switch back to the branch the team run used, then retry.",
                     task=tid, commit=commit)

    # 5. never mix the revert with the user's uncommitted work
    dirty = dirty_files(d)
    if dirty:
        return _fail(as_json, f"{d} has uncommitted changes; undo only runs on a clean tree.",
                     dirty[:10] + ([f"... and {len(dirty) - 10} more"] if len(dirty) > 10 else []) +
                     [f"Commit or stash them first (git stash -u), then retry: omnicli undo {tid}"],
                     dirty_files=dirty)

    short = _short(full)
    affected = [x for x in dependents(tasks, tid) if x["task"].get("status") == "done"]
    affected_json = [{"id": x["task"].get("id"), "title": x["task"].get("title", ""),
                      "via": x["via"], "commit": x["task"].get("commit", "")} for x in affected]
    agent_warning = ""
    if args.agent:
        known = _known_agents()
        if known and args.agent not in known:
            agent_warning = (f"'{args.agent}' is not a known agent ({', '.join(known)}); "
                             f"on resume the task goes to another worker. See: omnicli agents")

    # 6. show what will be undone, then confirm
    if not as_json:
        subject = _git(d, "log", "-1", "--format=%s", full).stdout.strip()
        print(f"\n{c_bold('Undo')} {c_bold(c_cyan(tid))} {task.get('title', '')}")
        print(c_dim(f"  reverts checkpoint {short}: {subject}"))
        stat = _git(d, "show", "--stat", "--format=%h %s", full).stdout.rstrip()
        if stat:
            print()
            for line in stat.splitlines():
                print(f"  {line}")
        if affected:
            print(f"\n{BADGE_WARN} These tasks build on {tid} and are already done - their work stays, "
                  f"but it may now be broken:")
            for x in affected:
                via = c_dim(f" (via {x['via']})") if x["via"] else ""
                print(f"    {c_cyan(str(x['task'].get('id')))}  {x['task'].get('title', '')}{via}")
        if args.agent:
            print(f"{BADGE_INFO} It will be reassigned to {c_cyan(args.agent)}.")
        if agent_warning:
            print(f"{BADGE_WARN} {agent_warning}")
        if args.note:
            print(f"{BADGE_INFO} Your note will be added to its instructions.")
        print()

    if not args.yes:
        if as_json:
            return _fail(True, "Confirmation needed: re-run with -y (--json cannot prompt).",
                         task=tid, would_revert=short, affected_dependents=affected_json)
        try:
            ans = input(f"Revert {tid} ({short}) with `git revert`? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
            print()
        if ans not in ("y", "yes"):
            print(f"{BADGE_INFO} Cancelled - nothing changed.")
            return 1

    # the run may have been started while we were asking
    fresh = runs.load_state(d) or state
    if is_live(fresh):
        return _fail(as_json, f"The team run in {d} started running meanwhile; nothing was changed.")

    # 7. revert (history kept, never reset/force)
    ident = [] if _git(d, "config", "user.email").stdout.strip() else FALLBACK_IDENT
    res = _git(d, *ident, "revert", "--no-edit", full)
    if res.returncode != 0:
        in_progress = _git(d, "rev-parse", "-q", "--verify", "REVERT_HEAD").returncode == 0
        conflicts = [f for f in _git(d, "diff", "--name-only", "--relative", "--diff-filter=U").stdout.splitlines()
                     if f.strip()]
        if in_progress:
            _git(d, "revert", "--abort")
        output = (res.stdout + "\n" + res.stderr).strip()
        if conflicts:
            later = [str(x["task"].get("id")) for x in reversed(affected) if x["task"].get("commit")]
            hint = (f"Tasks built on it ({', '.join(later)}) probably changed the same lines; undo them first "
                    f"(newest first), then retry." if later else
                    "A later commit changed the same lines; resolve that by hand or undo the later task first.")
            return _fail(as_json, f"Reverting {short} conflicts with later changes, so it was aborted - "
                                  f"the repo and the run are unchanged.",
                         ["Conflicting files: " + ", ".join(conflicts), hint],
                         task=tid, commit=short, conflicts=conflicts)
        unchanged = (not in_progress and not dirty_files(d)
                     and _git(d, "diff", "--cached", "--quiet").returncode == 0)
        if "nothing to commit" in output or "nothing added to commit" in output or unchanged:
            return _fail(as_json, f"Reverting {short} changes nothing - its changes are already gone from HEAD. "
                                  f"Nothing was changed.", task=tid, commit=short)
        return _fail(as_json, f"git revert {short} failed; nothing was changed.",
                     [ln for ln in output.splitlines()[-8:]], task=tid, commit=short, git_output=output)
    new_short = _git(d, "rev-parse", "--short", "HEAD").stdout.strip()

    # 8. mark the task pending and the run stopped (resumable)
    state = fresh
    task = find_task(state.get("tasks") or [], tid) or task
    task["status"] = "pending"
    for key in ("commit", "agent_used", "summary", "review_note"):
        task[key] = ""
    task["duration_sec"] = 0.0
    if not isinstance(task.get("attempts"), list):
        task["attempts"] = []
    task["attempts"].append({
        "agent": "", "result": f"undone (revert {short})",
        "at": datetime.datetime.now().isoformat(timespec="seconds"),
        "reverted_commit": short, "revert_commit": new_short,
    })
    added_worker = ""
    if args.agent:
        task["assignee"] = args.agent
        workers = state.get("workers")
        if isinstance(workers, list) and args.agent not in workers and not agent_warning:
            workers.append(args.agent)  # otherwise resume would hand the task to someone else
            added_worker = args.agent
    if args.note:
        task["instructions"] = str(task.get("instructions") or "") + NOTE_PREFIX + args.note
    state["phase"] = "stopped"
    state["pid"] = 0
    final = state.get("final") if isinstance(state.get("final"), dict) else {}
    final["complete"] = False
    state["final"] = final
    state["updated_at"] = time.time()
    try:
        _save_state(d, state)
    except Exception as ex:
        return _fail(as_json, f"Reverted {tid} in git ({new_short}) but could not update its state.json: {ex}",
                     task=tid, reverted_commit=short, new_commit=new_short)
    _register(d, state)

    resume_cmd = f"omnicli resume {_quote(d)}"
    if as_json:
        _emit_json({"ok": True, "dir": str(d), "task": tid, "title": task.get("title", ""),
                    "reverted_commit": short, "reverted_commit_full": full, "new_commit": new_short,
                    "assignee": task.get("assignee", ""), "added_worker": added_worker,
                    "note_added": bool(args.note), "affected_dependents": affected_json,
                    "phase": "stopped", "next": resume_cmd})
        return 0
    if added_worker:
        print(f"{BADGE_INFO} Added {c_cyan(added_worker)} to this run's workers.")
    print(f"{BADGE_SUCCESS} Reverted {c_bold(tid)} ({new_short}). Redo it with: {c_bold(resume_cmd)}")
    return 0


# --------------------------------------------------------------------------- entry

def handler(args) -> int:
    as_json = bool(getattr(args, "json", False))
    d, err = resolve_project(args)
    if d is None:
        return _fail(as_json, err)
    state = runs.load_state(d)
    if not isinstance(state, dict):
        return _fail(as_json, f"Could not read {paths.project_state_dir(d) / 'state.json'} (missing or corrupt).")
    if getattr(args, "list", False) or not getattr(args, "task", None):
        if not as_json and (getattr(args, "agent", None) or getattr(args, "note", None)):
            print(f"{BADGE_WARN} --agent/--note only apply when undoing a task: omnicli undo <task> --agent NAME")
        return cmd_list(d, state, as_json)
    return cmd_undo(d, state, args, as_json)
