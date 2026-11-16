"""
`omnicli logs` - read the team's transcripts: what each AI CLI was asked and what it said.

    omnicli logs [TASK] [-d DIR | --run REF] [--agent NAME] [--lead [KIND]]
                 [--prompt] [--stderr] [--list] [--tail N] [--follow] [--json]

Every AI call of a team run is logged in <project>/.omnicli/team/logs/:

    <taskid>-<agent>-<n>.md        worker output for attempt n of a task   (t2-agy-1.md)
    lead-NNN-<kind>-<agent>.md     lead call number NNN (kind: plan|review|final|...)

each with a `.prompt.md` (the prompt) and a `.stderr.log` (stderr) sibling sharing the
stem. Task ids and user-defined agent names may contain dashes (`r2-t1`, `my-bot`), so
names are parsed against the known agent ids and the task ids in state.json.

Note: agents.run_agent writes the `.md` output only when the call ends, while
`.stderr.log` grows live; `--follow` therefore waits for the output file to appear.
"""

from __future__ import annotations

import codecs
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from omnicli import paths, runs
from omnicli.ui import BADGE_INFO, BADGE_WARN, c_bold, c_cyan, c_dim, c_magenta, print_table

NAMES = ["logs"]

STREAMS = ("output", "prompt", "stderr")
# Order matters: ".prompt.md" must be tried before the plain ".md".
_SUFFIXES = ((".prompt.md", "prompt"), (".stderr.log", "stderr"), (".md", "output"))
_SUFFIX_FOR = {stream: suffix for suffix, stream in _SUFFIXES}
STREAM_LABEL = {"output": "output", "prompt": "prompt", "stderr": "stderr"}

DEFAULT_AGENTS = ("claude", "codex", "agy", "muse")
KNOWN_LEAD_KINDS = ("plan", "review", "final")

_LEAD_RE = re.compile(r"^lead-(\d+)-(.+)$")
_ATTEMPT_RE = re.compile(r"^(.+)-(\d+)$")


# --------------------------------------------------------------------------- argparse

def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "logs",
        help="Read the team's transcripts: prompts, outputs and stderr of every AI call",
        description="Show the logs of an `omnicli team` run. With no TASK and no --lead, lists every log.",
    )
    p.add_argument("task", nargs="?", metavar="TASK", help="Show every attempt for this task id (e.g. t2)")
    p.add_argument("-d", "--dir", help="Project directory of the run (default: current dir, else the most recent run)")
    p.add_argument("--run", metavar="REF", help="Pick a run from `omnicli runs` (index, path or name)")
    p.add_argument("--agent", metavar="NAME", help="Only logs from this AI CLI (claude, codex, agy, ...)")
    p.add_argument("--lead", nargs="?", const="", default=None, metavar="KIND",
                   help="Show the lead's calls (optionally one kind: plan, review, final), newest last")
    p.add_argument("--prompt", action="store_true", help="Show the prompt that was sent instead of the output")
    p.add_argument("--stderr", action="store_true", help="Show the agent's stderr instead of the output")
    p.add_argument("--list", action="store_true", help="Table of log files (the default with no TASK / --lead)")
    p.add_argument("--tail", type=int, default=None, metavar="N", help="Only the last N lines of each file")
    p.add_argument("-f", "--follow", action="store_true",
                   help="Follow the newest log (of TASK, if given) as it grows; Ctrl-C to stop")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p.set_defaults(func=handler)


# --------------------------------------------------------------------------- parsing

def split_log_filename(name: str) -> Optional[Tuple[str, str]]:
    """'t2-agy-1.prompt.md' -> ('t2-agy-1', 'prompt'). None for files that are not logs."""
    for suffix, stream in _SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], stream
    return None


def _result(role: str, task: str = "", agent: str = "", attempt: Optional[int] = None,
            call: Optional[int] = None, lead_kind: str = "") -> Dict:
    return {"role": role, "task": task, "agent": agent, "attempt": attempt, "call": call, "lead_kind": lead_kind}


def _known_agent_suffix(rest: str, agents: Sequence[str]) -> Optional[str]:
    """Longest known agent `a` such that rest == '<something>-<a>'."""
    best = None
    for a in agents:
        if a and rest.endswith("-" + a) and len(rest) > len(a) + 1:
            if best is None or len(a) > len(best):
                best = a
    return best


