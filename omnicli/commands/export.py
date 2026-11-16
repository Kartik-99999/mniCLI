"""
`omnicli export` - save a team run as a shareable document (like Claude Code's /export).

    omnicli export                         # this dir's run (else the most recent) as Markdown on stdout
    omnicli export 2 --format html         # run #2 from `omnicli runs` -> <dir>/.omnicli/team/report.html
    omnicli export -d ./app --format json -o app-run.json
    omnicli export --include-logs -o run.md

Everything comes from files OmniCLI already keeps: the run's state.json, the usage
ledger (token numbers are ESTIMATES: characters / 4), the project's git checkpoints and,
with --include-logs, each task attempt's output log. No AI CLI is called.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from omnicli import __version__, ledger, paths, runs
from omnicli.ui import BADGE_SUCCESS, BADGE_WARN, c_bold, c_dim

NAMES = ["export"]

FORMATS = ("md", "json", "html")
EXTENSIONS = {"md": "md", "json": "json", "html": "html"}
LOG_LIMIT = 20000          # chars kept per attempt log (head + tail)
GIT_LIMIT = 200            # most git checkpoints listed
TOKEN_NOTE = ("Token counts are estimates (characters / 4) from the OmniCLI usage ledger; "
              "AI CLIs don't report exact usage.")


class ExportError(Exception):
    """A friendly, user-facing reason the export could not happen."""


# --------------------------------------------------------------------------- argparse

def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "export",
        help="Export a team run as Markdown, JSON or a self-contained HTML report",
        description="Export an `omnicli team` run (plan, tasks, attempts, reviews, usage, git checkpoints) "
                    "as Markdown, JSON or one self-contained HTML file.",
        epilog="examples:\n"
               "  omnicli export                       Markdown of this dir's run (else the most recent) to stdout\n"
               "  omnicli export 2 --format html       run #2 from `omnicli runs` -> .omnicli/team/report.html\n"
               "  omnicli export -d ./app --format json -o app.json\n"
               "  omnicli export --include-logs -o run.md\n\n"
               "Token numbers are estimates (characters / 4).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("ref", nargs="?", metavar="REF",
                   help="run to export: a number from `omnicli runs`, a directory, or part of its name/goal "
                        "(default: the run in the current dir, else the most recent)")
    p.add_argument("-d", "--dir", metavar="DIR", help="project directory of the run (takes precedence over REF)")
    p.add_argument("--format", choices=FORMATS, default="md", help="output format (default: md)")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="write to FILE (parent dirs are created; '-' = stdout). Default: md/json to stdout, "
                        "html to <dir>/.omnicli/team/report.html")
    p.add_argument("--include-logs", action="store_true",
                   help=f"include each task attempt's output log (each cut to ~{LOG_LIMIT // 1000}k chars)")
    p.set_defaults(func=handler)


# --------------------------------------------------------------------------- small helpers

def _s(v: Any) -> str:
    """Any state value as display text (None -> '', lists -> one item per line)."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "\n".join(_s(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _as_list(v: Any) -> List:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [x for x in v if x not in (None, "")]
    return [v]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fmt_duration(sec: Any) -> str:
    s = int(round(_num(sec)))
    if s <= 0:
        return "0s"
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _iso(epoch: Any) -> str:
    t = _num(epoch)
    if t <= 0:
        return ""
    try:
        return datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _short_time(epoch: Any) -> str:
    t = _num(epoch)
    if t <= 0:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def _k(n: Any) -> str:
    """Compact token number: 950, 12.3k, 1.2M."""
    n = int(_num(n))
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _q(p: Any) -> str:
    return shlex.quote(str(p))


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()
    return s[:60] or "run"


# --------------------------------------------------------------------------- which run

def resolve_run_dir(ref: Optional[str] = None, dir_opt: Optional[str] = None, cwd: Optional[Path] = None) -> Path:
    """-d DIR, else REF (runs.find_run), else the cwd if it holds a run, else the most recent run."""
    if dir_opt:
        d = Path(dir_opt).expanduser().resolve()
        if not (paths.project_state_dir(d) / "state.json").exists():
            raise ExportError(f"No OmniCLI team run in {d} (no .omnicli/team/state.json).\n"
                              f"  Start one with:  omnicli team \"<goal>\" -d {_q(d)}\n"
                              f"  Or list runs:    omnicli runs")
        return d
    if ref:
        rec = runs.find_run(ref)
        if not rec:
            raise ExportError(f"No team run matches '{ref}'.\n"
                              f"  List runs with:  omnicli runs\n"
                              f"  Or point at one: omnicli export -d <project-dir>")
        return Path(rec["dir"]).expanduser().resolve()
    here = Path(cwd) if cwd is not None else Path.cwd()
    if (paths.project_state_dir(here) / "state.json").exists():
        return here.expanduser().resolve()
    rec = runs.most_recent_run()
    if rec:
        return Path(rec["dir"]).expanduser().resolve()
    raise ExportError("Nothing to export yet: no OmniCLI team runs found.\n"
                      "  Start one with:  omnicli team \"<goal>\" -d <dir>\n"
                      "  Or point at one: omnicli export -d <project-dir>")


# --------------------------------------------------------------------------- data gathering

