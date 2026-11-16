"""
`omnicli review` - like Claude Code's /review, for any project folder.

A senior AI reviewer (read-only) inspects the project and replies with a score, a summary,
findings sorted by severity and follow-up tasks. `--fix` turns those follow-up tasks into
an `omnicli team` run: the reviewer leads, the other ready AI CLIs do the fixing.

    omnicli review                          review the current directory
    omnicli review security error handling  focus the review
    omnicli review -d ./app --json          machine-readable result
    omnicli review --fix -y                 review, then have the team fix high/medium findings

Files written (inside the reviewed project):
    .omnicli/review/review-<YYYYmmdd-HHMMSS>.md  raw reviewer output (+ .prompt.md, .stderr.log)
    .omnicli/review/latest.json                  the parsed review
    .omnicli/team/state.json                     --fix only: the planned team run
"""

from __future__ import annotations

import datetime
import json
import shutil
import sys
import time
from dataclasses import asdict
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from omnicli import agents
from omnicli import paths
from omnicli import runs as run_registry
from omnicli import settings
from omnicli.conductor import (
    TASK_JSON_SHAPE,
    Conductor,
    Task,
    TeamConfig,
    first_line,
    list_tree,
    normalize_tasks,
    tool_report,
)
from omnicli.ui import (
    BADGE_FAILED,
    BADGE_INFO,
    BADGE_SUCCESS,
    BADGE_WARN,
    c_bold,
    c_cyan,
    c_dim,
    c_green,
    c_magenta,
    c_red,
    c_yellow,
    print_table,
)

NAMES = ["review"]

HEADER = "[OmniCLI team - project review]"
RETRY_NOTE = ("\n\nIMPORTANT: your previous reply could not be parsed - reply with ONLY the JSON object "
              "described above, nothing else.")
SEVERITIES = ("high", "medium", "low")
_SEVERITY_ALIASES = {
    "critical": "high", "blocker": "high", "severe": "high", "major": "high", "error": "high", "high": "high",
    "medium": "medium", "med": "medium", "moderate": "medium", "warning": "medium", "warn": "medium",
    "low": "low", "minor": "low", "info": "low", "nit": "low", "trivial": "low", "style": "low",
    "suggestion": "low",
}
# Files worth showing the reviewer up front (excerpts); it can read everything else itself.
KEY_FILES = [
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "build.gradle.kts", "Gemfile", "composer.json",
    "deno.json", "tsconfig.json", "Makefile", "Dockerfile", "docker-compose.yml", "compose.yaml",
]
MAX_FILE_CHARS = 3000
MAX_KEY_FILES_CHARS = 12000

# Test hooks: tests replace these to inject fake adapters / health / a fake Conductor.
_load_specs = agents.load_agent_specs
_check_health = agents.check_health
_make_conductor = lambda cfg: Conductor(cfg)  # noqa: E731


# --------------------------------------------------------------------------- parser

def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "review",
        help="A senior AI reviewer reads a project (read-only) and reports findings; --fix has the team fix them",
        description="Review any project folder with a read-only AI reviewer (like Claude Code's /review). "
                    "Optional FOCUS words steer the review, e.g. `omnicli review security input validation`.",
    )
    p.add_argument("focus", nargs="*", help="What to focus on (optional), e.g. security, tests, error handling")
    p.add_argument("-d", "--dir", help="Project directory to review (default: current directory)")
    p.add_argument("--lead", help="AI CLI that reviews (default: `lead` setting, else the best ready one)")
    p.add_argument("--image", action="append", default=None, metavar="FILE",
                   help="Attach a screenshot or mockup for visual review (repeatable)")
    p.add_argument("--json", action="store_true", help="Print the review as JSON (progress goes to stderr)")
    p.add_argument("--fix", action="store_true",
                   help="Turn the follow-up tasks into a team run that fixes the high/medium findings")
    p.add_argument("-y", "--yes", action="store_true", help="With --fix: start the team without confirming the plan")
    p.add_argument("--max-tasks", type=int, default=None, help="Most follow-up tasks (default: max_tasks setting, 10)")
    p.add_argument("--timeout", type=float, default=None,
                   help="Minutes before the reviewer is stopped (default: lead_timeout setting, 10)")
    p.add_argument("-v", "--verbose", action="store_true", help="Show each finding's full detail and every file")
    p.set_defaults(func=handler)