def _split_lead_rest(rest: str, agents: Sequence[str]) -> Tuple[str, str]:
    """'review-claude' -> ('review', 'claude'), using known agents, then known kinds."""
    a = _known_agent_suffix(rest, agents)
    if a:
        return rest[: -(len(a) + 1)], a
    for k in KNOWN_LEAD_KINDS:
        if rest.startswith(k + "-") and len(rest) > len(k) + 1:
            return k, rest[len(k) + 1:]
    if "-" in rest:
        k, a = rest.split("-", 1)
        return k, a
    return rest, ""


def parse_log_stem(stem: str, known_agents: Iterable[str] = (), known_tasks: Iterable[str] = ()) -> Dict:
    """Classify a log stem (file name without .md / .prompt.md / .stderr.log).

    Returns {"role": "task"|"lead"|"other", "task", "agent", "attempt", "call", "lead_kind"}.
    Ambiguous dashes are resolved with the known agent ids and task ids:
      t2-agy-1            -> task t2,     agent agy,    attempt 1
      r2-t1-claude-3      -> task r2-t1,  agent claude, attempt 3
      fix-ui-my-bot-1     -> task fix-ui, agent my-bot (when my-bot or fix-ui is known)
      lead-004-review-codex -> lead call 4, kind review, agent codex
    """
    agents = [a for a in known_agents if a]
    tasks = set(t for t in known_tasks if t)

    # 1) a lead call whose agent we recognise (lead stems end with the agent, not a number)
    m = _LEAD_RE.match(stem)
    if m and _known_agent_suffix(m.group(2), agents):
        kind, agent = _split_lead_rest(m.group(2), agents)
        return _result("lead", agent=agent, call=int(m.group(1)), lead_kind=kind)

    # 2) a worker attempt <task>-<agent>-<n> where the agent or the task id is known
    m2 = _ATTEMPT_RE.match(stem)
    splits: List[Tuple[str, str]] = []
    if m2:
        prefix = m2.group(1)
        splits = [(prefix[:i], prefix[i + 1:]) for i, ch in enumerate(prefix)
                  if ch == "-" and 0 < i < len(prefix) - 1]

        def score(split: Tuple[str, str]) -> Tuple:
            task, agent = split
            a_known, t_known = agent in agents, task in tasks
            return (a_known and t_known, a_known, t_known, len(agent) if a_known else -len(agent))

        evidenced = [s for s in splits if s[1] in agents or s[0] in tasks]
        if evidenced:
            task, agent = max(evidenced, key=score)
            return _result("task", task=task, agent=agent, attempt=int(m2.group(2)))

    # 3) a lead call by unknown agent
    if m:
        kind, agent = _split_lead_rest(m.group(2), agents)
        return _result("lead", agent=agent, call=int(m.group(1)), lead_kind=kind)

    # 4) a worker attempt with no known parts: the agent is the last dash-segment
    if splits:
        task, agent = splits[-1]
        return _result("task", task=task, agent=agent, attempt=int(m2.group(2)))  # type: ignore[union-attr]

    return _result("other")


# --------------------------------------------------------------------------- collecting

def known_agent_names(state: Optional[Dict]) -> List[str]:
    """Agent ids from the adapter config plus every agent named in state.json."""
    names: List[str] = []
    try:
        from omnicli import agents as ag
        names.extend(ag.load_agent_specs().keys())
    except Exception:
        names.extend(DEFAULT_AGENTS)
    if state:
        names.append(str(state.get("lead") or ""))
        names.extend(str(w) for w in (state.get("workers") or []))
        for t in state.get("tasks") or []:
            names.append(str(t.get("assignee") or ""))
            names.append(str(t.get("agent_used") or ""))
            names.extend(str(a.get("agent") or "") for a in (t.get("attempts") or []) if isinstance(a, dict))
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def state_tasks(state: Optional[Dict]) -> List[Dict]:
    return [t for t in ((state or {}).get("tasks") or []) if isinstance(t, dict) and t.get("id")]


def logs_dir_for(project_dir) -> Path:
    return paths.project_state_dir(project_dir) / "logs"


