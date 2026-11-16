"""
`omnicli runs`, `omnicli resume`, `omnicli continue` - find and pick up team runs,
like Claude Code's `--resume` picker and `-c/--continue`.

    omnicli runs [--all] [--json] [--prune]     list runs on this machine, newest first
    omnicli resume [REF] [-y] [options]         REF = # from `omnicli runs`, a path, or part of a name/goal;
                                                no REF: pick from unfinished runs (or the latest if not a TTY)
    omnicli continue [-y] [options]             resume the most recent unfinished run

The run registry (omnicli/runs.py) finds runs; each run's own state.json stays the source
of truth. Resuming builds the same TeamConfig as `omnicli team --resume`, with every
option you don't pass falling back to your saved settings (omnicli/settings.py).
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from omnicli import runs
from omnicli.paths import project_state_dir
from omnicli.ui import (
    BADGE_FAILED,
    BADGE_INFO,
    BADGE_WARN,
    Colors,
    c_bold,
    c_cyan,
    c_dim,
    c_green,
    c_red,
    c_yellow,
    colorize,
    print_panel,
)

NAMES = ["runs", "resume", "continue", "replay", "fork"]


def _default_conductor(cfg):
    from omnicli.conductor import Conductor  # imported lazily: keeps every other omnicli command fast
    return Conductor(cfg)


# Tests replace this to inject fake agent specs/health.
_make_conductor = _default_conductor


# --------------------------------------------------------------------------- parsers

def _add_run_options(p) -> None:
    """Options shared by `resume` and `continue` - the same set as `omnicli team` (omnicli/teamopts.py)."""
    from omnicli.teamopts import add_run_options
    add_run_options(p)
    p.add_argument("--plan-only", action="store_true", help="Show the saved plan without running it")


def add_parser(subparsers) -> None:
    p = subparsers.add_parser("runs", help="List team runs on this machine, newest first")
    p.add_argument("--all", action="store_true", help="Also show runs whose project folder is gone")
    p.add_argument("--json", action="store_true", help="Print the list as JSON")
    p.add_argument("--prune", action="store_true", help="Forget runs whose project folder is gone")
    p.set_defaults(func=handle_runs)

    r = subparsers.add_parser("resume", help="Pick up an unfinished team run (like claude --resume)")
    r.add_argument("ref", nargs="?", help="# from `omnicli runs`, a project path, or part of its name/goal")
    _add_run_options(r)
    r.set_defaults(func=handle_resume)

    c = subparsers.add_parser("continue", help="Continue the most recent unfinished team run (like claude -c)")
    _add_run_options(c)
    c.set_defaults(func=handle_continue)

    replay = subparsers.add_parser("replay", help="Run a saved plan again in a new project directory")
    replay.add_argument("ref", help="Source run number, project path, or name/goal fragment")
    replay.add_argument("-d", "--dir", required=True, help="New, empty destination directory")
    from omnicli.teamopts import add_run_options
    add_run_options(replay)
    replay.set_defaults(func=handle_replay)

    fork = subparsers.add_parser("fork", help="Branch a run before one task and retry it with another agent")
    fork.add_argument("ref", help="Source run index, project path, or name")
    fork.add_argument("task", help="Task id to retry, such as t3")
    fork.add_argument("--agent", required=True, help="Different AI CLI to try, such as muse")
    fork.add_argument("-d", "--dir", help="New worktree directory (defaults to a sibling of the project)")
    _add_run_options(fork)
    fork.set_defaults(func=handle_fork)


# --------------------------------------------------------------------------- formatting

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _vlen(s: str) -> int:
    return len(_ANSI.sub("", s))


def _table(headers: List[str], rows: List[List[str]], title: Optional[str] = None) -> None:
    """Like ui.print_table, but pads by visible width so coloured cells stay aligned."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _vlen(cell))
    if title:
        print(f"\n{c_bold(c_cyan(title))}")
    last = len(headers) - 1
    print("  " + "  ".join(colorize(h if i == last else h.ljust(widths[i]), Colors.BOLD, Colors.BRIGHT_WHITE)
                           for i, h in enumerate(headers)))
    print(c_dim("  " + "  ".join("─" * w for w in widths)))
    for row in rows:
        print("  " + "  ".join(cell + " " * (widths[i] - _vlen(cell)) for i, cell in enumerate(row)).rstrip())
    print()


def _ago(ts) -> str:
    try:
        secs = max(0.0, time.time() - float(ts))
    except (TypeError, ValueError):
        return "?"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return f"{int(secs // 86400)} d ago"