# --------------------------------------------------------------------------- helpers

class _Out:
    """Human progress goes to stdout, or to stderr when --json owns stdout."""

    def __init__(self, to_stderr: bool):
        self.stream = sys.stderr if to_stderr else sys.stdout

    def __call__(self, msg: str = "") -> None:
        print(msg, file=self.stream, flush=True)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _resolve_dir(raw: Optional[str]) -> Path:
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def _review_dir(target: Path) -> Path:
    return Path(target) / ".omnicli" / "review"


def _unfinished_run(target: Path) -> Optional[str]:
    """Phase of an existing team run that is not complete ('unreadable' if state.json is corrupt), else None."""
    state_file = paths.project_state_dir(target) / "state.json"
    if not state_file.exists():
        return None
    state = run_registry.load_state(target)
    if not isinstance(state, dict):
        return "unreadable"
    phase = str(state.get("phase") or "planned")
    return None if phase == "complete" else phase


def _agent_order(specs: Dict[str, Dict]) -> List[str]:
    return [n for n in agents.PREFERRED_LEAD_ORDER if n in specs] + \
           [n for n in specs if n not in agents.PREFERRED_LEAD_ORDER]


def pick_reviewer(specs: Dict[str, Dict], health: Dict[str, Dict], order: List[str],
                  wanted: Optional[str], from_flag: bool) -> Tuple[Optional[str], str]:
    """(reviewer, problem). An explicit --lead that isn't ready is an error; a saved
    `lead` setting that isn't ready falls back to the first ready agent with a warning."""
    ready = [n for n in order if health.get(n, {}).get("ok")]
    if wanted and wanted in specs and health.get(wanted, {}).get("ok"):
        return wanted, ""
    if wanted and from_flag:
        if wanted not in specs:
            return None, (f"unknown agent '{wanted}'. Known: {', '.join(order) or '(none)'} - "
                          f"run `omnicli agents` to see them.")
        why = health.get(wanted, {}).get("reason") or "not ready"
        return None, (f"'{wanted}' cannot review right now ({why}). Log in to it, check with "
                      f"`omnicli agents --check`, or pick another reviewer with --lead.")
    if not ready:
        return None, ("no AI CLI is ready to review. Log in to at least one (e.g. `claude`, `codex login`, `agy`) "
                      "and check with `omnicli agents --check`.")
    return ready[0], ""


def _key_file_excerpts(target: Path) -> str:
    names: List[str] = []
    try:
        names += sorted(p.name for p in target.iterdir() if p.is_file() and p.name.lower().startswith("readme"))
    except Exception:
        pass
    names += [n for n in KEY_FILES if (target / n).is_file()]
    blocks: List[str] = []
    total = 0
    for name in dict.fromkeys(names):
        try:
            text = (target / name).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + f"\n... (truncated, {len(text)} chars - read the file for the rest)"
        if total + len(text) > MAX_KEY_FILES_CHARS:
            blocks.append(f"--- {name} (not shown - read it yourself)")
            continue
        total += len(text)
        blocks.append(f"--- {name}\n{text.rstrip()}")
    return "\n\n".join(blocks)