def read_attempt_log(logs_dir: Path, task_id: str, agent: str, n: int, limit: int = LOG_LIMIT) -> Dict[str, Any]:
    """The output log of attempt n (1-based) of a task: logs/<task>-<agent>-<n>.md, cut to ~limit chars."""
    name = f"{task_id}-{agent}-{n}.md"
    info: Dict[str, Any] = {"file": name, "path": "", "found": False, "chars": 0, "truncated": False, "text": ""}
    try:
        root = logs_dir.resolve()
        path = logs_dir / name
        if not path.is_file():
            # attempt may have been reassigned; any agent with the same task + attempt number will do
            pattern = f"{glob.escape(task_id)}-*-{n}.md"
            cands = sorted(p for p in logs_dir.glob(pattern)
                           if not p.name.endswith((".prompt.md", ".stderr.log")))
            if not cands:
                return info
            path = cands[0]
        rp = path.resolve()
        if root not in rp.parents:          # task ids come from AI output; never read outside logs/
            return info
        text = rp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return info
    total = len(text)
    info.update({"file": path.name, "path": str(path), "found": True, "chars": total})
    if total > limit:
        head = limit // 2
        tail = limit - head
        omitted = total - limit
        text = (text[:head]
                + f"\n\n[... {omitted:,} characters omitted by omnicli export (log was {total:,} chars) - "
                  f"full log: {path} ...]\n\n"
                + text[-tail:])
        info["truncated"] = True
    info["text"] = text
    return info


def gather_usage(run_dir: str, since: float = 0.0) -> Dict[str, Any]:
    """Per-agent / per-kind AI call totals for this run from the usage ledger (tokens are estimates)."""
    entries = ledger.read(since=since, run_dir=run_dir)
    by_agent = ledger.summarize(entries, "agent")
    by_kind = ledger.summarize(entries, "kind")
    totals = {"calls": 0, "ok": 0, "failed": 0, "seconds": 0.0, "est_tokens_in": 0, "est_tokens_out": 0}
    for g in by_agent.values():
        for key in totals:
            totals[key] += g.get(key, 0)
    for groups in (by_agent, by_kind):
        for g in groups.values():
            g["seconds"] = round(g["seconds"], 1)
    totals["seconds"] = round(totals["seconds"], 1)
    return {"estimated": True, "note": TOKEN_NOTE, "by_agent": by_agent, "by_kind": by_kind, "totals": totals}


def gather_git(project_dir: Path, task_ids: Optional[List[str]] = None, limit: int = GIT_LIMIT) -> Dict[str, Any]:
    """Git checkpoints touching the project dir (newest first), if it is a git work tree."""
    info: Dict[str, Any] = {"available": bool(shutil.which("git")), "repo": False, "commits": [],
                            "truncated": False, "limit": limit}
    if not info["available"]:
        return info

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(project_dir), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL)

    try:
        r = git("rev-parse", "--is-inside-work-tree")
        if r.returncode != 0 or r.stdout.strip() != "true":
            return info
        info["repo"] = True
        r = git("log", f"-n{limit + 1}", "--format=%h%x09%s%x09%an%x09%ad", "--date=iso", "--", ".")
    except (OSError, subprocess.SubprocessError):
        return info
    if r.returncode != 0:        # e.g. a fresh repo with no commits yet
        return info
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    if len(lines) > limit:
        info["truncated"] = True
        lines = lines[:limit]
    known = set(task_ids or [])
    for ln in lines:
        h, _, rest = ln.partition("\t")
        parts = rest.rsplit("\t", 2)
        subject, author, date = (parts if len(parts) == 3 else [rest, "", ""])
        m = re.match(r"([^\s:]+):\s", subject)
        info["commits"].append({"hash": h, "subject": subject, "author": author, "date": date,
                                "task_id": m.group(1) if m and m.group(1) in known else ""})
    return info