def _short_dir(d: str, limit: int = 60) -> str:
    try:
        home = str(Path.home())
    except Exception:
        home = ""
    if home and (d == home or d.startswith(home + "/")):
        d = "~" + d[len(home):]
    return d if len(d) <= limit else "…" + d[-(limit - 1):]


def _status(rec: Dict) -> str:
    """One word for what a run is doing: missing | live | interrupted | <phase>."""
    if not rec.get("exists", True):
        return "missing"
    if rec.get("phase") == "running":
        return "live" if rec.get("alive") else "interrupted"
    return rec.get("phase") or "unknown"


def _phase_cell(rec: Dict) -> str:
    st = _status(rec)
    if st == "missing":
        return c_dim("missing")
    if st == "live":
        return c_cyan("● running")
    if st == "interrupted":
        return c_yellow("interrupted")
    if st == "complete":
        return c_green(st)
    if st == "failed":
        return c_red(st)
    if st in ("stopped", "incomplete", "planned"):
        return c_yellow(st)
    return c_cyan(st)  # planning


def _tasks_cell(rec: Dict) -> str:
    cell = f"{int(rec.get('tasks_done') or 0)}/{int(rec.get('tasks_total') or 0)}"
    failed = int(rec.get("tasks_failed") or 0)
    return cell + (c_red(f" {failed}✗") if failed else "")


def _name(rec: Dict) -> str:
    return rec.get("project_name") or Path(rec["dir"]).name


def _row(idx: Optional[int], rec: Dict) -> List[str]:
    missing = not rec.get("exists", True)
    cells = [str(idx) if idx else "-", _name(rec), _phase_cell(rec), _tasks_cell(rec),
             rec.get("lead") or "-", _ago(rec.get("updated_at")), _short_dir(rec["dir"])]
    if missing:
        return [c_dim(_ANSI.sub("", x)) for x in cells]
    return [cells[0], c_bold(cells[1])] + cells[2:5] + [c_dim(cells[5]), c_dim(cells[6])]


_HEADERS = ["#", "Project", "Phase", "Tasks", "Lead", "Updated", "Dir"]


# --------------------------------------------------------------------------- omnicli runs

def _json_record(idx: Optional[int], rec: Dict) -> Dict:
    return {
        "index": idx,
        "dir": rec.get("dir"),
        "project_name": rec.get("project_name", ""),
        "goal": rec.get("goal", ""),
        "phase": rec.get("phase", ""),
        "status": _status(rec),
        "exists": bool(rec.get("exists", True)),
        "alive": bool(rec.get("alive")),
        "lead": rec.get("lead", ""),
        "workers": rec.get("workers", []),
        "tasks_total": int(rec.get("tasks_total") or 0),
        "tasks_done": int(rec.get("tasks_done") or 0),
        "tasks_failed": int(rec.get("tasks_failed") or 0),
        "round": rec.get("round"),
        "pid": rec.get("pid", 0),
        "started_at": rec.get("started_at"),
        "updated_at": rec.get("updated_at"),
    }


def _indexed(all_runs: List[Dict]) -> List[tuple]:
    """(index, record) pairs. Only runs that still exist get an index, so the numbers match
    what `omnicli resume <#>` (runs.find_run) understands even when --all shows missing ones."""
    out, i = [], 0
    for rec in all_runs:
        if rec.get("exists", True):
            i += 1
            out.append((i, rec))
        else:
            out.append((None, rec))
    return out


def handle_runs(args) -> int:
    as_json = getattr(args, "json", False)
    pruned = 0
    if getattr(args, "prune", False):
        for rec in runs.list_runs(include_missing=True):
            if not rec.get("exists") and runs.forget_run(rec["dir"]):
                pruned += 1
        msg = f"Forgot {pruned} run(s) whose project folder is gone."
        if as_json:
            print(msg, file=sys.stderr)
        else:
            print(f"{BADGE_INFO} {msg}")

    everything = runs.list_runs(include_missing=True)
    show_all = getattr(args, "all", False)
    pairs = [(i, r) for i, r in _indexed(everything) if show_all or r.get("exists")]
    hidden = len(everything) - len(pairs)

    if as_json:
        print(json.dumps([_json_record(i, r) for i, r in pairs], indent=2))
        return 0

    if not pairs:
        print(f"\n{BADGE_INFO} {'No runs to show' if hidden else 'No team runs yet'}. Start one with:")
        print('  omnicli team "build a todo web app with tests" -d ./todo-app\n')
        if hidden:
            print(c_dim(f"{hidden} run(s) whose folder is gone are hidden (--all to show, --prune to forget)."))
        return 0

    _table(_HEADERS, [_row(i, r) for i, r in pairs], title=f"Team runs on this machine ({len(pairs)})")
    if hidden:
        print(c_dim(f"{hidden} run(s) whose folder is gone are hidden (--all to show, --prune to forget)."))
    if any(_status(r) == "interrupted" for _, r in pairs):
        print(c_dim("'interrupted' = the process running it died; resume picks up where it stopped."))
    print(f"Resume one with: {c_bold('omnicli resume <#>')}   "
          f"{c_dim('(or the latest unfinished: omnicli continue)')}")
    return 0