def _team_block(target: Path) -> str:
    state = run_registry.load_state(target)
    if not isinstance(state, dict):
        return ""
    lines = [
        "This project was built (or is being built) by an OmniCLI team run.",
        f"Team goal: {state.get('goal') or '(unknown)'}",
        f"Run phase: {state.get('phase') or '(unknown)'}",
    ]
    if state.get("plan_summary"):
        lines.append(f"Plan summary: {state['plan_summary']}")
    tasks = state.get("tasks") or []
    if tasks:
        lines.append("Tasks:")
        for t in tasks[:40]:
            if not isinstance(t, dict):
                continue
            note = f" | lead note: {first_line(str(t.get('review_note') or ''))}" if t.get("review_note") else ""
            lines.append(f"- {t.get('id', '?')} [{t.get('status', '?')}] ({t.get('agent_used') or t.get('assignee') or '?'}) "
                         f"{t.get('title', '')}: {first_line(str(t.get('summary') or ''))}{note}")
    issues = (state.get("final") or {}).get("remaining_issues") if isinstance(state.get("final"), dict) else None
    if issues:
        lines.append("Remaining issues reported at the end of that run:")
        lines += [f"- {i}" for i in issues[:20]]
    return "\n".join(lines)


def _workers_block(specs: Dict[str, Dict], workers: List[str]) -> str:
    return "\n".join(
        f"- {w}: {specs.get(w, {}).get('name', w)} - strengths: {specs.get(w, {}).get('strengths', 'general coding')}"
        for w in workers
    )


def build_review_prompt(target: Path, focus: str, specs: Dict[str, Dict], workers: List[str],
                        installed: List[str], missing: List[str], max_tasks: int,
                        images: Optional[List[str]] = None) -> str:
    key_files = _key_file_excerpts(target)
    team = _team_block(target)
    focus_block = focus or ("(none given) - do a general review: correctness bugs, security, error handling, "
                            "tests, maintainability, documentation and how easy it is to run.")
    parts = [
        HEADER,
        "You are a SENIOR SOFTWARE ENGINEER doing a thorough, honest code review of the project below.",
        "This is a READ-ONLY review: do NOT create, modify or delete any files and do not run anything that changes "
        "the project. Read whatever files you need inside the project directory.",
        "",
        f"PROJECT DIRECTORY: {target}",
        "Contents:",
        list_tree(target),
        "",
        "REVIEW FOCUS (from the user):",
        focus_block,
    ]
    if key_files:
        parts += ["", "README AND MANIFEST EXCERPTS:", key_files]
    if team:
        parts += ["", "TEAM RUN CONTEXT:", team]
    if images:
        parts += ["", "VISUAL REFERENCES:"]
        parts.extend("- {}".format(image) for image in images)
        parts.append("Inspect the supplied screenshots for layout, visual hierarchy, responsive behavior, and visible UI defects.")
    parts += [
        "",
        f"Tools installed on this machine: {', '.join(installed) or 'unknown'}",
        f"NOT installed: {', '.join(missing) or '(nothing notable)'}",
        "",
        "WORKERS who will fix your findings (assign every follow-up task to one of these ids):",
        _workers_block(specs, workers),
        "",
        "RULES:",
        "- Findings must be concrete and verifiable from the code: name the files (relative paths), explain the problem "
        "in \"detail\", and say exactly what to change in \"fix\". No vague advice, no praise-only findings.",
        "- severity: \"high\" = bugs, security holes, data loss, crashes, broken build/tests; \"medium\" = missing error "
        "handling, missing tests for important paths, fragile or confusing code; \"low\" = style, naming, docs, nits.",
        "- score: overall quality from 0 to 10 (10 = production ready, 5 = works but needs real work, 0 = broken).",
        f"- followup_tasks: at most {max_tasks} tasks that fix the HIGH and MEDIUM findings (not the low ones), each "
        "doable by one agent in one session, with self-contained instructions naming the files to change. "
        "\"owns\" lists the paths a task changes; tasks that could run at the same time must not share paths. "
        "\"verify\" is an optional shell command using only installed tools that exits 0 when the fix works "
        "(\"\" if none makes sense; never installs, servers or network calls). Use [] if nothing needs fixing.",
        "",
        "Reply with ONLY a JSON object (no prose) of this shape:",
        '{"summary": "2-4 sentences on the overall state of the project", "score": 7,',
        ' "findings": [{"severity": "high", "title": "short title", "detail": "what is wrong and why it matters",',
        '               "files": ["relative/path.py"], "fix": "exactly what to change"}],',
        f' "followup_tasks": [{TASK_JSON_SHAPE}]}}',
    ]
    return "\n".join(parts) + "\n"


