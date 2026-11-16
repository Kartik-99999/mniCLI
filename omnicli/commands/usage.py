"""
`omnicli usage` - how much AI work OmniCLI has done (like Claude Code's /usage and /cost).

Reads the usage ledger (~/.omnicli/ledger.jsonl, see omnicli/ledger.py): one line per
AI CLI call made by `omnicli team` (and by health pings). Shows calls, failures, agent
time and *estimated* tokens, grouped by agent, call kind, run or day.

    omnicli usage                         everything recorded, by agent
    omnicli usage --days 7 --by day       last week, one row per day
    omnicli usage --run 1 --by kind       the most recent run, plan/review/task split
    omnicli usage --native                also show each AI CLI's own quota report

Token numbers are estimates (characters / 4): the AI CLIs don't report real token
counts in a common format, so they are always printed with a "~" prefix.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from omnicli import ledger, runs
from omnicli.ui import BADGE_INFO, BADGE_WARN, c_bold, c_cyan, c_dim, c_yellow, print_panel, print_table

NAMES = ["usage"]

GROUP_KEYS = ("agent", "kind", "run", "day")

# Each AI CLI's OWN usage/quota command, run by `omnicli usage --native`.
# Only commands verified to exist AND not to start a model turn are listed here.
# Verified 2026-11-15 on this machine by reading each CLI's --help output
# (never by guessing, never by sending a prompt):
#   muse 1.4.20  `muse --help` lists "usage  Show your subscription allowance";
#                `muse usage --help` says "Show your subscription allowance (no model
#                turn)" and documents `--json` ("Print one JSON object").
#   claude 2.1.325, codex-cli 0.163.0, agy 1.5.9: `--help` lists no usage/quota/cost
#                subcommand -> deliberately absent ("no usage command").
#   gemini: not installed here, so nothing could be verified -> absent.
# "bin" must match the resolved executable's basename, so a user adapter that reuses
# one of these ids for a different program is never sent these arguments.
NATIVE_USAGE_COMMANDS: Dict[str, Dict] = {
    "muse": {"bin": "muse", "args": ["usage"], "json_args": ["usage", "--json"]},
}

# Pointers for CLIs whose usage report exists only inside an interactive session.
NATIVE_HINTS: Dict[str, str] = {
    "claude": "open `claude` and type /usage (or /cost) inside the session",
}

NATIVE_TIMEOUT_SEC = 20
_STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
_MAX_NATIVE_LINES = 80
_NO_RUN_LABEL = "(no run: health pings etc.)"


# --------------------------------------------------------------------------- parser

def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "usage",
        help="Show AI calls, agent time and estimated tokens used by OmniCLI (like /usage, /cost)",
        description="Show how much AI work OmniCLI has done, from its usage ledger. "
                    "Token numbers are estimates (characters / 4).",
    )
    win = p.add_mutually_exclusive_group()
    win.add_argument("--days", type=int, metavar="N", help="Only the last N days")
    win.add_argument("--since", metavar="YYYY-MM-DD", help="Only calls on or after this local date")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--run", metavar="REF",
                       help="Only one run: index from `omnicli runs`, a project path, or part of its name")
    scope.add_argument("-d", "--dir", metavar="DIR", help="Only the team run in this project directory")
    p.add_argument("--agent", metavar="NAME", help="Only calls to this AI CLI (claude, codex, agy, muse, ...)")
    p.add_argument("--by", choices=GROUP_KEYS, default="agent", help="Group rows by agent (default), kind, run or day")
    p.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    p.add_argument("--native", action="store_true",
                   help="Also show each installed AI CLI's own usage/quota report (only verified commands)")
    p.set_defaults(func=handler)


# --------------------------------------------------------------------------- helpers

def human_duration(seconds: float) -> str:
    """42s, 3m12s, 1h04m."""
    s = int(round(float(seconds or 0)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _tok(n: int) -> str:
    return f"~{int(n or 0):,}"


def _pct(failed: int, calls: int) -> float:
    return round(failed / calls, 4) if calls else 0.0


def _local_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(float(ts or 0)))


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _short_path(p: str) -> str:
    home = str(Path.home())
    return "~" + p[len(home):] if p == home or p.startswith(home + os.sep) else p


def _err(msg: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": msg}))
    else:
        print(f"{BADGE_WARN} {msg}")
    return 2


def _parse_window(args) -> Tuple[float, Dict]:
    """-> (since_epoch, window dict). Raises ValueError with a friendly message."""
    now = time.time()
    days = getattr(args, "days", None)
    since_s = getattr(args, "since", None)
    window = {"label": "all time", "since": 0.0, "since_date": None, "until": now, "days": None}
    if days is not None:
        if days < 1:
            raise ValueError("--days must be 1 or more (e.g. `omnicli usage --days 7`).")
        since = now - days * 86400
        window.update(label=f"last {days} day{'s' if days != 1 else ''} (since {_fmt_ts(since)})",
                      since=since, since_date=_local_day(since), days=days)
        return since, window
    if since_s:
        try:
            dt = datetime.strptime(since_s.strip(), "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"--since expects a date like 2026-11-01, got '{since_s}'.")
        since = time.mktime(dt.timetuple())  # local midnight
        window.update(label=f"since {dt.strftime('%Y-%m-%d')}", since=since, since_date=dt.strftime("%Y-%m-%d"))
        return since, window
    return 0.0, window


def _resolve_scope(args) -> Tuple[Optional[str], Optional[str]]:
    """-> (run_dir, run_name) for --run / -d, or (None, None). Raises LookupError if --run matches nothing."""
    ref = getattr(args, "run", None)
    d = getattr(args, "dir", None)
    if ref:
        rec = runs.find_run(ref)
        if rec:
            return rec["dir"], _project_name(rec["dir"], rec)
        p = Path(ref).expanduser()
        if p.exists():  # a project dir that is no longer registered can still have ledger entries
            rd = str(p.resolve())
            return rd, _project_name(rd)
        raise LookupError(f"No run matches '{ref}'. Run `omnicli runs` to see them, then "
                          f"`omnicli usage --run <number>` (or `-d <project dir>`).")
    if d:
        rd = str(Path(d).expanduser().resolve())
        return rd, _project_name(rd)
    return None, None


_name_cache: Dict[str, str] = {}


def _project_name(run_dir: str, rec: Optional[Dict] = None) -> str:
    if not run_dir:
        return _NO_RUN_LABEL
    if run_dir in _name_cache:
        return _name_cache[run_dir]
    name = (rec or {}).get("project_name") or ""
    if not name:
        try:
            for r in runs.list_runs(include_missing=True):
                if r.get("dir") == run_dir and r.get("project_name"):
                    name = r["project_name"]
                    break
        except Exception:
            pass
    if not name:
        st = runs.load_state(run_dir) or {}
        name = st.get("project_name") or ""
    name = str(name or Path(run_dir).name or run_dir)
    _name_cache[run_dir] = name
    return name


def _group_key(e: Dict, by: str) -> str:
    if by == "day":
        return _local_day(e.get("ts", 0))
    if by == "run":
        return str(e.get("run_dir") or "")
    if by == "kind":
        return str(e.get("kind") or "other")
    return str(e.get("agent") or "?")


def build_report(entries: List[Dict], by: str) -> Tuple[Dict[str, Dict], Dict]:
    """-> (groups, totals). Pure: no I/O except project-name lookups for by=run / top_run."""
    keyed = [dict(e, _g=_group_key(e, by)) for e in entries]
    groups = ledger.summarize(keyed, key="_g")
    timed = {}
    for e in keyed:
        if e.get("timed_out"):
            timed[e["_g"]] = timed.get(e["_g"], 0) + 1
    for k, g in groups.items():
        g["seconds"] = round(g["seconds"], 2)
        g["timed_out"] = timed.get(k, 0)
        g["failure_rate"] = _pct(g["failed"], g["calls"])
        if by == "run":
            g["dir"] = k
            g["name"] = _project_name(k)
    if by == "day":
        order = sorted(groups)
    else:
        order = sorted(groups, key=lambda k: (-groups[k]["seconds"], -groups[k]["calls"], k))
    groups = {k: groups[k] for k in order}

    totals: Dict = {f: sum(g[f] for g in groups.values())
                    for f in ("calls", "ok", "failed", "est_tokens_in", "est_tokens_out")}
    totals["seconds"] = round(sum((float(e.get("duration_sec", 0) or 0) for e in entries), 0.0), 2)
    totals["timed_out"] = sum(1 for e in entries if e.get("timed_out"))
    totals["failure_rate"] = _pct(totals["failed"], totals["calls"])

    per_agent = ledger.summarize(entries, key="agent")
    agent_timeouts: Dict[str, int] = {}
    for e in entries:
        if e.get("timed_out"):
            a = str(e.get("agent", ""))
            agent_timeouts[a] = agent_timeouts.get(a, 0) + 1
    totals["failures_by_agent"] = {
        a: {"failed": g["failed"], "calls": g["calls"], "rate": _pct(g["failed"], g["calls"]),
            "timed_out": agent_timeouts.get(a, 0)}
        for a, g in sorted(per_agent.items(), key=lambda kv: -_pct(kv[1]["failed"], kv[1]["calls"]))
        if g["failed"]
    }

    run_groups = ledger.summarize([e for e in entries if e.get("run_dir")], key="run_dir")
    top = None
    if run_groups:
        d, g = max(run_groups.items(), key=lambda kv: (kv[1]["seconds"], kv[1]["calls"]))
        top = {"dir": d, "name": _project_name(d), "calls": g["calls"], "seconds": round(g["seconds"], 2),
               "est_tokens_in": g["est_tokens_in"], "est_tokens_out": g["est_tokens_out"]}
    totals["top_run"] = top
    return groups, totals


# --------------------------------------------------------------------------- native reports

def _native_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.setdefault("NO_COLOR", "1")
    return env


def collect_native(as_json: bool = False) -> Dict[str, Dict]:
    """Run each installed AI CLI's verified usage command (NATIVE_USAGE_COMMANDS only)."""
    try:
        from omnicli import agents as ag  # lazy: plain `omnicli usage` must work even if agents.py can't load
        specs = ag.load_agent_specs()
        resolve = ag.resolve_bin
    except Exception as ex:
        return {"_error": {"installed": False, "command": None, "error": f"could not load AI CLI adapters: {ex}"}}

    out: Dict[str, Dict] = {}
    for name, spec in specs.items():
        try:
            path = resolve(spec)
        except Exception:
            path = None
        res: Dict = {"name": spec.get("name", name), "installed": bool(path), "command": None}
        entry = NATIVE_USAGE_COMMANDS.get(name)
        if not path:
            res["note"] = "not installed"
        elif not entry or os.path.basename(path) != entry["bin"]:
            res["note"] = "no usage command"
            if name in NATIVE_HINTS:
                res["hint"] = NATIVE_HINTS[name]
        else:
            args = list(entry.get("json_args") if as_json and entry.get("json_args") else entry["args"])
            cmd = [path] + args
            res["command"] = [entry["bin"]] + args
            try:
                cp = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=NATIVE_TIMEOUT_SEC,
                                    env=_native_env())
                res["exit_code"] = cp.returncode
                res["output"] = (cp.stdout or "").strip()
                if cp.returncode != 0 and (cp.stderr or "").strip():
                    res["error"] = cp.stderr.strip()[-2000:]
                if as_json and res["output"]:
                    try:
                        res["data"] = json.loads(res["output"])
                    except ValueError:
                        pass
            except subprocess.TimeoutExpired:
                res["exit_code"] = None
                res["error"] = f"timed out after {NATIVE_TIMEOUT_SEC}s"
            except Exception as ex:
                res["exit_code"] = None
                res["error"] = f"could not run: {ex}"
        out[name] = res
    return out