# --------------------------------------------------------------------------- resume / continue

def _is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty())
    except Exception:
        return False


def _nothing_to_resume() -> int:
    print(f"\n{BADGE_INFO} Nothing to resume: no unfinished team runs on this machine.")
    print('Start one with:  omnicli team "<goal>" -d ./my-project')
    print(c_dim("See every run (including finished ones) with: omnicli runs"))
    return 1


def _unfinished() -> List[Dict]:
    return [r for r in runs.list_runs() if r.get("phase") in runs.UNFINISHED_PHASES]


def _pick(candidates: List[Dict]) -> Optional[Dict]:
    """Numbered picker. Enter = 1, q = cancel. Returns None when cancelled."""
    _table(_HEADERS, [_row(i, r) for i, r in enumerate(candidates, 1)], title="Unfinished team runs")
    n = len(candidates)
    question = f"Resume which run? [1-{n}, Enter = 1, q = cancel]: " if n > 1 else "Resume this run? [Enter = yes, q = cancel]: "
    for _ in range(3):
        try:
            answer = input(c_bold(c_yellow(question))).strip().lower().lstrip("#")
        except EOFError:
            return None
        if answer in ("q", "quit", "n", "no"):
            return None
        if answer in ("", "y", "yes"):
            return candidates[0]
        if answer.isdigit() and 1 <= int(answer) <= n:
            return candidates[int(answer) - 1]
        print(f"{BADGE_WARN} Type a number from 1 to {n}, or q to cancel.")
    return None


def _build_config(args, project_dir: Path):
    """Same mapping as `omnicli team` (shared in omnicli/teamopts.py): a flag wins, else the saved setting."""
    from omnicli.teamopts import build_team_config
    return build_team_config(args, goal="", project_dir=project_dir, resume=True)


def _resume(rec: Dict, args) -> int:
    d = Path(rec["dir"])
    state = runs.load_state(d)
    if state is None:
        print(f"{BADGE_FAILED} Can't read this run's saved state: {d / '.omnicli' / 'team' / 'state.json'}")
        print(c_dim("If the folder was moved or deleted, forget it with: omnicli runs --prune"))
        return 1

    phase = state.get("phase") or rec.get("phase") or "planned"
    name = state.get("project_name") or rec.get("project_name") or d.name
    # Refuse if either the state file or the registry says a live process owns this run:
    # two conductors on one project would fight over the same files and state.json.
    live_pid = 0
    if phase == "running" and runs.pid_alive(state.get("pid") or 0):
        live_pid = state.get("pid")
    elif rec.get("alive"):
        live_pid = rec.get("pid") or 0
    if live_pid:
        print(f"{BADGE_WARN} {c_bold(name)} is running right now in another process (pid {live_pid}).")
        print(f"Watch it with: {c_bold('omnicli status -d ' + shlex.quote(str(d)))}   "
              f"{c_dim('(stop it with Ctrl+C in its terminal first if you really want to resume it here)')}")
        return 1

    if phase == "complete" and not getattr(args, "yes", False):
        print(f"{BADGE_INFO} {c_bold(name)} is already complete.")
        print(c_dim("Resuming asks the lead to check it again and may plan (and run) follow-up tasks."))
        if not _is_tty():
            print(f"Add -y to resume it anyway: omnicli resume {shlex.quote(str(d))} -y")
            return 1
        try:
            answer = input(c_bold(c_yellow("Resume it anyway? [y/N]: "))).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print(f"{BADGE_INFO} Left as is.")
            return 0

    tasks = state.get("tasks") or []
    done = sum(1 for t in tasks if isinstance(t, dict) and t.get("status") == "done")
    was = "interrupted (its process died)" if phase == "running" else phase
    lines = [
        f"Project: {name}",
        f"Goal:    {(state.get('goal') or rec.get('goal') or '').strip()[:86]}",
        f"Was:     {was} · {done}/{len(tasks)} tasks done · round {state.get('round', 1)}",
        f"Team:    lead {state.get('lead') or '-'} · workers {', '.join(state.get('workers') or []) or '-'}",
        f"Dir:     {_short_dir(str(d), 84)}",
    ]
    print()
    print_panel("\n".join(lines), title="Resuming team run")
    try:
        cfg = _build_config(args, d)
    except ValueError as ex:
        print(f"{BADGE_FAILED} {ex}")
        return 1
    return int(_make_conductor(cfg).run() or 0)