def _as_str_list(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _score(value) -> Optional[float]:
    try:
        s = float(value)
    except (TypeError, ValueError):
        return None
    if s != s:  # NaN
        return None
    s = max(0.0, min(10.0, s))
    return int(s) if s == int(s) else round(s, 1)


def normalize_review(data: Dict) -> Dict:
    """Clean the reviewer's JSON: known severities, sorted high -> medium -> low, list-typed fields."""
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        sev = _SEVERITY_ALIASES.get(str(f.get("severity") or "").strip().lower(), "medium")
        findings.append({
            "severity": sev,
            "title": str(f.get("title") or "").strip() or first_line(str(f.get("detail") or "")) or "(untitled)",
            "detail": str(f.get("detail") or "").strip(),
            "files": _as_str_list(f.get("files") or f.get("file")),
            "fix": str(f.get("fix") or "").strip(),
        })
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))  # stable: keeps the reviewer's order within a level
    raw_tasks = data.get("followup_tasks")
    return {
        "summary": str(data.get("summary") or "").strip(),
        "score": _score(data.get("score")),
        "findings": findings,
        "followup_tasks": [t for t in raw_tasks if isinstance(t, dict)] if isinstance(raw_tasks, list) else [],
    }


def _severity_cell(sev: str, pad: bool = True) -> str:
    # padded before colouring: every colour code has the same length, so table columns stay aligned
    label = f"{sev.upper():<6}" if pad else sev.upper()
    return {"high": c_red, "medium": c_yellow}.get(sev, c_cyan)(label)


def _score_text(score) -> str:
    if score is None:
        return c_dim("n/a")
    text = f"{score}/10"
    return c_green(text) if score >= 8 else c_yellow(text) if score >= 5 else c_red(text)


def print_review(result: Dict, verbose: bool = False) -> None:
    findings = result.get("findings") or []
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    print()
    print(f"{c_bold('Review of')} {c_bold(result.get('dir', ''))} {c_dim('by')} {c_magenta(result.get('reviewer', '?'))}")
    print(f"{c_bold('Score:')}   {_score_text(result.get('score'))}")
    if result.get("summary"):
        print(f"{c_bold('Summary:')} {result['summary']}")
    if not findings:
        print(f"\n{BADGE_SUCCESS} No findings - the reviewer found nothing to fix.")
        return
    rows = []
    for i, f in enumerate(findings, start=1):
        files = ", ".join(f["files"])
        if not verbose and len(files) > 40:
            files = files[:37] + "..."
        title = f["title"] if verbose or len(f["title"]) <= 60 else f["title"][:57] + "..."
        rows.append([str(i), _severity_cell(f["severity"]), title, files or c_dim("-")])
    title = f"Findings ({len(findings)}: {counts['high']} high, {counts['medium']} medium, {counts['low']} low)"
    print_table(["#", "Severity", "Title", "Files"], rows, title=title)
    for i, f in enumerate(findings, start=1):
        print(f"{c_bold(f'{i}.')} {_severity_cell(f['severity'], pad=False)} {f['title']}")
        if verbose and f["detail"]:
            print(f"   {c_dim('Detail:')} {f['detail']}")
        print(f"   {c_dim('Fix:')} {f['fix'] or '(no fix suggested)'}")
    print()


def save_review(target: Path, result: Dict) -> Path:
    d = _review_dir(target)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "latest.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    return path