def collect_entries(logs_dir: Path, state: Optional[Dict] = None,
                    known_agents: Optional[Sequence[str]] = None) -> List[Dict]:
    """One entry per log stem (output + prompt + stderr siblings), oldest first.

    Entry: {stem, kind ("task" | "lead:<kind>" | "other"), role, task, agent, attempt, call,
            lead_kind, output, prompt, stderr (absolute paths or None), size (output bytes),
            sizes {stream: bytes}, started, modified (epoch), result, duration_sec}
    """
    logs_dir = Path(logs_dir)
    if not logs_dir.is_dir():
        return []
    if known_agents is None:
        known_agents = known_agent_names(state)
    known_tasks = [str(t["id"]) for t in state_tasks(state)]

    groups: Dict[str, Dict[str, Path]] = {}
    try:
        children = list(logs_dir.iterdir())
    except OSError:
        return []
    for f in children:
        if not f.is_file():
            continue
        split = split_log_filename(f.name)
        if split:
            groups.setdefault(split[0], {})[split[1]] = f

    entries = []
    for stem, files in groups.items():
        info = parse_log_stem(stem, known_agents, known_tasks)
        stats = {}
        for stream, f in files.items():
            try:
                stats[stream] = f.stat()
            except OSError:
                pass
        mtimes = [s.st_mtime for s in stats.values()]
        started = stats["prompt"].st_mtime if "prompt" in stats else (min(mtimes) if mtimes else 0.0)
        if info["role"] == "lead":
            kind = "lead:" + (info["lead_kind"] or "?")
        else:
            kind = info["role"]
        e = {"stem": stem, "kind": kind}
        e.update(info)
        for stream in STREAMS:
            e[stream] = str(files[stream]) if stream in stats else None
        e["sizes"] = {s: (stats[s].st_size if s in stats else None) for s in STREAMS}
        e["size"] = e["sizes"]["output"] or 0
        e["started"] = started
        e["modified"] = max(mtimes) if mtimes else 0.0
        e["result"] = None
        e["duration_sec"] = None
        entries.append(e)

    records = match_attempts(entries, state)
    for e in entries:
        rec = records.get(e["stem"])
        if rec:
            e["result"] = rec.get("result")
            e["duration_sec"] = rec.get("duration_sec")
    entries.sort(key=lambda e: (e["started"], e["call"] or 0, e["task"], e["attempt"] or 0, e["stem"]))
    return entries


def match_attempts(entries: List[Dict], state: Optional[Dict]) -> Dict[str, Dict]:
    """Map task-log stems to their state.json attempt records.

    The conductor names attempt logs <task>-<agent>-<len(attempts)+1>, so attempt n is
    attempts[n-1] when the agent matches; otherwise fall back to the k-th attempt by that
    agent (e.g. state edited by hand)."""
    out: Dict[str, Dict] = {}
    by_task: Dict[str, List[Dict]] = {}
    for e in entries:
        if e["role"] == "task":
            by_task.setdefault(e["task"], []).append(e)
    tasks = {str(t["id"]): t for t in state_tasks(state)}
    for tid, group in by_task.items():
        attempts = [a for a in (tasks.get(tid, {}).get("attempts") or []) if isinstance(a, dict)]
        if not attempts:
            continue
        group.sort(key=lambda e: e["attempt"] or 0)
        used = set()
        pending = []
        for e in group:
            i = (e["attempt"] or 0) - 1
            if 0 <= i < len(attempts) and attempts[i].get("agent") == e["agent"]:
                out[e["stem"]] = attempts[i]
                used.add(i)
            else:
                pending.append(e)
        for e in pending:
            for i, a in enumerate(attempts):
                if i not in used and a.get("agent") == e["agent"]:
                    out[e["stem"]] = a
                    used.add(i)
                    break
    return out


def filter_entries(entries: List[Dict], task: Optional[str] = None, agent: Optional[str] = None,
                   lead: Optional[str] = None) -> List[Dict]:
    """task: only that task's attempts. lead: None = no lead filter, "" = every lead call,
    "plan" = only that kind. agent: case-insensitive exact match."""
    out = entries
    if task is not None:
        out = [e for e in out if e["role"] == "task" and e["task"] == task]
    if lead is not None:
        out = [e for e in out if e["role"] == "lead" and (not lead or e["lead_kind"].lower() == lead.lower())]
    if agent:
        out = [e for e in out if e["agent"].lower() == agent.lower()]
    return out