def handle_resume(args) -> int:
    ref = (getattr(args, "ref", None) or "").strip()
    if ref:
        rec = runs.find_run(ref)
        if rec is None:
            p = Path(ref).expanduser()
            if p.is_dir():
                print(f"{BADGE_FAILED} No team run in {p.resolve()} (no .omnicli/team/state.json).")
                print(f'Start one there with:  omnicli team "<goal>" -d {shlex.quote(ref)}')
            elif ref.isdigit():
                print(f"{BADGE_FAILED} There is no run #{ref}. See the numbers with: omnicli runs")
            else:
                print(f"{BADGE_FAILED} No run matches '{ref}'. See your runs with: omnicli runs")
            return 1
        return _resume(rec, args)

    candidates = _unfinished()
    if not candidates:
        return _nothing_to_resume()
    if _is_tty():
        try:
            chosen = _pick(candidates)
        except KeyboardInterrupt:
            print(f"\n{BADGE_INFO} Cancelled.")
            return 130
        if chosen is None:
            print(f"{BADGE_INFO} Cancelled.")
            return 0
        return _resume(chosen, args)
    rec = runs.most_recent_run(unfinished_only=True)
    if rec is None:
        return _nothing_to_resume()
    print(f"{BADGE_INFO} Not a terminal: resuming the most recent unfinished run. "
          f"{c_dim('(pick another with: omnicli resume <#>)')}")
    return _resume(rec, args)


def handle_continue(args) -> int:
    rec = runs.most_recent_run(unfinished_only=True)
    if rec is None:
        return _nothing_to_resume()
    return _resume(rec, args)


def prepare_replay(ref: str, destination: str):
    """Copy a saved plan into a new, empty directory with all task results reset."""
    rec = runs.find_run(ref)
    if rec is None:
        raise ValueError("no run matches {!r}; see `omnicli runs`".format(ref))
    source = Path(rec["dir"]).expanduser().resolve()
    state = runs.load_state(source)
    if not isinstance(state, dict) or not isinstance(state.get("tasks"), list) or not state["tasks"]:
        raise ValueError("the source run has no readable saved plan")
    target = Path(destination).expanduser().resolve()
    if target == source:
        raise ValueError("replay destination must differ from the source run directory")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("replay destination must be empty: {}".format(target))
    state_dir = project_state_dir(target)
    state_path = state_dir / "state.json"
    if state_path.exists():
        raise ValueError("a team run already exists in {}".format(target))
    tasks = []
    for old in state["tasks"]:
        if not isinstance(old, dict):
            continue
        task = dict(old)
        task.update(status="pending", agent_used="", summary="", review_note="", attempts=[], commit="",
                    duration_sec=0.0, workspace="", outcome_recorded=False)
        tasks.append(task)
    if not tasks:
        raise ValueError("the source run has no valid tasks to replay")
    replay_state = dict(state)
    replay_state.update(dir=str(target), phase="planned", pid=0, round=1, final={}, started_at=time.time(),
                        updated_at=time.time(), tasks=tasks)
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(replay_state, indent=2), encoding="utf-8")
    tmp.replace(state_path)
    runs.register_run({"dir": str(target), "goal": replay_state.get("goal", ""),
                       "project_name": replay_state.get("project_name") or target.name,
                       "phase": "planned", "lead": replay_state.get("lead", ""),
                       "workers": replay_state.get("workers", []), "tasks_total": len(tasks),
                       "tasks_done": 0, "tasks_failed": 0, "round": 1, "started_at": replay_state["started_at"],
                       "pid": 0})
    return target, replay_state