def _print_native(native: Dict[str, Dict]) -> None:
    note = c_dim("(each AI CLI's own numbers, not estimates)")
    print(f"\n{c_bold(c_cyan('Native usage reports'))} {note}")
    for name, r in native.items():
        label = c_bold(name) + (c_dim(f" ({r['name']})") if r.get("name") and r.get("name") != name else "")
        if r.get("command"):
            print(f"  ── {label} {c_dim('$ ' + ' '.join(r['command']))}")
            text = r.get("output") or ""
            lines = text.splitlines()
            for line in lines[:_MAX_NATIVE_LINES]:
                print(f"     {line}")
            if len(lines) > _MAX_NATIVE_LINES:
                print(c_dim(f"     ... ({len(lines) - _MAX_NATIVE_LINES} more lines)"))
            if r.get("error"):
                print(f"     {c_yellow(r['error'].splitlines()[-1][:200])}")
            elif not lines:
                print(c_dim(f"     (no output, exit code {r.get('exit_code')})"))
        else:
            note = r.get("note") or r.get("error") or "no usage command"
            hint = f" - {r['hint']}" if r.get("hint") else ""
            print(f"  ── {label}: {c_dim(note + hint)}")
    print()


# --------------------------------------------------------------------------- handler

def _row(label: str, g: Dict) -> List[str]:
    return [label, str(g["calls"]), str(g["ok"]), str(g["failed"]), human_duration(g["seconds"]),
            _tok(g["est_tokens_in"]), _tok(g["est_tokens_out"])]