def gather(project_dir, include_logs: bool = False) -> Dict[str, Any]:
    """One plain dict describing the whole run - every format is rendered from this."""
    d = Path(project_dir).expanduser().resolve()
    state_dir = paths.project_state_dir(d)
    state = runs.load_state(d)
    if not isinstance(state, dict):
        raise ExportError(f"Could not read {state_dir / 'state.json'} (missing or not valid JSON).\n"
                          f"  List runs with:  omnicli runs")

    tasks: List[Dict[str, Any]] = []
    counts = {"total": 0, "done": 0, "failed": 0, "skipped": 0, "pending": 0, "running": 0}
    for raw in state.get("tasks") or []:
        if not isinstance(raw, dict):
            continue
        t = dict(raw)
        t["id"] = _s(t.get("id"))
        attempts = [dict(a) for a in (t.get("attempts") or []) if isinstance(a, dict)]
        if include_logs:
            for i, a in enumerate(attempts, start=1):
                a["log"] = read_attempt_log(state_dir / "logs", t["id"], _s(a.get("agent")), i)
        t["attempts"] = attempts
        t["tries"] = len(attempts)
        t["status"] = _s(t.get("status")) or "pending"
        counts["total"] += 1
        counts[t["status"]] = counts.get(t["status"], 0) + 1
        tasks.append(t)

    final = dict(state.get("final") or {}) if isinstance(state.get("final"), dict) else {}
    final["remaining_issues"] = _as_list(final.get("remaining_issues"))
    final["followup_tasks"] = _as_list(final.get("followup_tasks"))
    complete = bool(final.get("complete"))
    phase = _s(state.get("phase")) or "unknown"
    pid = int(_num(state.get("pid")))
    alive = phase == "running" and runs.pid_alive(pid)
    started = _num(state.get("started_at"))
    updated = _num(state.get("updated_at"))
    end = time.time() if alive else (updated or started)
    duration = max(0.0, end - started) if started else 0.0

    report_md = state_dir / "REPORT.md"
    return {
        "export": {"tool": "omnicli export", "version": __version__, "schema": 1,
                   "exported_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "include_logs": bool(include_logs)},
        "dir": str(d),
        "state_dir": str(state_dir),
        "logs_dir": str(state_dir / "logs"),
        "report_md": str(report_md) if report_md.exists() else "",
        "project_name": _s(state.get("project_name")) or d.name,
        "goal": _s(state.get("goal")),
        "phase": phase,
        "complete": complete,
        "outcome": "complete" if complete else "incomplete",
        "alive": alive,
        "lead": _s(state.get("lead")),
        "workers": [_s(w) for w in _as_list(state.get("workers"))],
        "round": int(_num(state.get("round"), 1)),
        "started_at": started,
        "updated_at": updated,
        "started_at_iso": _iso(started),
        "updated_at_iso": _iso(updated),
        "duration_sec": round(duration, 1),
        "plan_summary": _s(state.get("plan_summary")),
        "final": final,
        "counts": counts,
        "tasks": tasks,
        "usage": gather_usage(str(d), since=max(0.0, started - 1) if started else 0.0),
        "git": gather_git(d, [t["id"] for t in tasks]),
        "state_version": state.get("version"),
    }


# --------------------------------------------------------------------------- markdown

def _cell(v: Any) -> str:
    """One Markdown table cell: single line, pipes escaped."""
    text = " ".join(_s(v).split())
    return text.replace("|", "\\|") or "-"


def _fence(text: str, lang: str = "") -> str:
    """A fenced code block that survives backticks inside the text."""
    longest = max([len(r) for r in re.findall(r"`+", text or "")] + [2])
    f = "`" * max(3, longest + 1)
    return f"{f}{lang}\n{(text or '').rstrip()}\n{f}"


def _code_span(text: str) -> str:
    text = _s(text)
    longest = max([len(r) for r in re.findall(r"`+", text)] + [0])
    ticks = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def _quote(text: str) -> List[str]:
    return [("> " + ln).rstrip() for ln in _s(text).strip().splitlines()] or ["> (none)"]


def _attempt_label(a: Dict) -> str:
    rev = int(_num(a.get("revision")))
    return "first try" if rev == 0 else f"revision {rev}"


def _outcome_text(data: Dict) -> str:
    head = "COMPLETE" if data["complete"] else "INCOMPLETE"
    extra = ", still running" if data.get("alive") else ""
    return f"{head} (phase: {data['phase']}{extra}) after {data['round']} round(s)"


def _task_stats(data: Dict) -> str:
    c = data["counts"]
    bits = [f"{c.get('done', 0)} of {c.get('total', 0)} done"]
    for k in ("failed", "skipped", "running", "pending"):
        if c.get(k):
            bits.append(f"{c[k]} {k}")
    return ", ".join(bits)


def _followup_title(f: Any) -> str:
    if isinstance(f, dict):
        return _s(f.get("title") or f.get("name") or f.get("instructions") or json.dumps(f, ensure_ascii=False))
    return _s(f)


def render_md(data: Dict) -> str:
    L: List[str] = []
    final = data["final"]
    L += [f"# {' '.join(data['project_name'].split())} - OmniCLI team run", ""]
    if data["goal"]:
        L += _quote(data["goal"]) + [""]
    workers = ", ".join(data["workers"]) or "-"
    L += [
        f"- **Outcome:** {_outcome_text(data)}",
        f"- **Lead:** {data['lead'] or '-'} · **Workers:** {workers}",
        f"- **Tasks:** {_task_stats(data)}",
        f"- **Duration:** {fmt_duration(data['duration_sec'])} "
        f"(started {_short_time(data['started_at'])}, last update {_short_time(data['updated_at'])})",
        f"- **Project directory:** {_code_span(data['dir'])}",
        f"- **Exported:** {data['export']['exported_at']} by omnicli export v{data['export']['version']}",
        "",
    ]
    if data.get("alive"):
        L += ["> **Note:** this run is still in progress - the export is a snapshot.", ""]

    if data["plan_summary"].strip():
        L += ["## Approach", "", data["plan_summary"].strip(), ""]
    if _s(final.get("summary")).strip():
        L += ["## What was built", "", _s(final.get("summary")).strip(), ""]
    if _s(final.get("how_to_run")).strip():
        L += ["## How to run", "", _fence(_s(final.get("how_to_run"))), ""]

    L += ["## Tasks", ""]
    if data["tasks"]:
        L += ["| ID | Title | Worker | Status | Tries | Time | Commit |", "|---|---|---|---|---:|---:|---|"]
        for t in data["tasks"]:
            L.append("| " + " | ".join([
                _cell(t["id"]), _cell(t.get("title")), _cell(t.get("agent_used") or t.get("assignee")),
                _cell(t["status"]), str(t["tries"]), fmt_duration(t.get("duration_sec")),
                _cell(t.get("commit")),
            ]) + " |")
        L.append("")
    else:
        L += ["_No tasks were planned._", ""]

    if data["tasks"]:
        L += ["## Task details", ""]
        for t in data["tasks"]:
            L += _md_task(t)

    issues = final.get("remaining_issues") or []
    if issues:
        L += ["## Remaining issues", ""] + [f"- {' '.join(_s(i).split())}" for i in issues] + [""]
    followups = final.get("followup_tasks") or []
    if followups:
        L += ["## Follow-up tasks proposed by the lead", ""]
        L += [f"- {' '.join(_followup_title(f).split())}" for f in followups] + [""]

    L += _md_usage(data["usage"])
    L += _md_git(data["git"])
    L += ["---", "", f"Full prompts and agent output: {_code_span(data['logs_dir'])}", ""]
    return "\n".join(L)


def _md_task(t: Dict) -> List[str]:
    L = [f"### {' '.join(_s(t['id']).split())} - {' '.join(_s(t.get('title')).split())}", ""]
    worker = _s(t.get("agent_used") or t.get("assignee")) or "-"
    assigned = _s(t.get("assignee"))
    worker_txt = worker + (f" (assigned: {assigned})" if assigned and assigned != worker else "")
    commit = _code_span(t["commit"]) if _s(t.get("commit")) else "-"
    L.append(f"- **Status:** {t['status']} · **Worker:** {worker_txt} · **Round:** {_s(t.get('round')) or '1'} · "
             f"**Tries:** {t['tries']} · **Time:** {fmt_duration(t.get('duration_sec'))} · **Commit:** {commit}")
    deps = _as_list(t.get("depends_on"))
    if deps:
        L.append(f"- **Depends on:** {', '.join(_s(x) for x in deps)}")
    owns = _as_list(t.get("owns"))
    if owns:
        L.append(f"- **Owns:** {', '.join(_code_span(x) for x in owns)}")
    L.append("")
    if _s(t.get("instructions")).strip():
        L += ["**Instructions**", "", _s(t.get("instructions")).strip(), ""]
    if _s(t.get("acceptance")).strip():
        L += ["**Acceptance**", "", _s(t.get("acceptance")).strip(), ""]
    verify = _s(t.get("verify")).strip()
    if not verify:
        L += ["**Verify:** _none_", ""]
    elif "\n" in verify:
        L += ["**Verify:**", "", _fence(verify, "sh"), ""]
    else:
        L += [f"**Verify:** {_code_span(verify)}", ""]
    if _s(t.get("summary")).strip():
        L += ["**Result summary**", ""] + _quote(t.get("summary")) + [""]
    if _s(t.get("review_note")).strip():
        L += [f"**Review note:** {' '.join(_s(t.get('review_note')).split())}", ""]

    attempts = t.get("attempts") or []
    if attempts:
        L += ["**Attempts**", ""]
        for i, a in enumerate(attempts, start=1):
            bits = [f"{i}. **{_s(a.get('agent')) or '?'}** ({_attempt_label(a)}) - {_s(a.get('result')) or 'no result'}"]
            if a.get("exit_code") is not None:
                bits.append(f"exit {a.get('exit_code')}")
            if a.get("duration_sec") is not None:
                bits.append(fmt_duration(a.get("duration_sec")))
            if a.get("files_changed") is not None:
                bits.append(f"{a.get('files_changed')} file(s) changed")
            if a.get("verify_exit") is not None:
                bits.append(f"verify exit {a.get('verify_exit')}")
            if a.get("at"):
                bits.append(_s(a.get("at")))
            L.append(" · ".join(bits))
            if _s(a.get("feedback")).strip():
                L += ["", "   Feedback:", ""] + ["   " + ln for ln in _quote(a.get("feedback"))]
            L.append("")
        for i, a in enumerate(attempts, start=1):
            log = a.get("log")
            if not isinstance(log, dict):
                continue
            if not log.get("found"):
                L += [f"_Output log for attempt {i} ({_s(a.get('agent'))}) not found ({log.get('file')})._", ""]
                continue
            note = f", truncated from {log['chars']:,} chars" if log.get("truncated") else f", {log['chars']:,} chars"
            L += [f"**Output log - attempt {i} ({_s(a.get('agent'))}{note})**", "", _fence(log.get("text", "")), ""]
    return L


def _md_usage(u: Dict) -> List[str]:
    L = ["## Usage (estimated tokens)", ""]
    if not u["by_agent"]:
        L += ["_No AI calls for this run were found in the usage ledger._", ""]
        return L
    L += ["| Agent | Calls | OK | Failed | Time | ~Tokens in | ~Tokens out |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    for name, g in sorted(u["by_agent"].items(), key=lambda kv: -kv[1]["calls"]):
        L.append(f"| {_cell(name)} | {g['calls']} | {g['ok']} | {g['failed']} | {fmt_duration(g['seconds'])} | "
                 f"~{g['est_tokens_in']:,} | ~{g['est_tokens_out']:,} |")
    tot = u["totals"]
    L.append(f"| **Total** | **{tot['calls']}** | {tot['ok']} | {tot['failed']} | {fmt_duration(tot['seconds'])} | "
             f"~{tot['est_tokens_in']:,} | ~{tot['est_tokens_out']:,} |")
    kinds = ", ".join(f"{k or 'other'} {g['calls']}" for k, g in sorted(u["by_kind"].items()))
    L += ["", f"Calls by kind: {kinds}.", "", f"_{TOKEN_NOTE}_", ""]
    return L


def _md_git(g: Dict) -> List[str]:
    L = ["## Git checkpoints", ""]
    if not g["available"]:
        return L + ["_git is not installed - no checkpoints listed._", ""]
    if not g["repo"]:
        return L + ["_The project is not a git repository - no checkpoints._", ""]
    if not g["commits"]:
        return L + ["_No commits yet._", ""]
    L += ["| Commit | Message | Author | Date |", "|---|---|---|---|"]
    for c in g["commits"]:
        L.append(f"| {_cell(c['hash'])} | {_cell(c['subject'])} | {_cell(c['author'])} | {_cell(c['date'])} |")
    if g["truncated"]:
        L += ["", f"_Showing the newest {g['limit']} commits; run `git log` in the project for the rest._"]
    L.append("")
    return L


# --------------------------------------------------------------------------- json

def render_json(data: Dict) -> str:
    return json.dumps(data, indent=2, default=str, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- html

CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --bg:#f6f7f9;--surface:#ffffff;--surface-2:#f1f3f6;--text:#1b1f24;--muted:#5d6878;--border:#dde2e8;
  --accent:#4f46e5;--code-bg:#f2f4f7;
  --ok-bg:#e1f5e8;--ok-fg:#13652f;--bad-bg:#fde7e7;--bad-fg:#a8261c;--warn-bg:#fff0d4;--warn-fg:#7d4a00;
  --run-bg:#e1ebff;--run-fg:#1c47c4;--idle-bg:#eceff3;--idle-fg:#4a5361;
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#0e1014;--surface:#161a20;--surface-2:#1c2129;--text:#e5e8ec;--muted:#9aa4b1;--border:#2a303a;
    --accent:#9a9cff;--code-bg:#0b0d11;
    --ok-bg:#123321;--ok-fg:#86e3a8;--bad-bg:#3b1717;--bad-fg:#ffa39b;--warn-bg:#382a0e;--warn-fg:#f3c870;
    --run-bg:#15244a;--run-fg:#9cbaff;--idle-bg:#252a33;--idle-fg:#b6bec9;
  }
}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  overflow-wrap:break-word}
.wrap{max-width:980px;margin:0 auto;padding:28px 16px 56px}
h1,h2,h3{line-height:1.25;letter-spacing:-.01em}
h1{font-size:clamp(24px,5.5vw,34px);margin:4px 0 10px;overflow-wrap:anywhere}
h2{font-size:19px;margin:0 0 12px}
section{margin:32px 0}
.eyebrow{margin:0;font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.goal{margin:0 0 14px;font-size:17px;white-space:pre-wrap;overflow-wrap:anywhere}
.muted{color:var(--muted)}
.small{font-size:13px}
.outcome{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.note{background:var(--run-bg);color:var(--run-fg);border-radius:10px;padding:10px 14px;margin:14px 0 0}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:22px}
.stat{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 14px;min-width:0}
.stat .k{font-size:12px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}
.stat .v{font-size:20px;font-weight:650;margin-top:2px;overflow-wrap:anywhere}
.stat .s{font-size:12px;color:var(--muted);margin-top:2px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.prose{white-space:pre-wrap;overflow-wrap:anywhere}
code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace}
code{font-size:.88em;background:var(--code-bg);border:1px solid var(--border);padding:.05em .35em;border-radius:5px;
  overflow-wrap:anywhere}
pre{background:var(--code-bg);border:1px solid var(--border);border-radius:8px;padding:12px 14px;margin:8px 0;
  font-size:13px;line-height:1.5;white-space:pre-wrap;overflow-wrap:anywhere;overflow-x:auto}
pre code{background:none;border:0;padding:0;font-size:inherit}
pre.log{max-height:520px;overflow:auto}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;background:var(--surface);border:1px solid var(--border);
  border-radius:12px;max-width:100%}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:9px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--border)}