def handle_replay(args) -> int:
    try:
        target, state = prepare_replay(args.ref, args.dir)
        cfg = _build_config(args, target)
    except (ValueError, OSError) as ex:
        print("Replay error: {}".format(ex))
        return 1
    cfg.goal = state.get("goal", "")
    cfg.project_name = state.get("project_name") or target.name
    cfg.resume = True
    cfg.confirm_saved_plan = True
    if not getattr(args, "lead", None) and not cfg.lead:
        cfg.lead = state.get("lead") or None
    if not getattr(args, "workers", None) and not cfg.workers:
        cfg.workers = state.get("workers") or None
    print_panel("Source: {}\nDestination: {}\nTasks: {}".format(args.ref, target, len(state["tasks"])),
                title="Replaying saved plan")
    return int(_make_conductor(cfg).run() or 0)


def prepare_fork(ref: str, task_id: str, agent: str, destination: Optional[str] = None):
    """Create a Git worktree and saved run state rewound to the chosen task."""
    import re

    rec = runs.find_run(ref)
    if rec is None:
        raise ValueError("no run matches {!r}; see `omnicli runs`".format(ref))
    source = Path(rec["dir"]).expanduser().resolve()
    state = runs.load_state(source)
    if not isinstance(state, dict) or not isinstance(state.get("tasks"), list):
        raise ValueError("the source run has no readable saved plan")
    tasks = [dict(task) for task in state["tasks"] if isinstance(task, dict)]
    index = next((i for i, task in enumerate(tasks) if task.get("id") == task_id), None)
    if index is None:
        raise ValueError("task {!r} is not in this run".format(task_id))
    from omnicli.gitutil import safe_rev
    if safe_rev(tasks[index].get("commit")):
        base_result = subprocess.run(["git", "rev-parse", "--verify", "{}^".format(safe_rev(tasks[index]["commit"]))],
                                     cwd=str(source), capture_output=True, text=True)
        base = base_result.stdout.strip() if base_result.returncode == 0 else ""
    else:
        base = ""
    if not base:
        previous = next((safe_rev(task.get("commit")) for task in reversed(tasks[:index])
                         if safe_rev(task.get("commit"))), "")
        if previous:
            base = previous
        else:
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(source), capture_output=True, text=True)
            if head.returncode:
                raise ValueError("fork requires a Git project with at least one commit")
            base = head.stdout.strip()
    target = Path(destination).expanduser().resolve() if destination else source.parent / (
        source.name + "-fork-{}-{}".format(re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id), int(time.time())))
    if target == source or target.exists():
        raise ValueError("fork destination must be a new directory: {}".format(target))
    branch = "omnicli/fork-{}-{}".format(re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id), int(time.time()))
    result = subprocess.run(["git", "worktree", "add", "-b", branch, str(target), base],
                            cwd=str(source), capture_output=True, text=True)
    if result.returncode:
        raise ValueError("could not create fork worktree: {}".format((result.stderr or result.stdout).strip()))
    for i, task in enumerate(tasks):
        if i < index:
            task["status"] = "done"
            continue
        task.update(status="pending", agent_used="", summary="", review_note="", attempts=[], commit="",
                    duration_sec=0.0, workspace="", outcome_recorded=False)
    tasks[index]["assignee"] = agent
    fork_state = dict(state)
    fork_state.update(dir=str(target), phase="planned", pid=0, final={}, started_at=time.time(),
                      updated_at=time.time(), tasks=tasks)
    state_path = project_state_dir(target) / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(fork_state, indent=2), encoding="utf-8")
    tmp.replace(state_path)
    workers = list(dict.fromkeys((state.get("workers") or []) + [agent]))
    runs.register_run({"dir": str(target), "goal": fork_state.get("goal", ""),
                       "project_name": (fork_state.get("project_name") or target.name) + " (fork)",
                       "phase": "planned", "lead": fork_state.get("lead", ""), "workers": workers,
                       "tasks_total": len(tasks), "tasks_done": index, "tasks_failed": 0,
                       "round": fork_state.get("round", 1), "started_at": fork_state["started_at"], "pid": 0})
    return target, fork_state, workers


def handle_fork(args) -> int:
    try:
        target, state, workers = prepare_fork(args.ref, args.task, args.agent, getattr(args, "dir", None))
        cfg = _build_config(args, target)
    except (ValueError, OSError) as ex:
        print("Fork error: {}".format(ex))
        return 1
    cfg.goal = state.get("goal", "")
    cfg.project_name = (state.get("project_name") or target.name) + " (fork)"
    cfg.resume = True
    cfg.confirm_saved_plan = True
    cfg.workers = workers
    cfg.lead = cfg.lead or state.get("lead") or None
    print_panel("Source: {}\nTask: {} -> {}\nBranch directory: {}".format(args.ref, args.task, args.agent, target),
                title="Forking team run")
    return int(_make_conductor(cfg).run() or 0)