def run_review(spec: Dict, prompt: str, target: Path, timeout_min: float, stamp: str,
               say) -> Tuple[Optional[Dict], Path, str]:
    """(parsed JSON or None, log path of the last call, error). One retry if the reply can't be parsed."""
    base, n = f"review-{stamp}", 2
    while (_review_dir(target) / f"{base}.md").exists():  # two reviews in the same second keep both logs
        base, n = f"review-{stamp}-{n}", n + 1
    stamp = base[len("review-"):]
    log = _review_dir(target) / f"review-{stamp}.md"
    p = prompt
    for attempt in (1, 2):
        if attempt == 2:
            log = _review_dir(target) / f"review-{stamp}-retry.md"
            say(f"{BADGE_WARN} The reply was not valid review JSON; asking once more for JSON only...")
        run = agents.run_agent(spec, p, "readonly", str(target), timeout=timeout_min * 60, log_path=log,
                               meta={"kind": "project-review", "run_dir": str(target)})
        if not run.ok:
            hint = ""
            if run.auth_problem:
                hint = " Log in to it again, or pick another reviewer with --lead."
            return None, log, f"{spec.get('id', '?')} failed: {run.failure_reason()}.{hint}"
        data = agents.extract_json(run.output, "findings")
        if data is not None:
            return data, log, ""
        p = prompt + RETRY_NOTE
    return None, log, f"{spec.get('id', '?')} did not reply with review JSON (asked twice)."


# --------------------------------------------------------------------------- --fix

def write_fix_state(target: Path, result: Dict, tasks: List[Task], reviewer: str, workers: List[str]) -> Path:
    """Write <dir>/.omnicli/team/state.json in Conductor.save_state's schema, phase 'planned'."""
    state_dir = paths.project_state_dir(target)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    if state_path.exists():  # a finished run: keep its record instead of silently overwriting it
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        state_path.replace(state_dir / f"state.before-review-fix-{stamp}.json")
    now = time.time()
    summary = result.get("summary") or "review findings"
    data = {
        "version": 1,
        "goal": f"Fix review findings: {summary[:200]}",
        "project_name": target.name,
        "plan_summary": f"Fix the high/medium findings of the review by {reviewer} "
                        f"(score {result.get('score') if result.get('score') is not None else 'n/a'}/10).",
        "lead": reviewer,
        "workers": list(workers),
        "round": 1,
        "final": {},
        "started_at": now,
        "updated_at": now,
        "phase": "planned",
        "pid": 0,
        "tasks": [asdict(t) for t in tasks],
    }
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(state_path)
    run_registry.register_run({
        "dir": str(target), "goal": data["goal"], "project_name": data["project_name"], "phase": "planned",
        "lead": reviewer, "workers": list(workers), "tasks_total": len(tasks), "tasks_done": 0,
        "tasks_failed": 0, "round": 1, "started_at": now, "pid": 0,
    })
    return state_path


def _show_fix_plan(tasks: List[Task]) -> None:
    rows = [[c_bold(t.id), c_cyan(t.assignee), t.title[:58], ",".join(t.depends_on) or "-",
             (", ".join(t.owns)[:28] or c_dim("(runs alone)")), (t.verify[:34] or c_dim("-"))] for t in tasks]
    print_table(["ID", "Worker", "Task", "After", "Owns", "Verify"], rows, title="Fix plan")


def _confirm() -> bool:
    try:
        ans = input(c_bold(c_yellow("Start the team on this fix plan? [Y/n]: "))).strip().lower()
    except (KeyboardInterrupt, EOFError):
        return False
    return ans in ("", "y", "yes")


def run_fix(target: Path, result: Dict, reviewer: str, workers: List[str], max_tasks: int, yes: bool) -> int:
    tasks = normalize_tasks(result.get("followup_tasks") or [], workers, max_tasks=max_tasks, round_no=1)
    if not tasks:
        print(f"{BADGE_INFO} The reviewer proposed no follow-up tasks, so there is nothing for the team to fix.")
        return 0
    state_path = write_fix_state(target, result, tasks, reviewer, workers)
    print(f"\n{c_bold('Lead:')} {c_magenta(reviewer)}   {c_bold('Workers:')} {', '.join(c_cyan(w) for w in workers)}")
    _show_fix_plan(tasks)
    if not (yes or settings.get("auto_approve")) and not _confirm():
        print(f"{BADGE_INFO} Not started. The fix plan is saved in {state_path}; "
              f"run it later with `omnicli resume {target}`.")
        return 0
    return int(_make_conductor(_fix_config(target, reviewer, workers, max_tasks)).run())