th{background:var(--surface-2);color:var(--muted);font-size:12px;font-weight:600;letter-spacing:.04em;
  text-transform:uppercase;white-space:nowrap}
tbody tr:last-child td{border-bottom:0}
tfoot td{border-top:1px solid var(--border);border-bottom:0;font-weight:600}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.nowrap{white-space:nowrap}
td.wide{min-width:14em}
td a{color:inherit;text-decoration:none;border-bottom:1px dotted var(--muted)}
.pill{display:inline-block;padding:1px 10px;border-radius:999px;font-size:12px;font-weight:600;line-height:1.7;
  white-space:nowrap;background:var(--idle-bg);color:var(--idle-fg)}
.pill.ok{background:var(--ok-bg);color:var(--ok-fg)}
.pill.bad{background:var(--bad-bg);color:var(--bad-fg)}
.pill.warn{background:var(--warn-bg);color:var(--warn-fg)}
.pill.run{background:var(--run-bg);color:var(--run-fg)}
details.task{background:var(--surface);border:1px solid var(--border);border-radius:12px;margin:10px 0;overflow:hidden}
details.task>summary{cursor:pointer;list-style:none;display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;
  padding:12px 16px}
details.task>summary::-webkit-details-marker{display:none}
details.task>summary::before{content:"\\25B8";color:var(--muted);transition:transform .15s ease}
details.task[open]>summary::before{transform:rotate(90deg)}
details.task[open]>summary{border-bottom:1px solid var(--border)}
details.task>summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.tid{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;color:var(--muted)}
.ttitle{font-weight:600;flex:1 1 12em;min-width:0;overflow-wrap:anywhere}
.tmeta{font-size:13px;color:var(--muted);white-space:nowrap}
.task-body{padding:14px 16px 16px}
.task-body h4{margin:16px 0 6px;font-size:13px;letter-spacing:.04em;text-transform:uppercase;color:var(--muted)}
.task-body h4:first-child{margin-top:0}
dl.kv{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:4px 16px;margin:0;font-size:14px}
dl.kv dt{color:var(--muted)}
dl.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
ol.attempts{margin:0;padding-left:22px}
ol.attempts>li{margin:8px 0}
.attempt-head{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}
.feedback{margin:6px 0 0;padding:8px 12px;border-left:3px solid var(--border);color:var(--text);
  white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}