def newest_entry(entries: List[Dict]) -> Optional[Dict]:
    """The most recently *started* call (prompt files are written once, when a call starts)."""
    if not entries:
        return None
    return max(entries, key=lambda e: (e["started"], e["call"] or 0, e["attempt"] or 0, e["stem"]))


# --------------------------------------------------------------------------- formatting

def fmt_duration(sec) -> str:
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return ""
    if sec < 60:
        return f"{sec:.0f}s"
    return f"{sec / 60:.1f} min"


def fmt_size(n: Optional[int]) -> str:
    if n is None:
        return "-"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def fmt_ago(ts: float, now: Optional[float] = None) -> str:
    if not ts:
        return "-"
    d = max(0.0, (now if now is not None else time.time()) - ts)
    if d < 5:
        return "just now"
    if d < 60:
        return f"{int(d)}s ago"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 86400:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // 86400)}d ago"


def _short(text: str, limit: int = 100) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tail_text(text: str, n: Optional[int]) -> Tuple[str, int]:
    """Last n lines of text -> (text, number of hidden lines). n None = everything."""
    if n is None:
        return text, 0
    lines = text.splitlines()
    if len(lines) <= n:
        return text, 0
    kept = lines[len(lines) - n:] if n > 0 else []
    return "\n".join(kept) + ("\n" if kept and text.endswith("\n") else ""), len(lines) - n