def _fix_config(target: Path, reviewer: str, workers: List[str], max_tasks: int) -> TeamConfig:
    """TeamConfig for resuming the fix plan, with the user's saved defaults (like `omnicli team`)."""
    wanted = dict(
        goal="",
        project_dir=target,
        lead=reviewer,
        workers=list(workers),
        jobs=max(1, int(settings.get("jobs") or 2)),
        max_rounds=max(1, int(settings.get("rounds") or 2)),
        retries=max(0, int(settings.get("retries") or 0)),
        max_tasks=max_tasks,
        task_timeout_min=float(settings.get("task_timeout") or 25),
        lead_timeout_min=float(settings.get("lead_timeout") or 10),
        review=bool(settings.get("review")),
        use_git=bool(settings.get("git")),
        auto_approve=True,
        resume=True,
        models=dict(settings.get("models") or {}),
        effort=dict(settings.get("effort") or {}),
        max_calls=int(settings.get("max_calls") or 0),
        max_minutes=float(settings.get("max_minutes") or 0),
    )
    known = {f.name for f in dataclass_fields(TeamConfig)}  # tolerate older/newer TeamConfig versions
    return TeamConfig(**{k: v for k, v in wanted.items() if k in known})


def _with_saved_options(spec: Dict, name: str) -> Dict:
    """The reviewer's spec with the user's saved model / effort (shared rules: agents.with_saved_options)."""
    from omnicli import agents as _ag
    return _ag.with_saved_options(spec, name)


# --------------------------------------------------------------------------- handler