details.log{margin:8px 0}
details.log>summary{cursor:pointer;font-size:13px;color:var(--muted)}
ul.issues{margin:0;padding-left:20px}
ul.issues li{margin:4px 0;overflow-wrap:anywhere}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--border);font-size:13px;color:var(--muted);
  overflow-wrap:anywhere}
@media (max-width:480px){
  body{font-size:14px}
  .wrap{padding-top:20px}
  .stat .v{font-size:18px}
  th,td{padding:8px 10px}
  details.task>summary,.task-body{padding-left:12px;padding-right:12px}
}
"""


def _e(v: Any) -> str:
    return html.escape(_s(v), quote=True)


def _pill_class(status: str) -> str:
    s = _s(status).lower()
    if s in ("done", "approved", "complete", "ok"):
        return "ok"
    if s in ("failed", "error") or s.startswith("agent error"):
        return "bad"
    if s in ("revise", "incomplete", "stopped"):
        return "warn"
    if s in ("running", "planning"):
        return "run"
    return "idle"


def _pill(text: str, cls: Optional[str] = None) -> str:
    return f'<span class="pill {cls or _pill_class(text)}">{_e(text)}</span>'


def _anchor(task_id: str) -> str:
    return "task-" + (re.sub(r"[^A-Za-z0-9_-]+", "-", _s(task_id)).strip("-") or "x")


def _html_outcome_pill(data: Dict) -> str:
    if data["complete"]:
        return _pill("Complete", "ok")
    if data.get("alive"):
        return _pill("Running", "run")
    if data["phase"] == "failed":
        return _pill("Failed", "bad")
    return _pill("Incomplete", "warn")


def render_html(data: Dict) -> str:
    final = data["final"]
    c = data["counts"]
    u = data["usage"]
    title = f"{data['project_name']} - OmniCLI team run"
    P: List[str] = []
    a = P.append

    a("<!doctype html>")
    a('<html lang="en">')
    a("<head>")
    a('<meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width, initial-scale=1">')
    a('<meta name="color-scheme" content="light dark">')
    # Self-contained by design: nothing may load from anywhere, and no script may run.
    a("<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">")
    a(f'<meta name="generator" content="omnicli export v{_e(data["export"]["version"])}">')
    a(f"<title>{_e(title)}</title>")
    a(f"<style>{CSS}</style>")
    a("</head>")
    a("<body>")
    a('<main class="wrap">')

    # ---- header
    a("<header>")
    a('<p class="eyebrow">OmniCLI team run</p>')
    a(f"<h1>{_e(data['project_name'])}</h1>")
    if data["goal"]:
        a(f'<p class="goal">{_e(data["goal"].strip())}</p>')
    a(f'<div class="outcome">{_html_outcome_pill(data)}'
      f'<span class="muted small">phase {_e(data["phase"])} · {_e(data["round"])} round(s) · '
      f'<code>{_e(data["dir"])}</code></span></div>')
    if data.get("alive"):
        a('<p class="note small">This run is still in progress - this export is a snapshot.</p>')

    sub_bits = [f"{c.get(k)} {k}" for k in ("failed", "skipped", "running", "pending") if c.get(k)]
    tot = u["totals"]
    tokens = tot["est_tokens_in"] + tot["est_tokens_out"]
    stats = [
        ("Tasks done", f"{c.get('done', 0)}/{c.get('total', 0)}", ", ".join(sub_bits) or "nothing failed"),
        ("Duration", fmt_duration(data["duration_sec"]), f"started {_short_time(data['started_at'])}"),
        ("Lead", data["lead"] or "-", "plans and reviews"),
        ("Workers", ", ".join(data["workers"]) or "-", f"{len(data['workers'])} AI CLI(s)"),
        ("AI calls", str(tot["calls"]), f"~{_k(tokens)} tokens (estimated)"),
    ]
    a('<div class="stats">')
    for k, v, s in stats:
        a(f'<div class="stat"><div class="k">{_e(k)}</div><div class="v">{_e(v)}</div>'
          f'<div class="s">{_e(s)}</div></div>')
    a("</div>")
    a("</header>")

    # ---- narrative
    if data["plan_summary"].strip():
        a(f'<section><h2>Approach</h2><div class="card prose">{_e(data["plan_summary"].strip())}</div></section>')
    if _s(final.get("summary")).strip():
        a(f'<section><h2>What was built</h2><div class="card prose">{_e(_s(final.get("summary")).strip())}</div>'
          f"</section>")
    if _s(final.get("how_to_run")).strip():
        a(f"<section><h2>How to run</h2><pre><code>{_e(_s(final.get('how_to_run')).strip())}</code></pre></section>")

    # ---- tasks table
    a("<section><h2>Tasks</h2>")
    if data["tasks"]:
        a('<div class="table-wrap"><table><thead><tr><th>ID</th><th>Task</th><th>Worker</th><th>Status</th>'
          '<th class="num">Tries</th><th class="num">Time</th><th>Commit</th></tr></thead><tbody>')
        for t in data["tasks"]:
            a(f'<tr><td class="nowrap"><a href="#{_anchor(t["id"])}">{_e(t["id"])}</a></td>'
              f'<td class="wide">{_e(t.get("title"))}</td>'
              f'<td class="nowrap">{_e(t.get("agent_used") or t.get("assignee") or "-")}</td>'
              f"<td>{_pill(t['status'])}</td>"
              f'<td class="num">{t["tries"]}</td>'
              f'<td class="num">{_e(fmt_duration(t.get("duration_sec")))}</td>'
              f'<td class="nowrap">{("<code>" + _e(t["commit"]) + "</code>") if _s(t.get("commit")) else "-"}</td>'
              f"</tr>")
        a("</tbody></table></div>")
    else:
        a('<p class="muted">No tasks were planned.</p>')
    a("</section>")

    # ---- per-task details
    if data["tasks"]:
        a("<section><h2>Task details</h2>")
        for t in data["tasks"]:
            P.extend(_html_task(t))
        a("</section>")

    # ---- remaining issues / follow-ups
    issues = final.get("remaining_issues") or []
    if issues:
        a('<section><h2>Remaining issues</h2><div class="card"><ul class="issues">')
        P.extend(f"<li>{_e(i)}</li>" for i in issues)
        a("</ul></div></section>")
    followups = final.get("followup_tasks") or []
    if followups:
        a('<section><h2>Follow-up tasks proposed by the lead</h2><div class="card"><ul class="issues">')
        P.extend(f"<li>{_e(_followup_title(f))}</li>" for f in followups)
        a("</ul></div></section>")

    # ---- usage
    a('<section><h2>Usage <span class="muted small">(estimated tokens)</span></h2>')
    if u["by_agent"]:
        a('<div class="table-wrap"><table><thead><tr><th>Agent</th><th class="num">Calls</th><th class="num">OK</th>'
          '<th class="num">Failed</th><th class="num">Time</th><th class="num">~Tokens in</th>'
          '<th class="num">~Tokens out</th></tr></thead><tbody>')
        for name, g in sorted(u["by_agent"].items(), key=lambda kv: -kv[1]["calls"]):
            a(f"<tr><td>{_e(name or '?')}</td><td class=\"num\">{g['calls']}</td><td class=\"num\">{g['ok']}</td>"
              f"<td class=\"num\">{g['failed']}</td><td class=\"num\">{_e(fmt_duration(g['seconds']))}</td>"
              f"<td class=\"num\">~{g['est_tokens_in']:,}</td><td class=\"num\">~{g['est_tokens_out']:,}</td></tr>")
        a(f"</tbody><tfoot><tr><td>Total</td><td class=\"num\">{tot['calls']}</td><td class=\"num\">{tot['ok']}</td>"
          f"<td class=\"num\">{tot['failed']}</td><td class=\"num\">{_e(fmt_duration(tot['seconds']))}</td>"
          f"<td class=\"num\">~{tot['est_tokens_in']:,}</td><td class=\"num\">~{tot['est_tokens_out']:,}</td>"
          f"</tr></tfoot></table></div>")
        kinds = ", ".join(f"{k or 'other'} {g['calls']}" for k, g in sorted(u["by_kind"].items()))
        a(f'<p class="muted small">Calls by kind: {_e(kinds)}. {_e(TOKEN_NOTE)}</p>')
    else:
        a('<p class="muted">No AI calls for this run were found in the usage ledger.</p>')
    a("</section>")

    # ---- git
    g = data["git"]
    a("<section><h2>Git checkpoints</h2>")
    if not g["available"]:
        a('<p class="muted">git is not installed - no checkpoints listed.</p>')
    elif not g["repo"]:
        a('<p class="muted">The project is not a git repository - no checkpoints.</p>')
    elif not g["commits"]:
        a('<p class="muted">No commits yet.</p>')
    else:
        a('<div class="table-wrap"><table><thead><tr><th>Commit</th><th>Message</th><th>Author</th><th>Date</th>'
          "</tr></thead><tbody>")
        for cm in g["commits"]:
            a(f'<tr><td class="nowrap"><code>{_e(cm["hash"])}</code></td><td class="wide">{_e(cm["subject"])}</td>'
              f'<td class="nowrap">{_e(cm["author"])}</td><td class="nowrap">{_e(cm["date"])}</td></tr>')
        a("</tbody></table></div>")
        if g["truncated"]:
            a(f'<p class="muted small">Showing the newest {g["limit"]} commits; run <code>git log</code> '
              f"in the project for the rest.</p>")
    a("</section>")

    a(f"<footer>Exported {_e(data['export']['exported_at'])} by omnicli export v{_e(data['export']['version'])}. "
      f"Full prompts and agent output: <code>{_e(data['logs_dir'])}</code></footer>")
    a("</main>")
    a("</body>")
    a("</html>")
    return "\n".join(P) + "\n"


def _html_task(t: Dict) -> List[str]:
    P: List[str] = []
    a = P.append
    open_attr = " open" if t["status"] == "failed" else ""
    worker = _s(t.get("agent_used") or t.get("assignee")) or "-"
    a(f'<details class="task" id="{_anchor(t["id"])}"{open_attr}>')
    a(f'<summary><span class="tid">{_e(t["id"])}</span><span class="ttitle">{_e(t.get("title"))}</span>'
      f'{_pill(t["status"])}<span class="tmeta">{_e(worker)} · {t["tries"]} tr{"y" if t["tries"] == 1 else "ies"} · '
      f'{_e(fmt_duration(t.get("duration_sec")))}</span></summary>')
    a('<div class="task-body">')

    rows = [("Worker", _e(worker)), ("Assigned to", _e(t.get("assignee") or "-")),
            ("Round", _e(t.get("round") or 1))]
    if _s(t.get("commit")):
        rows.append(("Commit", f"<code>{_e(t['commit'])}</code>"))
    deps = _as_list(t.get("depends_on"))
    if deps:
        rows.append(("Depends on", _e(", ".join(_s(x) for x in deps))))
    owns = _as_list(t.get("owns"))
    if owns:
        rows.append(("Owns", " ".join(f"<code>{_e(x)}</code>" for x in owns)))
    verify = _s(t.get("verify")).strip()
    rows.append(("Verify", f"<code>{_e(verify)}</code>" if verify else '<span class="muted">none</span>'))
    a('<dl class="kv">' + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>")

    for label, key in (("Instructions", "instructions"), ("Acceptance", "acceptance"), ("Result summary", "summary")):
        if _s(t.get(key)).strip():
            a(f'<h4>{label}</h4><div class="prose">{_e(_s(t.get(key)).strip())}</div>')
    if _s(t.get("review_note")).strip():
        a(f'<h4>Review note</h4><div class="prose">{_e(_s(t.get("review_note")).strip())}</div>')

    attempts = t.get("attempts") or []
    if attempts:
        a('<h4>Attempts</h4><ol class="attempts">')
        for i, at in enumerate(attempts, start=1):
            result = _s(at.get("result")) or "no result"
            short, _, rest = result.partition(": ") if result.startswith("agent error") else (result, "", "")
            meta = [_attempt_label(at)]
            if at.get("exit_code") is not None:
                meta.append(f"exit {at.get('exit_code')}")
            if at.get("duration_sec") is not None:
                meta.append(fmt_duration(at.get("duration_sec")))
            if at.get("files_changed") is not None:
                meta.append(f"{at.get('files_changed')} file(s) changed")
            if at.get("verify_exit") is not None:
                meta.append(f"verify exit {at.get('verify_exit')}")
            if at.get("at"):
                meta.append(_s(at.get("at")))
            a(f'<li><div class="attempt-head"><strong>{_e(at.get("agent") or "?")}</strong>{_pill(short)}'
              f'<span class="muted small">{_e(" · ".join(meta))}</span></div>')
            if rest:
                a(f'<div class="feedback">{_e(rest)}</div>')
            if _s(at.get("feedback")).strip():
                a(f'<div class="feedback">{_e(_s(at.get("feedback")).strip())}</div>')
            log = at.get("log")
            if isinstance(log, dict):
                if log.get("found"):
                    size = (f"truncated from {log['chars']:,} chars" if log.get("truncated")
                            else f"{log['chars']:,} chars")
                    a(f'<details class="log"><summary>Output log ({_e(size)})</summary>'
                      f'<pre class="log"><code>{_e(log.get("text", ""))}</code></pre></details>')
                else:
                    a(f'<p class="muted small">Output log not found ({_e(log.get("file"))}).</p>')
            a("</li>")
        a("</ol>")
    a("</div>")
    a("</details>")
    return P


# --------------------------------------------------------------------------- output

RENDERERS = {"md": render_md, "json": render_json, "html": render_html}


def render(data: Dict, fmt: str) -> str:
    return RENDERERS[fmt](data)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _write_stdout(text: str) -> None:
    if not text.endswith("\n"):
        text += "\n"
    try:
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            sys.stdout.flush()
            sys.stdout.buffer.write(text.encode("utf-8"))
        sys.stdout.flush()
    except BrokenPipeError:          # `omnicli export | head` - stop quietly
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except Exception:
            pass


def default_html_path(project_dir) -> Path:
    return paths.project_state_dir(project_dir) / "report.html"


def handler(args) -> int:
    fmt = getattr(args, "format", None) or "md"
    if fmt not in RENDERERS:
        _err(f"{BADGE_WARN} Unknown format '{fmt}'. Use one of: {', '.join(FORMATS)}")
        return 2
    try:
        project_dir = resolve_run_dir(getattr(args, "ref", None), getattr(args, "dir", None))
        data = gather(project_dir, include_logs=bool(getattr(args, "include_logs", False)))
    except ExportError as ex:
        _err(f"{BADGE_WARN} {ex}")
        return 1
    text = render(data, fmt)

    out = getattr(args, "output", None)
    if out == "-":
        _write_stdout(text)
        return 0
    if out:
        path = Path(out).expanduser()
        if path.is_dir():
            path = path / f"{_slug(data['project_name'])}-omnicli-run.{EXTENSIONS[fmt]}"
    elif fmt == "html":
        path = default_html_path(project_dir)
    else:
        _write_stdout(text)
        return 0

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as ex:
        _err(f"{BADGE_WARN} Could not write {path}: {ex}\n  Try another location:  omnicli export -o <file>")
        return 1
    label = {"md": "Markdown", "json": "JSON", "html": "HTML report"}[fmt]
    print(f"{BADGE_SUCCESS} {label} for {c_bold(data['project_name'])} written to {path.resolve()}")
    if fmt == "html":
        opener = "open " if sys.platform == "darwin" else ""
        print(c_dim(f"  Self-contained - open it in any browser:  {opener}{_q(path.resolve())}"))
    return 0