def _read(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _in_progress(e: Dict, state: Optional[Dict], entries: List[Dict]) -> bool:
    for t in state_tasks(state):
        if t["id"] == e["task"]:
            if t.get("status") != "running":
                return False
            last = max((x["attempt"] or 0) for x in entries if x["role"] == "task" and x["task"] == e["task"])
            return (e["attempt"] or 0) == last
    return False


def entry_header(e: Dict, record: Optional[Dict] = None, in_progress: bool = False) -> str:
    """'t2 · agy · attempt 1 · result: revise · 1.9 min' / 'lead · review · claude · call 4 · 5m ago'."""
    if e["role"] == "task":
        parts = [e["task"], e["agent"] or "?", f"attempt {e['attempt']}"]
        if record:
            parts.append("result: " + _short(record.get("result") or "?", 80))
            dur = fmt_duration(record.get("duration_sec"))
            if dur:
                parts.append(dur)
        else:
            parts.append("result: in progress" if in_progress else "result: not recorded")
        return " · ".join(parts)
    if e["role"] == "lead":
        return " · ".join(["lead", e["lead_kind"] or "?", e["agent"] or "?", f"call {e['call']}",
                           fmt_ago(e["modified"])])
    return f"{e['stem']} · {fmt_ago(e['modified'])}"


def _detail_lines(record: Optional[Dict]) -> List[str]:
    if not record:
        return []
    bits = []
    if record.get("exit_code") is not None:
        bits.append(f"exit {record['exit_code']}")
    if record.get("verify_exit") is not None:
        bits.append(f"verify exit {record['verify_exit']}")
    if record.get("files_changed") is not None:
        n = record["files_changed"]
        bits.append(f"{n} file{'' if n == 1 else 's'} changed")
    if record.get("revision"):
        bits.append(f"revision {record['revision']}")
    if record.get("at"):
        bits.append(str(record["at"]))
    lines = [" · ".join(bits)] if bits else []
    if record.get("feedback"):
        lines.append("lead feedback: " + _short(record["feedback"], 200))
    return lines


def _missing_note(e: Dict, stream: str) -> str:
    if stream == "output" and e.get("prompt"):
        return "(no output yet - the call may still be running or was interrupted; try --stderr or --follow)"
    return f"({STREAM_LABEL[stream]} file missing)"


# --------------------------------------------------------------------------- run lookup

def resolve_run_dir(args) -> Tuple[Optional[Path], str]:
    """-d DIR, else --run REF, else cwd (if it holds a team run), else the most recent run."""
    d = getattr(args, "dir", None)
    if d:
        p = Path(d).expanduser().resolve()
        if not p.is_dir():
            return None, f"{d} is not a directory."
        return p, ""
    ref = getattr(args, "run", None)
    if ref:
        rec = runs.find_run(ref)
        if not rec:
            return None, f"No team run matches '{ref}'. See your runs with: omnicli runs"
        return Path(rec["dir"]), ""
    cwd = Path.cwd()
    if (paths.project_state_dir(cwd) / "state.json").exists():
        return cwd.resolve(), ""
    rec = runs.most_recent_run()
    if rec:
        return Path(rec["dir"]), ""
    return None, 'No team runs found yet. Start one with: omnicli team "<goal>" -d DIR'


def _resolve_task(ref: str, state: Optional[Dict], entries: List[Dict]) -> Tuple[Optional[str], List[str]]:
    ids: List[str] = []
    for t in state_tasks(state):
        if str(t["id"]) not in ids:
            ids.append(str(t["id"]))
    for e in entries:
        if e["role"] == "task" and e["task"] not in ids:
            ids.append(e["task"])
    if ref in ids:
        return ref, ids
    low = [i for i in ids if i.lower() == ref.lower()]
    return (low[0] if low else None), ids


# --------------------------------------------------------------------------- output

def _fail(msg: str, as_json: bool, code: int = 1) -> int:
    if as_json:
        print(json.dumps({"error": msg}))
    else:
        print(f"{BADGE_WARN} {msg}")
    return code


def _public(e: Dict) -> Dict:
    return {k: e[k] for k in ("kind", "task", "agent", "attempt", "call", "lead_kind", "stem", "size",
                              "modified", "started", "result", "duration_sec", "output", "prompt",
                              "stderr", "sizes")}


def print_list(entries: List[Dict], title: str) -> None:
    now = time.time()
    rows = []
    for e in entries:
        if e["role"] == "task":
            num = str(e["attempt"])
        elif e["role"] == "lead":
            num = f"call {e['call']}"
        else:
            num = ""
        rows.append([
            e["kind"],
            e["task"] or ("" if e["role"] != "other" else e["stem"]),
            e["agent"],
            num,
            fmt_size(e["sizes"]["output"]),
            fmt_ago(e["modified"], now),
            "✓" if e["output"] else "·",
            "✓" if e["prompt"] else "·",
            "✓" if e["stderr"] else "·",
        ])
    print_table(["Kind", "Task", "Agent", "#", "Size", "Modified", "Out", "Prompt", "Err"], rows, title=title)


def show_entries(entries: List[Dict], streams: Sequence[str], tail: Optional[int], state: Optional[Dict],
                 all_entries: List[Dict], records: Dict[str, Dict], as_json: bool) -> None:
    if as_json:
        out = []
        for e in entries:
            rec = records.get(e["stem"])
            item = _public(e)
            item["header"] = entry_header(e, rec, _in_progress(e, state, all_entries))
            item["record"] = rec
            content = {}
            for s in streams:
                text = _read(e.get(s))
                content[s] = tail_text(text, tail)[0] if text is not None else None
            item["content"] = content
            out.append(item)
        print(json.dumps(out, indent=2))
        return
    for e in entries:
        rec = records.get(e["stem"])
        header = entry_header(e, rec, _in_progress(e, state, all_entries))
        print(c_bold(c_cyan("━━ " + header)))
        for line in _detail_lines(rec):
            print(c_dim("   " + line))
        for s in streams:
            if len(streams) > 1:
                print(c_dim(f"── {STREAM_LABEL[s]} ──"))
            text = _read(e.get(s))
            if text is None:
                print(c_dim(_missing_note(e, s)))
                continue
            text, hidden = tail_text(text, tail)
            if hidden:
                print(c_dim(f"… {hidden} earlier line{'s' if hidden != 1 else ''} hidden (--tail {tail})"))
            if text.strip():
                print(text.rstrip("\n"))
            elif not hidden:
                print(c_dim(f"(empty {STREAM_LABEL[s]})"))
        print()


# --------------------------------------------------------------------------- follow

def follow_logs(pick: Callable[[], Optional[Dict]], logs_dir: Path, stream: str = "output",
                tail: Optional[int] = None, max_seconds: Optional[float] = None, poll: float = 0.5,
                out=None, header: Callable[[Dict], str] = entry_header) -> int:
    """Print the newest log's `stream` and keep printing what is appended to it.

    `pick()` returns the newest matching entry (re-evaluated every poll, so when a newer
    call starts - a revision, the next lead review - following switches to it).
    Stops on Ctrl-C (exit 0) or after max_seconds (used by tests)."""
    out = out if out is not None else sys.stdout
    logs_dir = Path(logs_dir)
    deadline = time.time() + max_seconds if max_seconds is not None else None

    def emit(text: str) -> None:
        out.write(text)
        out.flush()

    current: Optional[Dict] = None
    pos = 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    first_entry = True
    initial = False
    waiting_note = False
    ends_with_newline = True
    emit(c_dim(f"Following {STREAM_LABEL[stream]} in {logs_dir} (Ctrl-C to stop)") + "\n")
    try:
        while True:
            newest = pick()
            if newest is not None and (current is None or newest["stem"] != current["stem"]):
                if current is not None and not ends_with_newline:
                    emit("\n")
                current = newest
                pos = 0
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                initial = first_entry  # --tail trims only what existed before we started
                first_entry = False
                waiting_note = False
                emit(c_bold(c_cyan("━━ " + header(current))) + "\n")
            elif current is None and not waiting_note:
                emit(c_dim("(no logs yet - waiting for the first AI call)") + "\n")
                waiting_note = True

            if current is not None:
                path = logs_dir / (current["stem"] + _SUFFIX_FOR[stream])
                try:
                    size = path.stat().st_size
                except OSError:
                    size = None
                if size is None:
                    if not waiting_note:
                        emit(c_dim(f"(waiting for {path.name} to appear)") + "\n")
                        waiting_note = True
                else:
                    if size < pos:
                        emit(("" if ends_with_newline else "\n") + c_dim("(file was rewritten)") + "\n")
                        pos = 0
                        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    if size > pos:
                        with open(path, "rb") as fh:
                            fh.seek(pos)
                            data = fh.read()
                        pos += len(data)
                        text = decoder.decode(data)
                        if initial and tail is not None:
                            text, hidden = tail_text(text, tail)
                            if hidden:
                                emit(c_dim(f"… {hidden} earlier lines hidden") + "\n")
                        if text:
                            emit(text)
                            ends_with_newline = text.endswith("\n")
                initial = False

            if deadline is not None and time.time() >= deadline:
                break
            time.sleep(poll if deadline is None else max(0.0, min(poll, deadline - time.time())))
    except KeyboardInterrupt:
        pass
    if not ends_with_newline:
        emit("\n")
    return 0


# --------------------------------------------------------------------------- handler

def handler(args) -> int:
    as_json = bool(getattr(args, "json", False))
    tail = getattr(args, "tail", None)
    task_ref = getattr(args, "task", None)
    lead = getattr(args, "lead", None)
    agent = getattr(args, "agent", None)
    follow = bool(getattr(args, "follow", False))
    want_list = bool(getattr(args, "list", False))

    if tail is not None and tail < 0:
        return _fail("--tail needs a number of lines (0 or more).", as_json, 2)
    if task_ref and lead is not None:
        return _fail("Use either TASK or --lead, not both (lead calls are not tied to one task).", as_json, 2)
    if follow and (as_json or want_list):
        return _fail("--follow streams plain text; drop --json / --list.", as_json, 2)

    streams = [s for s, on in (("prompt", getattr(args, "prompt", False)),
                               ("stderr", getattr(args, "stderr", False))) if on] or ["output"]

    project, err = resolve_run_dir(args)
    if project is None:
        return _fail(err, as_json)
    state = runs.load_state(project)
    logs_dir = logs_dir_for(project)
    if state is None and not logs_dir.is_dir():
        return _fail(f'No team run in {project}. Start one with: omnicli team "<goal>" -d {project}', as_json)

    known_agents = known_agent_names(state)
    entries = collect_entries(logs_dir, state, known_agents)
    name = (state or {}).get("project_name") or project.name

    task_id = None
    if task_ref:
        task_id, valid = _resolve_task(task_ref, state, entries)
        if task_id is None:
            ids = ", ".join(valid) if valid else "(none yet)"
            return _fail(f"No task '{task_ref}' in {name}. Valid task ids: {ids}", as_json)

    if follow:
        def pick() -> Optional[Dict]:
            found = collect_entries(logs_dir, runs.load_state(project), known_agents)
            return newest_entry(filter_entries(found, task=task_id, agent=agent, lead=lead))

        recs: Dict[str, Dict] = {}

        def header(e: Dict) -> str:
            st = runs.load_state(project)
            all_e = collect_entries(logs_dir, st, known_agents)
            recs.update(match_attempts(all_e, st))
            return entry_header(e, recs.get(e["stem"]), _in_progress(e, st, all_e))

        return follow_logs(pick, logs_dir, stream=streams[0], tail=tail,
                           max_seconds=getattr(args, "follow_max_seconds", None),
                           poll=float(getattr(args, "follow_poll", 0.5) or 0.5), header=header)

    selected = filter_entries(entries, task=task_id, agent=agent, lead=lead)
    agents_seen = sorted({e["agent"] for e in entries if e["agent"]})
    records = match_attempts(entries, state)

    # ---- list mode (default with no TASK and no --lead)
    if want_list or (task_id is None and lead is None):
        if as_json:
            print(json.dumps([_public(e) for e in selected], indent=2))
            return 0
        if not entries:
            print(f"{BADGE_INFO} No logs yet for {name} ({logs_dir}).")
            print(c_dim(f"They appear as soon as the team starts: omnicli resume {project}"))
            return 0
        if not selected:
            print(f"{BADGE_WARN} No logs match those filters. Agents in this run: {', '.join(agents_seen) or '-'}")
            return 1
        print_list(selected, f"Logs · {name} ({len(selected)} of {len(entries)} calls)")
        first_task = next((e["task"] for e in entries if e["role"] == "task"), "t1")
        print(c_dim(f"Read one: omnicli logs {first_task}   lead calls: omnicli logs --lead   "
                    f"live: omnicli logs --follow"))
        print(c_dim(f"Files: {logs_dir}"))
        return 0

    # ---- task view
    if task_id is not None:
        if not selected:
            if as_json:
                print("[]")
                return 0
            mine = [e for e in entries if e["role"] == "task" and e["task"] == task_id]
            if mine and agent:
                ran = ", ".join(sorted({e["agent"] for e in mine}))
                print(f"{BADGE_WARN} {task_id} has no logs from '{agent}' (it ran on: {ran}).")
                return 1
            status = next((t.get("status") for t in state_tasks(state) if t["id"] == task_id), None)
            print(f"{BADGE_INFO} {task_id} has no logs yet" + (f" (status: {status})." if status else "."))
            return 0
        selected = sorted(selected, key=lambda e: (e["attempt"] or 0, e["started"]))
        if not as_json:
            title = next((t.get("title") for t in state_tasks(state) if t["id"] == task_id), "")
            n = len(selected)
            print()
            print(c_bold(task_id + (f" - {title}" if title else "")) +
                  c_dim(f"  ({n} attempt{'s' if n != 1 else ''}, {name})"))
            print()
        show_entries(selected, streams, tail, state, entries, records, as_json)
        return 0

    # ---- lead view
    if not selected:
        if as_json:
            print("[]")
            return 0
        leads = [e for e in entries if e["role"] == "lead"]
        if not leads:
            print(f"{BADGE_INFO} No lead calls logged yet for {name}.")
            return 0
        kinds = ", ".join(sorted({e["lead_kind"] for e in leads}))
        who = ", ".join(sorted({e["agent"] for e in leads}))
        print(f"{BADGE_WARN} No lead calls match. Kinds in this run: {kinds}; lead agents: {who}.")
        return 1
    selected = sorted(selected, key=lambda e: (e["call"] or 0, e["started"]))
    if not as_json:
        what = f"lead {lead} calls" if lead else "lead calls"
        print()
        print(c_bold(c_magenta(what)) + c_dim(f"  ({len(selected)}, {name}, newest last)"))
        print()
    show_entries(selected, streams, tail, state, entries, records, as_json)
    return 0