def handler(args) -> int:
    as_json = bool(getattr(args, "json", False))
    fix = bool(getattr(args, "fix", False))
    say = _Out(as_json)
    target = _resolve_dir(getattr(args, "dir", None))

    if target in (Path.home().resolve(), Path("/").resolve()):
        _err(f"{c_red('Refusing to review:')} {target} is your home/root directory. "
             f"Point -d at a project folder, e.g. `omnicli review -d ./my-app`.")
        return 1
    if not target.is_dir():
        _err(f"{BADGE_FAILED} No such project directory: {target}. Pass an existing folder with -d.")
        return 1
    if as_json and fix:
        _err(f"{BADGE_FAILED} --json and --fix cannot be combined (the team run prints progress). "
             f"Use `omnicli review --json` to inspect, or `omnicli review --fix` to fix.")
        return 1
    if fix:
        phase = _unfinished_run(target)
        if phase:
            what = "an unreadable team state file" if phase == "unreadable" else f"an unfinished team run (phase: {phase})"
            _err(f"{BADGE_FAILED} {target} already has {what}; --fix would replace it. Finish it first with "
                 f"`omnicli resume` (or `omnicli resume {target}`), then run `omnicli review --fix` again.")
            return 1

    max_tasks = max(1, int(settings.pick(getattr(args, "max_tasks", None), "max_tasks") or 10))
    timeout_min = float(settings.pick(getattr(args, "timeout", None), "lead_timeout") or 10)
    if timeout_min <= 0:
        _err(f"{BADGE_FAILED} --timeout must be a positive number of minutes.")
        return 1
    focus = " ".join(getattr(args, "focus", None) or []).strip()
    image_paths = []
    for raw in getattr(args, "image", None) or []:
        image = Path(raw).expanduser().resolve()
        if not image.is_file():
            _err(f"{BADGE_FAILED} Screenshot not found: {image}")
            return 1
        image_paths.append(image)

    # ------------------------------------------------------------ pick the reviewer
    specs = _load_specs()
    order = _agent_order(specs)
    say(f"{BADGE_INFO} Checking which AI CLIs are ready (cached successes are reused)...")
    try:
        health = _check_health(specs, names=order)
    except KeyboardInterrupt:
        agents.kill_all()
        _err(f"\n{BADGE_WARN} Stopped.")
        return 130
    flag_lead = getattr(args, "lead", None)
    saved_lead = None if flag_lead else settings.get("lead")
    reviewer, problem = pick_reviewer(specs, health, order, flag_lead or saved_lead, from_flag=bool(flag_lead))
    if not reviewer:
        _err(f"{BADGE_FAILED} Cannot review: {problem}")
        return 1
    if image_paths and not specs[reviewer].get("sees_images"):
        capable = [name for name in order if health.get(name, {}).get("ok") and specs[name].get("sees_images")]
        if flag_lead:
            _err(f"{BADGE_FAILED} Reviewer '{reviewer}' cannot inspect images; choose an image-capable reviewer.")
            return 1
        if not capable:
            _err(f"{BADGE_FAILED} No ready AI CLI can inspect images. Run `omnicli agents` to see available reviewers.")
            return 1
        reviewer = capable[0]
    if saved_lead and reviewer != saved_lead:
        why = health.get(saved_lead, {}).get("reason") or ("unknown agent" if saved_lead not in specs else "not ready")
        say(f"{BADGE_WARN} Your `lead` setting '{saved_lead}' is not usable ({why}); {reviewer} will review instead.")

    ready = [n for n in order if health.get(n, {}).get("ok")]
    wanted_workers = settings.get("workers") or []
    workers = [w for w in wanted_workers if w in ready] or ready
    say(f"{c_bold('Reviewer:')} {c_magenta(reviewer)}   {c_bold('Ready to fix:')} {', '.join(c_cyan(w) for w in workers)}")

    # ------------------------------------------------------------ review
    installed, missing = tool_report()
    stored_images = []
    if image_paths:
        image_dir = _review_dir(target) / "attachments"
        image_dir.mkdir(parents=True, exist_ok=True)
        for index, image in enumerate(image_paths, 1):
            dest = image_dir / "{}-{}".format(index, image.name)
            shutil.copy2(str(image), str(dest))
            stored_images.append(str(dest))
    prompt = build_review_prompt(target, focus, specs, workers, installed, missing, max_tasks, stored_images)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    say(f"\n{BADGE_INFO} {c_magenta(reviewer)} is reviewing {c_bold(str(target))}"
        f"{(' (focus: ' + focus + ')') if focus else ''} - read-only, up to {timeout_min:g} min...")
    started = time.time()
    try:
        review_spec = _with_saved_options(specs[reviewer], reviewer)
        if stored_images:
            review_spec["_images"] = stored_images
            if review_spec.get("add_dir_args"):
                review_spec["_add_dirs"] = list(dict.fromkeys(
                    list(review_spec.get("_add_dirs") or []) + [str(Path(stored_images[0]).parent)]))
        data, log, error = run_review(review_spec, prompt, target,
                                      timeout_min, stamp, say)
    except KeyboardInterrupt:
        agents.kill_all()
        _err(f"\n{BADGE_WARN} Review stopped.")
        return 130
    if data is None:
        _err(f"{BADGE_FAILED} Review failed: {error}")
        _err(f"  Raw output: {log}  (prompt: {log.with_suffix('.prompt.md')}, errors: {log.with_suffix('.stderr.log')})")
        return 1

    result = {
        "reviewer": reviewer,
        "dir": str(target),
        "focus": focus,
        "reviewed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "duration_sec": round(time.time() - started, 1),
        "log": str(log),
    }
    result.update(normalize_review(data))
    saved = save_review(target, result)

    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        say(c_dim(f"Saved: {saved}"))
        return 0

    print_review(result, verbose=bool(getattr(args, "verbose", False)))
    print(c_dim(f"Saved: {saved}   Raw reply: {log}"))
    if not fix:
        n_fix = sum(1 for f in result["findings"] if f["severity"] in ("high", "medium"))
        if result["followup_tasks"] and n_fix:
            print(f"{BADGE_INFO} To have the team fix the {n_fix} high/medium finding(s): "
                  f"{c_bold(f'omnicli review --fix -d {target}')} (reviews again, then shows the plan first)")
        return 0
    return run_fix(target, result, reviewer, workers, max_tasks, bool(getattr(args, "yes", False)))