def _labels(groups: Dict[str, Dict], by: str) -> Dict[str, str]:
    if by != "run":
        return {k: (k or "?") for k in groups}
    names = {k: g.get("name") or _project_name(k) for k, g in groups.items()}
    seen: Dict[str, int] = {}
    for n in names.values():
        seen[n] = seen.get(n, 0) + 1
    out = {}
    for k, n in names.items():
        out[k] = f"{n} [{_short_path(k)}]" if k and seen[n] > 1 else n
        if len(out[k]) > 48:
            out[k] = out[k][:45] + "..."
    return out


def handler(args) -> int:
    as_json = bool(getattr(args, "json", False))
    _name_cache.clear()
    by = getattr(args, "by", None) or "agent"
    agent = (getattr(args, "agent", None) or "").strip() or None
    try:
        since, window = _parse_window(args)
        run_dir, run_name = _resolve_scope(args)
    except (ValueError, LookupError) as ex:
        return _err(str(ex), as_json)

    entries = ledger.read(since=since, agent=agent, run_dir=run_dir)
    window.update(agent=agent, run_dir=run_dir, run_name=run_name)
    if window["since"] == 0.0 and entries:
        window["first_call"] = min(float(e.get("ts", 0) or 0) for e in entries)
    groups, totals = build_report(entries, by)
    native = collect_native(as_json=as_json) if getattr(args, "native", False) else None

    if as_json:
        doc = {"window": window, "group_by": by, "groups": groups, "totals": totals}
        doc["quota_resets"] = ledger.active_quota_resets()
        if native is not None:
            doc["native"] = native
        print(json.dumps(doc, indent=2, ensure_ascii=False))
        return 0

    filters = []
    if agent:
        filters.append(f"agent={agent}")
    if run_dir:
        filters.append(f"run={run_name}")
    label = window["label"]
    if window.get("first_call"):
        label += f" (first call {_fmt_ts(window['first_call'])})"

    if not entries:
        recorded = len(ledger.read())
        print()
        if recorded and (filters or since):
            print(f"{BADGE_INFO} No AI calls match {label}" + (f" · {', '.join(filters)}" if filters else "")
                  + f" ({recorded} call{'s' if recorded != 1 else ''} recorded overall).")
            print(c_dim("  Try `omnicli usage` with no filters, or `omnicli runs` to pick a run."))
        else:
            print(f"{BADGE_INFO} No AI usage recorded yet.")
            print("  OmniCLI records every AI call from now on - each lead plan/review and every worker task of")
            print("  every `omnicli team` run (plus `omnicli agents --check` pings). Start one, then check back:")
            print(c_dim('    omnicli team "<what to build>" -d <dir>'))
        print()
        if native is not None:
            _print_native(native)
        return 0

    header = [f"Window : {label}"]
    if filters:
        header.append(f"Filter : {', '.join(filters)}")
    header.append(f"Calls  : {totals['calls']} ({totals['ok']} ok, {totals['failed']} failed"
                  + (f", {totals['timed_out']} timed out" if totals["timed_out"] else "") + ")")
    header.append(f"Time   : {human_duration(totals['seconds'])} of agent time")
    header.append(f"Tokens : {_tok(totals['est_tokens_in'])} in / {_tok(totals['est_tokens_out'])} out (estimated)")
    print()
    print_panel("\n".join(header), title="OmniCLI usage")

    labels = _labels(groups, by)
    rows = [_row(labels[k], g) for k, g in groups.items()]
    rows.append(_row("Total", totals))
    heads = [{"agent": "Agent", "kind": "Kind", "run": "Run", "day": "Day"}[by],
             "Calls", "OK", "Failed", "Time", "~Tokens in", "~Tokens out"]
    widths = [max(len(heads[i]), *(len(r[i]) for r in rows)) for i in range(len(heads))]
    for r in rows:  # right-align the numbers
        for i in range(1, len(heads)):
            r[i] = r[i].rjust(widths[i])
    rows.insert(len(rows) - 1, ["─" * w for w in widths])
    print_table(heads, rows, title=f"By {by}")

    top = totals.get("top_run")
    if top and not run_dir:
        print(f"{c_bold('Most expensive run:')} {top['name']} - {human_duration(top['seconds'])} across "
              f"{top['calls']} call{'s' if top['calls'] != 1 else ''} "
              f"({_tok(top['est_tokens_in'] + top['est_tokens_out'])} tokens) {c_dim(_short_path(top['dir']))}")
    fails = totals.get("failures_by_agent") or {}
    if fails:
        parts = [f"{a} {f['failed']}/{f['calls']} ({f['rate'] * 100:.0f}%"
                 + (f", {f['timed_out']} timed out" if f["timed_out"] else "") + ")"
                 for a, f in fails.items()]
        print(f"{c_bold('Failure rate:')} {c_yellow(', '.join(parts))}  "
              f"{c_dim('-> `omnicli logs` shows what went wrong')}")
    resets = ledger.active_quota_resets()
    if resets:
        now = time.time()
        parts = []
        for name, info in sorted(resets.items()):
            reset_at = float(info.get("reset_at", 0) or 0)
            parts.append("{} at {} (in {})".format(name, _fmt_ts(reset_at), human_duration(reset_at - now)))
        print(f"{c_bold('Usage caps:')} {c_yellow('; '.join(parts))}")
    print(c_dim("~ Token counts are estimates (characters / 4); the AI CLIs don't report real tokens in a common way."))
    print(c_dim("  Time is summed agent time: parallel calls overlap, so it can exceed wall-clock time."))
    if native is None:
        print(c_dim("  For each CLI's own quota/allowance report, run `omnicli usage --native`."))
    print()
    if native is not None:
        _print_native(native)
    return 0
