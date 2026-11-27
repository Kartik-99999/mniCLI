"""
Team conductor: give ONE command to a lead AI CLI; the other AI CLIs on the
device work on its orders until the project is complete.

    plan (lead) -> you approve -> workers build tasks in dependency order
    (in parallel when their files don't overlap) -> verify command ->
    lead reviews each result -> revise or approve -> git checkpoint ->
    lead's final integration check -> follow-up round if needed -> report

All state lives in <project>/.omnicli/team/ so an interrupted run can be resumed.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Optional, Tuple

import os

from omnicli import agents as ag
from omnicli import ledger, workflows
from omnicli import runs as run_registry
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

IGNORE_DIRS = {
    ".git", ".omnicli", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".next", "dist", "build", "target", ".idea", ".vscode",
}
# Kept out of checkpoints via .git/info/exclude (local only; the project's .gitignore is untouched).
GIT_EXCLUDES = [".omnicli/", "node_modules/", ".venv/", "venv/", "__pycache__/", "*.pyc",
                ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", ".next/", ".DS_Store",
                # never let a checkpoint commit secrets that happen to sit in the project folder
                ".env", ".env.*", "!.env.example", "!.env.sample", "!.env.template",
                "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore", "*.jks", "id_rsa*", "id_ecdsa*", "id_ed25519*",
                ".ssh/", ".aws/", ".npmrc", ".pypirc", ".netrc", "credentials.json", "service-account*.json",
                "*.tfstate", "*.tfstate.*"]
PROBE_TOOLS = ["python3", "pip3", "pytest", "node", "npm", "npx", "pnpm", "yarn", "bun", "deno",
               "go", "cargo", "java", "mvn", "gradle", "make", "docker", "ruby", "php", "swift",
               "chromium", "google-chrome", "firefox"]
SUMMARY_MARKER = "=== SUMMARY ==="
MAX_DIFF_CHARS = 14000


# --------------------------------------------------------------------------- data

@dataclass
class Task:
    id: str
    title: str
    assignee: str
    instructions: str
    depends_on: List[str] = field(default_factory=list)
    owns: List[str] = field(default_factory=list)
    acceptance: str = ""
    verify: str = ""
    verify_note: str = ""   # why a planned verify command was refused (shown to the user)
    category: str = "other"
    round: int = 1
    status: str = "pending"  # pending | running | done | failed | skipped
    agent_used: str = ""
    summary: str = ""
    review_note: str = ""
    attempts: List[Dict] = field(default_factory=list)
    commit: str = ""
    duration_sec: float = 0.0
    workspace: str = ""
    outcome_recorded: bool = False
    # Behaviors observed before or during this task that each subsequent revision must retain.
    passing_workflow_checks: Optional[List[str]] = None

    @classmethod
    def from_dict(cls, d: Dict) -> "Task":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class TeamConfig:
    goal: str
    project_dir: Path
    lead: Optional[str] = None
    workers: Optional[List[str]] = None
    jobs: int = 2
    max_rounds: int = 2
    retries: int = 1
    task_timeout_min: float = 25
    lead_timeout_min: float = 10
    max_tasks: int = 10
    review: bool = True
    use_git: bool = True
    auto_approve: bool = False
    approve_tasks: bool = False
    notify: bool = False
    use_worktrees: bool = False
    cross_vendor_review: bool = False
    red_team: bool = False
    human_worker: bool = False
    plan_only: bool = False
    resume: bool = False
    confirm_saved_plan: bool = False
    recheck: bool = False
    # Worker models / reasoning effort, e.g. {"claude": "sonnet"}; ignored (with a warning)
    # for agents whose adapter has no verified flag for it
    models: Dict[str, str] = field(default_factory=dict)
    # Optional per-agent model used for lead planning/review; absent entries inherit `models`.
    # The value "default" explicitly asks the CLI to use its own default model.
    lead_models: Dict[str, str] = field(default_factory=dict)
    effort: Dict[str, str] = field(default_factory=dict)
    # A lead can use a different model from its worker role, so its tier is separate too.
    lead_effort: Dict[str, str] = field(default_factory=dict)
    # budgets for this session: AI calls (lead + workers) and wall-clock minutes; 0 = unlimited
    max_calls: int = 0
    max_minutes: float = 0.0
    # print each agent's raw output lines as they arrive (-q turns this off)
    stream_output: bool = True
    # extra absolute directories workers may read/write (adapters with add_dir_args only)
    add_dirs: List[str] = field(default_factory=list)
    # overrides the project name the lead picks (shown in runs/status/report)
    project_name: Optional[str] = None
    images: List[str] = field(default_factory=list)  # reference images (absolute paths) for lead + workers


# --------------------------------------------------------------------------- pure helpers

def _norm_path(p: str) -> Optional[str]:
    p = str(p).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    if p.startswith("/") or p.startswith("~") or ".." in PurePosixPath(p).parts:
        return None  # never let a plan claim paths outside the project
    # a glob only claims its fixed prefix ("src/*.py" -> "src")
    parts = []
    for part in PurePosixPath(p).parts if p not in ("", ".") else ():
        if any(ch in part for ch in "*?["):
            break
        parts.append(part)
    return "/".join(parts)


def paths_overlap(a: List[str], b: List[str]) -> bool:
    """True if any path in a is equal to / inside / a parent of any path in b. '' means 'everything'."""
    for x in a:
        px = PurePosixPath(x).parts if x else ()
        for y in b:
            py = PurePosixPath(y).parts if y else ()
            n = min(len(px), len(py))
            if px[:n] == py[:n]:
                return True
    return False


def protected_scope_conflicts(tasks: List[Task], protected_paths: List[str]) -> List[str]:
    """Find planned tasks that could overwrite files already being edited outside this run."""
    if not protected_paths:
        return []
    issues = []
    for task in tasks:
        if not task.owns:
            issues.append("{} has whole-project scope (owns is empty) while other work is active".format(task.id))
            continue
        conflicts = [path for path in protected_paths if paths_overlap(task.owns, [path])]
        if conflicts:
            issues.append("{} owns protected path(s): {}".format(task.id, ", ".join(conflicts[:8])))
    return issues


def classify_task(text: str) -> str:
    """Infer a coarse skill category from task text and owned paths."""
    value = str(text or "").lower()
    checks = (
        ("tests", ("test", "pytest", "unittest", "spec")),
        ("docs", ("readme", "documentation", "docs", "tutorial", "changelog")),
        ("design", ("design", "mockup", "screenshot", "visual", "ui polish")),
        ("frontend", ("frontend", "front-end", "react", "vue", "css", "html", "component", "page", "ui")),
        ("backend", ("backend", "back-end", "api", "endpoint", "database", "sql", "auth", "server")),
        ("devops", ("deploy", "docker", "ci/cd", "pipeline", "infrastructure", "workflow", "release")),
    )
    for category, words in checks:
        if any(word in value for word in words):
            return category
    return "other"


def file_in_scope(path: str, owns: List[str]) -> bool:
    return paths_overlap([path], owns)


# Build/test by-products that are not source changes (relevant when a project has no .gitignore).
_GENERATED_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".tox"}
_GENERATED_FILES = {".DS_Store", ".coverage"}


def is_generated_path(path: str) -> bool:
    parts = path.split("/")
    return (any(part in _GENERATED_DIRS or part.endswith(".egg-info") for part in parts[:-1])
            or parts[-1] in _GENERATED_FILES or parts[-1].endswith((".pyc", ".pyo")))


def normalize_tasks(
    raw_tasks: List, workers: List[str], existing_ids: Optional[List[str]] = None,
    prefix: str = "t", max_tasks: int = 10, round_no: int = 1,
) -> List[Task]:
    """Turn the lead's JSON into valid Tasks: unique ids, real workers, known deps, no cycles."""
    existing = set(existing_ids or [])
    tasks: List[Task] = []
    seen = set(existing)
    rr = 0
    for i, raw in enumerate([r for r in raw_tasks if isinstance(r, dict)][:max_tasks], start=1):
        # ids come from the AI lead and end up in log file names and commit messages:
        # keep them to safe characters so an id like "../x" can't escape the logs folder
        tid = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(raw.get("id") or "").strip()).strip(".-")[:40] or f"{prefix}{i}"
        base, n = tid, 2
        while tid in seen:
            tid, n = f"{base}-{n}", n + 1
        seen.add(tid)
        assignee = str(raw.get("assignee") or raw.get("worker") or "").strip().lower()
        if assignee in ("me", "human") and "human" in workers:
            assignee = "human"
        if assignee not in workers:
            assignee = workers[rr % len(workers)]
            rr += 1
        owns_raw = raw.get("owns") or raw.get("files") or []
        if isinstance(owns_raw, str):
            owns_raw = [owns_raw]
        owns = []
        for p in owns_raw:
            np_ = _norm_path(p)
            if np_ is not None and np_ not in owns:
                owns.append(np_)
        deps = raw.get("depends_on") or raw.get("after") or []
        if isinstance(deps, str):
            deps = [deps]
        title = str(raw.get("title") or raw.get("name") or tid).strip()
        tasks.append(Task(
            id=tid,
            title=title,
            assignee=assignee,
            instructions=str(raw.get("instructions") or raw.get("description") or title).strip(),
            depends_on=[str(d) for d in deps],
            owns=owns,
            acceptance=str(raw.get("acceptance") or raw.get("acceptance_criteria") or "").strip(),
            verify=str(raw.get("verify") or "").strip(),
            verify_note="",
            category=(str(raw.get("category") or "").strip().lower()
                      if str(raw.get("category") or "").strip().lower() in ledger.SKILL_CATEGORIES
                      else classify_task(" ".join((title, str(raw.get("instructions") or raw.get("description") or ""),
                                                   " ".join(owns))))),
            round=round_no,
        ))

    ids = {t.id for t in tasks} | existing
    for t in tasks:
        t.depends_on = [d for d in dict.fromkeys(t.depends_on) if d in ids and d != t.id]

    # break dependency cycles inside this batch
    batch = {t.id: t for t in tasks}
    resolved = set(existing)
    remaining = list(tasks)
    while remaining:
        ready = [t for t in remaining if all(d in resolved or d not in batch for d in t.depends_on)]
        if not ready:
            victim = remaining[0]
            victim.depends_on = [d for d in victim.depends_on if d in resolved or d not in batch]
            continue
        for t in ready:
            resolved.add(t.id)
            remaining.remove(t)

    # verify commands run on the user's machine: drop any that aren't plain test invocations
    from omnicli import settings as _settings
    from omnicli.safecmd import verify_allowed
    policy = _settings.get("verify_policy")
    for t in tasks:
        if t.verify:
            ok, why = verify_allowed(t.verify, policy)
            if not ok:
                t.verify_note = f"`{t.verify}` {why}"
                t.verify = ""
    return tasks


def extract_summary(output: str, limit: int = 3000) -> str:
    text = output or ""
    if SUMMARY_MARKER in text:
        text = text.rsplit(SUMMARY_MARKER, 1)[1]
    else:
        text = "\n".join(text.strip().splitlines()[-40:])
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def list_tree(root: Path, max_entries: int = 150, max_depth: int = 4) -> str:
    lines: List[str] = []

    def walk(d: Path, depth: int):
        if len(lines) >= max_entries or depth > max_depth:
            return
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except Exception:
            return
        for p in entries:
            if len(lines) >= max_entries:
                lines.append("  ... (truncated)")
                return
            if p.name in IGNORE_DIRS or p.name == ".DS_Store":
                continue
            rel = p.relative_to(root).as_posix()
            lines.append(rel + ("/" if p.is_dir() else ""))
            if p.is_dir():
                walk(p, depth + 1)

    walk(root, 1)
    return "\n".join(lines) if lines else "(empty directory)"


def uncommitted_paths(root: Path, limit: Optional[int] = None) -> List[str]:
    """List user/other-agent changes so the lead can keep its plan out of their files."""
    try:
        result = subprocess.run(["git", "status", "--porcelain", "-z", "--untracked-files=all"],
                                cwd=str(root), capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    if result.returncode:
        return []
    records = result.stdout.split("\0")
    found = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            continue
        path = record[3:].replace("\\", "/")
        if path and not (path == ".omnicli" or path.startswith(".omnicli/")):
            found.append(path)
        if "R" in record[:2] or "C" in record[:2]:
            if index < len(records) and records[index]:
                found.append(records[index].replace("\\", "/"))
                index += 1
    paths = list(dict.fromkeys(found))
    return paths if limit is None else paths[:max(0, int(limit))]


PY_MODULES = ["pytest"]


def tool_report() -> Tuple[List[str], List[str]]:
    """(installed, missing) dev tools, including python modules people often assume exist (pytest)."""
    have = [t for t in PROBE_TOOLS if shutil.which(t)]
    missing = [t for t in PROBE_TOOLS if t not in have]
    py = shutil.which("python3")
    for mod in PY_MODULES:
        ok = bool(py) and subprocess.run([py, "-c", f"import {mod}"], capture_output=True).returncode == 0
        (have if ok else missing).append(f"python3 -m {mod}")
    return have, missing


def available_tools() -> List[str]:
    return tool_report()[0]


def missing_dependency(code: int, out: str, project_dir: Path) -> str:
    """Explain a verify failure caused by the machine (tool/module not installed) rather than by the code."""
    if code == 0 or not out:
        return ""
    if "sandbox-exec: sandbox_apply: Operation not permitted" in out:
        return "macOS sandbox could not launch the verification command"
    m = re.search(r"No module named '?([A-Za-z0-9_.]+)'?", out)
    if m:
        root = m.group(1).split(".")[0]
        if not any((project_dir / c).exists() for c in (root, root + ".py", "src/" + root)):
            return f"python module '{root}' is not installed on this machine"
    m = re.search(r"(?:^|\s)([\w.\-/]+): (?:command )?not found", out)
    if code == 127 and m:
        cmd = m.group(1)
        if not cmd.startswith((".", "/")) and not (project_dir / cmd).exists():
            return f"command '{cmd}' is not installed on this machine"
    return ""


def verify_passed(verify: Optional[Tuple[int, str, str]]) -> bool:
    """None (no check), exit 0, or could-not-run-here all count as 'not failing'."""
    return verify is None or verify[0] == 0 or bool(verify[2])


def parse_agent_values(items: Optional[List[str]], flag: str = "--model", what: str = "VALUE") -> Dict[str, str]:
    """['claude=opus', 'codex=o3'] -> {'claude': 'opus', 'codex': 'o3'}; ValueError with a friendly message."""
    out: Dict[str, str] = {}
    for raw in items or []:
        agent, sep, value = str(raw).partition("=")
        agent, value = agent.strip(), value.strip()
        if not sep or not agent or not value or any(ch.isspace() for ch in agent):
            raise ValueError(f"{flag} expects AGENT={what} (e.g. {flag} claude={'opus' if what == 'MODEL' else 'high'}); "
                             f"got '{raw}'")
        out[agent] = value
    return out


def resolve_add_dirs(dirs: Optional[List[str]]) -> List[str]:
    """Absolute, existing, de-duplicated directories; ValueError for missing ones or home / '/' (or above)."""
    home = Path.home().resolve()
    out: List[str] = []
    for raw in dirs or []:
        p = Path(str(raw)).expanduser().resolve()
        if p == Path(p.anchor) or p == home or p in home.parents:
            raise ValueError(f"--add-dir {raw}: refusing to give agents your home or root directory ({p}). "
                             "Point it at a specific folder.")
        if not p.is_dir():
            raise ValueError(f"--add-dir {raw}: {p} is not an existing directory")
        if str(p) not in out:
            out.append(str(p))
    return out


# --------------------------------------------------------------------------- prompts

def _workers_block(specs: Dict[str, Dict], workers: List[str]) -> str:
    rows = []
    for w in workers:
        spec = specs[w]
        row = f"- {w}: {spec.get('name', w)} - strengths: {spec.get('strengths', 'general coding')}"
        row += f"; worker model: {spec.get('_model') or 'CLI default'}"
        row += f"; worker effort: {spec.get('_effort') or 'CLI default'}"
        if spec.get("_lead_model"):
            row += "; lead model: " + ("CLI default" if spec["_lead_model"] == "default" else spec["_lead_model"])
        rows.append(row)
    return "\n".join(rows)


TASK_JSON_SHAPE = (
    '{"id": "t1", "title": "short title", "assignee": "<worker id>", '
    '"instructions": "detailed, self-contained instructions", "depends_on": [], '
    '"owns": ["relative/path/or/dir"], "category": "frontend|backend|tests|docs|devops|design|other", '
    '"acceptance": "how to tell it is done", "verify": ""}'
)


def build_plan_prompt(goal: str, project_dir: Path, specs: Dict[str, Dict], workers: List[str],
                      tools: List[str], max_tasks: int, missing: Optional[List[str]] = None,
                      skill_recommendations: Optional[Dict[str, Dict]] = None,
                      protected_paths: Optional[List[str]] = None) -> str:
    skill_rows = []
    for category, row in sorted((skill_recommendations or {}).items()):
        first = int(row.get("first_try", 0))
        count = int(row.get("tasks", 0))
        minutes = float(row.get("avg_seconds", 0)) / 60
        skill_rows.append("- {}: {} {}/{} approved first try; average {:.1f} revisions, {:.1f} min".format(
            category, row.get("agent", ""), first, count, float(row.get("avg_revisions", 0)), minutes))
    performance = "\n".join(skill_rows) or "(not enough completed task history yet)"
    protected = list(protected_paths or [])
    protected_lines = ["- " + path for path in protected[:120]]
    if len(protected) > 120:
        protected_lines.append("- ... and {} more paths (all remain protected)".format(len(protected) - 120))
    return f"""[OmniCLI team - planning]
You are the LEAD ENGINEER of a team of AI coding agents. Each agent is a separate command-line tool on this computer.
Right now your only job is to break the project goal into tasks and assign each task to a worker.
Do NOT write code and do NOT modify any files in this step.

PROJECT GOAL:
{goal}

PROJECT DIRECTORY: {project_dir}
Current contents:
{list_tree(project_dir)}

CHANGED FILES OWNED BY THE USER OR OTHER ACTIVE AGENTS:
{chr(10).join(protected_lines) or '(none detected)'}

WORKERS (assign every task to one of these ids):
{_workers_block(specs, workers)}

Tools installed on this machine: {", ".join(tools) or "unknown"}
NOT installed: {", ".join(missing or []) or "(nothing notable)"}
Workers run sandboxed and usually cannot install packages, so verify commands must only use installed tools.

{workflows.editor_reference(goal)}

LOCAL SKILL HISTORY (aggregate outcomes only; use as a routing hint, not a guarantee):
{performance}

RULES:
- Research the existing product through its entry points, tests, and relevant docs. In the summary give workers
  the paths, interface decisions, and constraints they will need. For unfamiliar or version-sensitive APIs,
  use primary docs if accessible and identify assumptions that need a local experiment.
- Record the main user workflows before splitting the implementation. For each, describe the user's action,
  the resulting state, and what must still work after the next action or reload. Assign a behavior test to a
  task and reference its id in check_task. For independently shipped features, use focused checks so a later
  change can be tested against each finished feature. Workflows may share an integration suite. For inspectable
  deliverables such as documentation, set kind to inspection and describe what the reviewer must inspect.
  For interactive user journeys set kind to interaction. If execution is unavailable, leave check_task empty
  and report that journey as unverified.
  For interactive apps, inspect the project's test dependencies and available browser tooling. Plan an
  interaction through the public UI and assert the changed state. Vary the test data or operation order.
  A file-existence check, build success, or screenshot alone cannot demonstrate an interaction.
- Read the relevant entry points and existing tests before naming interfaces or proposing new files.
  Use the fewest tasks needed, at most {max_tasks}. A focused change can be one task including its checks.
- Translate the user's requested outcome into concrete, user-visible behaviors. Give every task specific acceptance
  criteria that describe what a person can do and observe, not just which files should exist. For broad product goals,
  cover the main workflows from start to finish and reserve time for integration and usability fixes.
- For user-facing applications, treat visual quality as part of completion: specify a coherent layout, hierarchy,
  typography, spacing, responsive behavior, accessible controls, and intentional empty/error states. Do not accept a
  bare canvas or a row of inert controls as a finished product.
- Do not silently shrink the goal into a toy demo. Avoid placeholder screens, controls with no working action, and
  claims that a feature is complete when only its simplest slice exists. If the full request cannot fit the task or
  round limits, state the reduced scope and leave the missing requirements for final follow-up work.
- Instructions must be self-contained: a worker sees only the goal, its own task and one-line summaries of finished tasks.
  Name concrete files, functions, endpoints, data shapes and conventions so separate workers' pieces fit together.
- Match tasks to worker strengths and spread the work across workers.
- Set category to one of frontend, backend, tests, docs, devops, design, other. Prefer a worker with strong history
  in that category when the local history has at least 3 tasks; use the listed evidence to explain close choices.
- depends_on: ids of tasks that must finish first.
- owns: the files/directories the task will create or modify (relative paths). Tasks that could run at the same time
  must not share paths (give independent tasks explicit, disjoint `owns` paths so they run in parallel). Use [] only if the task touches many places - it will then run alone.
- Treat the changed files listed above as protected work from the user or another active agent. Do not include
  those paths or their parent directories in task ownership, and do not edit them. If the goal depends on one,
  call out the coordination need and leave that work unassigned until it is no longer protected.
- While any protected paths are listed, every task must have explicit, narrow `owns` paths. A whole-project
  `owns: []` task is rejected because it cannot be kept away from another active agent's files.
- verify: optional command that exits 0 only when the task is done correctly, using installed tools
  (e.g. "python3 -m unittest -v test_app" or "npm test"). Use "" when no automated check makes sense.
  It runs on the user's machine, so it MUST be a plain test invocation: a test/build tool or read-only
  check (test, grep, diff), joined only with && or ||, piping only into grep/head/tail/wc. No inline code
  (no python -c / node -e - put checks in a test file), no $ or $(...), no ;, no {...} groups, no
  redirects except >/dev/null, no paths outside the project, no glob patterns like *.py (commands run
  without a shell, so nothing is expanded), no installs, servers, network or deletes.
  Commands that break these rules are dropped and the task gets no automated check.
- If the goal asks for a tool that is NOT installed (e.g. pytest), still deliver what was asked, but make it runnable
  with what IS installed - e.g. write tests as unittest.TestCase classes (pytest runs those too) and verify with
  `python3 -m unittest` - and mention the missing tool in the README's setup section.
- Include the checks that demonstrate the change. Document new setup or usage where users already look for it.

Reply with ONLY a JSON object (no prose) of this shape:
{{"project_name": "kebab-case-name", "summary": "one paragraph describing the approach and architecture",
  "workflows": [{{"scenario": "action and observable result", "kind": "interaction", "check_task": "t1"}}],
  "tasks": [{TASK_JSON_SHAPE}]}}
"""


def build_worker_prompt(task: Task, goal: str, project_dir: Path, agent_name: str, lead: str,
                        all_tasks: List[Task], feedback: str = "", revision: int = 0,
                        previous_agents: Optional[List[str]] = None,
                        extra_dirs: Optional[List[str]] = None,
                        team_memory: Optional[List[Dict]] = None,
                        model: str = "", effort: str = "",
                        protected_paths: Optional[List[str]] = None, plan_summary: str = "",
                        user_workflows: Optional[List[Dict]] = None) -> str:
    board = []
    for t in all_tasks:
        if t.id == task.id:
            board.append(f"- {t.id} [YOU] {t.title}")
        elif t.status == "done":
            board.append(f"- {t.id} [done by {t.agent_used or t.assignee}] {t.title} - {first_line(t.summary)}")
        else:
            board.append(f"- {t.id} [{t.status}, {t.assignee}] {t.title}")
    owns = ", ".join(task.owns) if task.owns else "(whole project - no other OmniCLI task runs alongside you)"
    parts = [
        f"[OmniCLI team - task assignment]",
        f"You are {agent_name}, a member of an AI engineering team coordinated by OmniCLI. "
        f"The lead engineer ({lead}) assigned you a task. Do the work now by creating and editing real files.",
        "",
        "OVERALL PROJECT GOAL:",
        goal,
        "",
        "TEAM BOARD (context only - do just YOUR task):",
        "\n".join(board),
        "",
        f"YOUR TASK ({task.id}): {task.title}",
        task.instructions,
        "",
        f"Acceptance criteria: {task.acceptance or 'the instructions above are fully implemented and working'}",
        f"Files/dirs you own: {owns}",
    ]
    reference = workflows.editor_reference(goal)
    if reference:
        parts += ["", reference]
    if plan_summary:
        parts += ["", "SHARED IMPLEMENTATION DECISIONS:", plan_summary]
    if user_workflows:
        parts += ["", "USER WORKFLOWS (implement your assigned part):", workflows.describe(user_workflows),
                  "If your task owns a workflow check, exercise it through the public entry point. Assert the "
                  "observable result after the action. A smoke test that only opens a page cannot demonstrate editing."]
    if protected_paths:
        parts += ["", "PROTECTED FILES already being edited by the user or another active agent:",
                  "Do not modify these paths or any parent directory that contains them:",
                  *["- " + path for path in protected_paths]]
    if model or effort:
        parts.append(f"Runtime selection: model {model or 'CLI default'}; reasoning effort {effort or 'CLI default'}.")
    if task.owns:
        parts.append("Other agents may be editing other paths at the same time - avoid editing files outside yours unless strictly necessary.")
    if task.verify:
        parts.append(f"After you finish, the lead will run this check, which must pass: {task.verify}")
        parts.append("Run this check yourself before finishing so any errors are fixed before review.")
    if previous_agents:
        parts += ["", f"NOTE: {', '.join(previous_agents)} attempted this task before you and did not finish. "
                      "Inspect the project, keep whatever partial work is good, and complete the task."]
    if feedback:
        parts += ["", f"REVISION #{revision}: the lead reviewed the previous attempt and requested these changes:", feedback]
    lessons = [str(row.get("lesson", "")).strip() for row in (team_memory or []) if row.get("lesson")]
    if lessons:
        parts += ["", "TEAM MEMORY (short lessons from previous runs; apply when relevant):"]
        parts.extend("- " + lesson for lesson in lessons[:5])
    parts += [
        "",
        "RULES:",
        f"- Work only inside {project_dir}." if not extra_dirs else
        f"- Work only inside {project_dir} and these extra directories you were given access to: {', '.join(extra_dirs)}.",
        "- Trace the existing entry point and its callers before editing. Exercise the changed behavior and report "
        "which checks ran, their results, and any checks you could not run.",
        "- Implement the requested behavior, not a visual placeholder or a control that only looks interactive. "
        "If any acceptance criterion cannot be completed, say exactly what remains unfinished.",
        "- For user-facing UI, deliver a cohesive, finished screen: intentional hierarchy and spacing, responsive "
        "layout, readable type, accessible focus/labels, meaningful empty states, and working controls. A blank "
        "canvas is complete only when that is the intended state and the next action is clear.",
        "- Do not run git commit/push - OmniCLI checkpoints the work itself. Do not start servers that keep running.",
        "- Never install anything outside the project: no `pip install` into the system or user Python (no --user, no sudo),"
        " no `npm -g`, no brew. Project-local installs are fine (a .venv inside the project, `npm install` in the project)."
        " If a tool you need is missing, say so in your summary instead of installing it globally.",
        f"- When finished, end your reply with a line '{SUMMARY_MARKER}' followed by 3-8 short bullets: what you built or "
        "changed, files touched, how to run/test it, and anything left unfinished.",
    ]
    return "\n".join(parts)


def build_review_prompt(task: Task, goal: str, agent: str, summary: str, changes: str,
                        verify: Optional[Tuple[int, str]], revision: int, max_revisions: int) -> str:
    if verify is None:
        vblock = "No automated check for this task."
    else:
        code, out, missing = verify
        if missing:
            vblock = (f"`{task.verify}` could not run on this machine: {missing}. This is an environment limitation, not "
                      f"the worker's fault - judge the work by reading the code, and do not ask the worker to install packages.")
        else:
            vblock = f"`{task.verify}` exited with code {code} ({'PASS' if code == 0 else 'FAIL'}). Output tail:\n{out or '(no output)'}"
    last_chance = revision >= max_revisions
    return f"""[OmniCLI team - review]
You are the LEAD ENGINEER. A team member just finished a task you assigned. Review it. Do not modify any files
(you may read files in the project directory to check the work).

PROJECT GOAL:
{goal}

TASK {task.id}: {task.title}  (done by {agent}, attempt {revision + 1})
Instructions given:
{task.instructions}
Acceptance criteria: {task.acceptance or "(see instructions)"}

WORKER'S REPORT:
{summary or "(the worker gave no summary)"}

CHANGES SINCE THE LAST CHECKPOINT:
{changes or "(no file changes detected)"}

VERIFICATION (run by OmniCLI - you are read-only and cannot run commands yourself):
{vblock}

Approve when the task meets its acceptance criteria well enough for the project to move on. Ask for a revision
only for concrete, fixable problems, and say exactly what to change.{" This is the last allowed revision round. Do not approve if acceptance criteria are still missing; OmniCLI will report the task as incomplete." if last_chance else ""}

QUALITY REVIEW:
- Inspect the changed implementation; a worker summary and passing tests are not enough by themselves.
- Check that each claimed feature has a real working path through the product. For user interfaces, look for
  placeholder-only screens, inert controls, missing save/load or error states, and obvious gaps in the requested
  workflow. For libraries and commands, check that the public behavior matches the request and handles ordinary
  edge cases.
- For UI tasks, judge the screen as a whole: visual hierarchy, spacing, typography, responsive layout, accessible
  controls, and meaningful empty states. If a browser or screenshot is available, inspect it; otherwise assess the
  implementation and don't claim visual verification that did not happen.
- Do not approve a feature just because its component, file, or button exists. If a concrete acceptance criterion
  is missing, request a focused revision and name the user-visible behavior that is absent.

Reply with ONLY a JSON object:
{{"verdict": "approve" or "revise", "feedback": "precise instructions for the worker if revise, else empty", "summary": "one line describing what was delivered"}}
"""


def format_checks(checks: List[Tuple[str, Tuple[int, str, str]]]) -> str:
    if not checks:
        return "(no automated checks were defined)"
    lines = []
    for cmd, (code, out, missing) in checks:
        if missing:
            lines.append(f"- `{cmd}` -> COULD NOT RUN ({missing})")
        else:
            tail = " | ".join(l.strip() for l in out.strip().splitlines()[-3:] if l.strip())[:300]
            lines.append(f"- `{cmd}` -> {'PASS' if code == 0 else f'FAIL (exit {code})'}" + (f": {tail}" if tail and code else ""))
    return "\n".join(lines)


def enforce_completion_gate(data: Dict, tasks: List[Task],
                            checks: List[Tuple[str, Tuple[int, str, str]]]) -> Dict:
    """Do not report a complete project when a task or runnable required check failed."""
    result = dict(data or {})
    issues = []
    unfinished = [f"{t.id} ({t.status})" for t in tasks if t.status != "done"]
    if unfinished:
        issues.append("Tasks did not finish successfully: " + ", ".join(unfinished))
    failed = []
    for command, (code, _output, missing) in checks:
        if code != 0 and not missing and command not in failed:
            failed.append(command)
    if failed:
        issues.append("Required verification failed: " + ", ".join(f"`{cmd}`" for cmd in failed))
    if issues:
        result["complete"] = False
        result["remaining_issues"] = list(result.get("remaining_issues") or [])
        for issue in issues:
            if issue not in result["remaining_issues"]:
                result["remaining_issues"].append(issue)
        if not result.get("summary"):
            result["summary"] = "The project still has unfinished work."
    return result


def build_final_prompt(goal: str, project_dir: Path, tasks: List[Task], workers: List[str],
                       specs: Dict[str, Dict], round_no: int, can_add: bool, max_tasks: int,
                       checks: Optional[List[Tuple[str, Tuple[int, str, str]]]] = None,
                      user_workflows: Optional[List[Dict]] = None) -> str:
    rows = []
    for t in tasks:
        note = f" | lead note: {t.review_note}" if t.review_note else ""
        check = f" | check: {t.verify}" if t.verify else ""
        rows.append(f"- {t.id} [{t.status}] ({t.agent_used or t.assignee}) {t.title}: {first_line(t.summary)}{note}{check}")
    followup = (
        f'If something essential is missing or broken, list up to {max_tasks} follow-up tasks in "followup_tasks" using the same '
        f"task shape (workers: {', '.join(workers)}):\n{TASK_JSON_SHAPE}\n"
        if can_add else 'No more work rounds are available, so "followup_tasks" must be [].\n'
    )
    workflow_block = ""
    if user_workflows:
        workflow_block = ("\nAGREED USER WORKFLOWS:\n" + workflows.describe(user_workflows) +
                          "\nFor each W id return a workflow_results entry with status met, unmet, or unverified "
                          "and evidence. Inspect the relevant implementation and what its tests actually assert. "
                          "Cite the observed state change or a specific file and symbol for inspected work. "
                          "Mark an interactive workflow unverified if only appearance was checked. For an unmet "
                          "workflow, identify the first broken step and propose a focused repair. If a workflow "
                          "had no original check, supply check_task with the id of a finished task whose executed "
                          "check exercises it.\n")
    return f"""[OmniCLI team - final check]
You are the LEAD ENGINEER. Round {round_no} of the team's work has finished. Inspect the project directory
(read files; do not modify anything) and decide whether the PROJECT GOAL is complete.

PROJECT GOAL:
{goal}

PROJECT DIRECTORY: {project_dir}
{list_tree(project_dir)}

TASK RESULTS:
{chr(10).join(rows)}

AUTOMATED CHECKS - OmniCLI just re-ran every task's verify command on the current project state.
You are read-only and cannot run commands yourself; treat these results as having been executed:
{format_checks(checks or [])}
{workflow_block}
{workflows.editor_reference(goal)}

COMPLETION BAR:
- Compare the delivered product directly against every capability and workflow in the user's original goal.
  Task statuses, summaries, commits, and passing tests are evidence, but none proves the goal is complete by itself.
- Judge the actual user experience and integration. A screen that renders but has inert or placeholder controls,
  a spreadsheet that only looks like a grid, or a presentation that only shows a title is not a complete editor.
  Name specific missing or shallow behaviors as remaining issues and create follow-up tasks for them when possible.
- Report complete=true only when the requested core workflows work end to end and no required feature is still a
  stub or materially below the requested scope. If the goal was intentionally reduced to an MVP, disclose that
  plainly and report complete=false unless the user asked for an MVP.
- List release-blocking gaps in remaining_issues. Put optional improvements in the summary.
- A failed required verification command or any task that did not finish successfully means the project is not
  complete, even if other checks pass.

WORKERS:
{_workers_block(specs, workers)}

{followup}
Reply with ONLY a JSON object:
{{"complete": true or false, "summary": "what the project now is and does", "how_to_run": "exact commands to install/run/test it",
  "workflow_results": {{"W1": {{"status": "met", "evidence": "specific observation"}}}},
  "remaining_issues": ["..."], "followup_tasks": []}}
"""


def build_red_team_prompt(task: "Task", workspace: Path) -> str:
    """Prompt for the independent red-team agent. Its tests must run under the task's existing,
    user-approved verify command - OmniCLI never executes a test command an AI makes up."""
    if task.verify:
        how = (f"OmniCLI will run exactly this command next, and your tests MUST be executed by it:\n"
               f"    {task.verify}\n"
               "Put them where that command finds them (for example, add test cases to a test file it already "
               "runs). Tests that this command does not run are worthless here.")
    else:
        how = ("This task has no automated check command, so put your tests where the project's normal test "
               "command discovers them.")
    return f"""[OmniCLI red-team test assignment]
You are an independent adversarial test engineer. Another agent implemented this task:
{task.title}

Inspect the current project and add focused automated tests that try to expose bugs, edge cases,
security mistakes, and regressions in this implementation. Do not edit or fix implementation files.
Only add or change test files. Do not delete existing tests. Use the project's existing test framework
and conventions. Do not install packages or run destructive commands. Work only inside {workspace}.

{how}

Acceptance criteria: add meaningful tests that fail for plausible defects and pass for correct behavior.
"""


def first_line(text: str, limit: int = 140) -> str:
    for line in (text or "").splitlines():
        line = line.strip(" -*\t")
        if line:
            return line[:limit]
    return ""


# --------------------------------------------------------------------------- conductor

class Conductor:
    def __init__(self, cfg: TeamConfig, specs: Optional[Dict[str, Dict]] = None,
                 health: Optional[Dict[str, Dict]] = None, input_fn: Callable[[str], str] = input,
                 verbose: bool = True):
        self.cfg = cfg
        self.specs = specs if specs is not None else ag.load_agent_specs()
        self._health_override = health
        self.input_fn = input_fn
        self.verbose = verbose
        self.dir = Path(cfg.project_dir).expanduser().resolve()
        self.state_dir = self.dir / ".omnicli" / "team"
        self.logs = self.state_dir / "logs"
        self.state_path = self.state_dir / "state.json"
        self.report_path = self.state_dir / "REPORT.md"
        self.lock = threading.RLock()
        self.git_lock = threading.RLock()
        self.stopping = False

        self.goal = cfg.goal
        self.tasks: List[Task] = []
        self.plan_summary = ""
        self.workflows: List[Dict] = []
        self.project_name = ""
        self.protected_paths: List[str] = []
        self.lead: str = ""
        self.workers: List[str] = []
        self.healthy: List[str] = []
        self.down: Dict[str, str] = {}
        self.quota_down = set()
        self.quota_resets: Dict[str, Dict] = ledger.active_quota_resets()
        self.round = 1
        self.final: Dict = {}
        self.started_at = time.time()
        self.git = False
        self._git_ident: List[str] = []
        self.running: Dict[str, Dict] = {}
        self._lead_calls = 0
        # planning | planned | running | stopped | complete | incomplete | failed
        self.phase = "planning"

        if cfg.project_name:
            self.project_name = str(cfg.project_name).strip()
        # budgets: every lead + worker call this session counts; time runs from run()
        self.calls = 0
        self._run_started = time.time()
        self.budget_reason = ""  # set once a call/task is refused because a budget is used up
        self._report_written = False
        self.add_dirs: List[str] = [str(Path(d).expanduser().resolve()) for d in (cfg.add_dirs or [])]
        self._option_warnings: List[str] = []
        self._apply_agent_options()

    # ---------------------------------------------------------------- agent options
    def _apply_agent_options(self):
        """Give this run its own spec copies carrying the chosen model / effort / extra dirs.

        Never mutates the caller's specs (or agents.BUILTIN_AGENTS). Values for agents whose
        adapter has no verified flag are ignored and reported once by select_team().
        """
        self.specs = {n: dict(s) for n, s in self.specs.items()}
        for kind, values, args_key, slot in (("model", self.cfg.models, "model_args", "_model"),
                                             ("lead model", self.cfg.lead_models, "model_args", "_lead_model"),
                                             ("effort", self.cfg.effort, "effort_args", "_effort"),
                                             ("lead effort", self.cfg.lead_effort, "effort_args", "_lead_effort")):
            for name, value in (values or {}).items():
                value = str(value or "").strip()
                if not value:
                    continue
                key = name if name in self.specs else str(name).lower()
                spec = self.specs.get(key)
                effort_model = spec.get("_model") if spec else None
                if kind == "lead effort" and spec:
                    effort_model = spec.get("_lead_model", effort_model)
                    if effort_model == "default":
                        effort_model = None
                if spec is None:
                    self._option_warnings.append(f"{kind} '{value}' for '{name}' ignored: no AI CLI with that id")
                elif kind == "lead model" and value.lower() == "default":
                    spec[slot] = "default"
                elif not spec.get(args_key):
                    self._option_warnings.append(f"{key} has no supported way to set a {kind}; ignoring {kind} '{value}'")
                else:
                    if kind in ("effort", "lead effort") and key in ("codex", "muse") and effort_model:
                        from omnicli import model_catalog
                        if (model_catalog.cached_effort_levels(spec, effort_model) is None
                                and model_catalog._bin_is(spec, key)):
                            model_catalog.discover(key, spec)
                    if kind in ("effort", "lead effort") and not ag.effort_supported(
                            spec, effort_model, value):
                        levels = ag.effort_levels(spec, effort_model)
                        if key == "codex" and effort_model:
                            reason = (f"catalog support was not confirmed for model '{effort_model}'; "
                                      "run `omnicli models codex --refresh` to refresh it")
                        else:
                            reason = "it accepts " + (", ".join(levels) if levels else "only the CLI default")
                        self._option_warnings.append(f"{kind} '{value}' ignored for {key}: {reason}")
                    else:
                        spec[slot] = value
        if self.add_dirs:
            for spec in self.specs.values():
                if spec.get("add_dir_args"):
                    spec["_add_dirs"] = list(self.add_dirs)
        if getattr(self.cfg, "images", None):
            for spec in self.specs.values():
                spec["_images"] = list(self.cfg.images)

    def _report_agent_options(self):
        for w in self._option_warnings:
            self.say(f"{BADGE_WARN} {w}")
        self._option_warnings = []  # warn once
        chosen = []
        for n, spec in self.specs.items():
            if n != self.lead and n not in self.workers:
                continue
            parts = []
            if n in self.workers and spec.get("_model"):
                parts.append("worker model " + spec["_model"])
            if n == self.lead and spec.get("_lead_model"):
                parts.append("lead model " + ("CLI default" if spec["_lead_model"] == "default" else spec["_lead_model"]))
            elif n == self.lead and spec.get("_model"):
                parts.append("lead inherits worker model " + spec["_model"])
            if n in self.workers and spec.get("_effort"):
                parts.append("worker effort " + spec["_effort"])
            if n == self.lead:
                if spec.get("_lead_effort"):
                    parts.append("lead effort " + spec["_lead_effort"])
                elif spec.get("_lead_model") == "default" or (
                        spec.get("_lead_model") and spec.get("_lead_model") != spec.get("_model")):
                    parts.append("lead effort CLI default")
                elif spec.get("_effort"):
                    parts.append("lead inherits worker effort " + spec["_effort"])
            if parts:
                chosen.append(f"{n}: " + ", ".join(parts))
        if chosen:
            self.say(f"{BADGE_INFO} Agent options: {'; '.join(chosen)}")
        if self.add_dirs:
            lacking = [w for w in self.workers if not self.specs[w].get("add_dir_args")]
            self.say(f"{BADGE_INFO} Extra directories for workers: {', '.join(self.add_dirs)}")
            if lacking:
                self.say(f"{BADGE_WARN} {', '.join(lacking)} cannot be given extra directories (no --add-dir "
                         f"equivalent); it will only work inside {self.dir}")

    # ---------------------------------------------------------------- budget
    def _budget_hit(self) -> str:
        """Why the session budget is used up ('' if it isn't)."""
        from omnicli.budgets import read_control
        live = read_control(self.dir)
        if "max_calls" in live:
            self.cfg.max_calls = int(live["max_calls"] or 0)
        if "max_minutes" in live:
            self.cfg.max_minutes = float(live["max_minutes"] or 0)
        max_calls = int(self.cfg.max_calls or 0)
        max_min = float(self.cfg.max_minutes or 0)
        if max_calls > 0 and self.calls >= max_calls:
            return f"{self.calls}/{max_calls} AI calls"
        if max_min > 0:
            used = (time.time() - self._run_started) / 60
            if used >= max_min:
                return f"{used:.1f}/{max_min:g} minutes"
        return ""

    def _reserve_call(self) -> bool:
        """Count one agent call against the budget; False (and remember why) if none is left."""
        with self.lock:
            reason = self._budget_hit()
            if reason:
                if not self.budget_reason:
                    self.budget_reason = reason
                return False
            self.calls += 1
            return True

    def _stop_for_budget(self) -> int:
        with self.lock:
            for t in self.tasks:
                if t.status == "running":
                    t.status = "pending"
            self.phase = "stopped"
            self.save_state()
        self.say(f"\n{BADGE_WARN} Budget reached ({self.budget_reason}); progress saved — "
                 f"continue with: omnicli resume {self.dir}")
        self._notify("Team run paused", "Budget reached; progress is saved.")
        return 3

    def _notify(self, title, message):
        if self.cfg.notify:
            from omnicli.notifications import notify
            notify(title, message)

    def result_summary(self, exit_code: int) -> Dict:
        """Machine-readable outcome of run() (printed by `omnicli team --json`)."""
        with self.lock:
            counts = {k: 0 for k in ("done", "failed", "skipped", "pending")}
            for t in self.tasks:
                k = "pending" if t.status == "running" else t.status
                counts[k] = counts.get(k, 0) + 1
            counts["total"] = len(self.tasks)
            return {
                "dir": str(self.dir),
                "phase": self.phase,
                "complete": self.phase == "complete",
                "exit_code": int(exit_code),
                "tasks": counts,
                "report": str(self.report_path) if self._report_written else None,
                "calls": self.calls,
                "minutes": round((time.time() - self._run_started) / 60, 2),
            }

    # ---------------------------------------------------------------- output
    def say(self, msg: str = ""):
        if self.verbose:
            with self.lock:
                print(msg, flush=True)

    # ---------------------------------------------------------------- state
    def save_state(self):
        with self.lock:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "version": 1,
                "goal": self.goal,
                "project_name": self.project_name,
                "plan_summary": self.plan_summary,
                "workflows": self.workflows,
                "lead": self.lead,
                "workers": self.workers,
                "round": self.round,
                "final": self.final,
                "started_at": self.started_at,
                "updated_at": time.time(),
                "phase": self.phase,
                "pid": os.getpid() if self.phase in ("planning", "running") else 0,
                "protected_paths": self.protected_paths,
                "tasks": [asdict(t) for t in self.tasks],
            }
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
            run_registry.register_run({
                "dir": str(self.dir),
                "goal": self.goal,
                "project_name": self.project_name,
                "phase": self.phase,
                "lead": self.lead,
                "workers": list(self.workers),
                "tasks_total": len(self.tasks),
                "tasks_done": sum(1 for t in self.tasks if t.status == "done"),
                "tasks_failed": sum(1 for t in self.tasks if t.status == "failed"),
                "round": self.round,
                "started_at": self.started_at,
                "pid": data["pid"],
            })

    def load_state(self) -> bool:
        if not self.state_path.exists():
            return False
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.goal = data.get("goal") or self.goal
        self.project_name = str(self.cfg.project_name or "").strip() or data.get("project_name", "")
        self.plan_summary = data.get("plan_summary", "")
        self.round = int(data.get("round", 1))
        self.final = data.get("final") or {}
        self.started_at = data.get("started_at", time.time())
        self.phase = data.get("phase", "planned")
        self.protected_paths = [str(path) for path in data.get("protected_paths", []) if isinstance(path, str)]
        self.tasks = [Task.from_dict(t) for t in data.get("tasks", [])]
        self.workflows = workflows.from_plan(data, self.tasks) if data.get("workflows") else []
        for t in self.tasks:
            if t.status == "running":
                t.status = "pending"
        self._saved_lead = data.get("lead", "")
        self._saved_workers = data.get("workers", [])
        return True

    # ---------------------------------------------------------------- git
    def _git(self, *args: str, stdin: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(self.dir), input=stdin, capture_output=True, text=True)

    def setup_git(self):
        if not self.cfg.use_git or not shutil.which("git"):
            self.git = False
            return
        inside = self._git("rev-parse", "--is-inside-work-tree").returncode == 0
        created = False
        if not inside:
            if self._git("init", "-q").returncode != 0:
                self.git = False
                return
            created = True
        self.git = True
        git_dir = Path(self._git("rev-parse", "--absolute-git-dir").stdout.strip() or self.dir / ".git")
        exclude = git_dir / "info" / "exclude"
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
            missing = [e for e in GIT_EXCLUDES if e not in current.splitlines()]
            if missing:
                exclude.write_text(current.rstrip("\n") + "\n# added by omnicli team\n" + "\n".join(missing) + "\n", encoding="utf-8")
        except Exception:
            pass
        self._git_ident = []
        if not self._git("config", "user.email").stdout.strip():
            self._git_ident = ["-c", "user.name=OmniCLI", "-c", "user.email=omnicli@localhost"]
        has_head = self._git("rev-parse", "--verify", "-q", "HEAD").returncode == 0
        dirty = bool(self._git("status", "--porcelain", "--", ".").stdout.strip())
        if dirty:
            if self.cfg.use_worktrees and self.protected_paths:
                if not has_head:
                    self.git = False
                    self.say(f"{BADGE_FAILED} Active edits are protected, but isolated worktrees need an existing Git commit.")
                    self.say(c_dim("Create a commit before starting the team, or wait until the active edits are finished."))
                    return
                current_paths = uncommitted_paths(self.dir)
                unknown = [path for path in current_paths if path not in self.protected_paths]
                if unknown:
                    self.git = False
                    self.say(f"{BADGE_FAILED} New files appeared after the plan; refusing to baseline-commit them automatically.")
                    self.say(c_dim("Re-plan so OmniCLI can protect the latest edits."))
                    return
                self.say(f"{BADGE_INFO} Leaving {len(current_paths)} protected path(s) untouched in the main checkout; workers will use isolated worktrees from HEAD.")
                return
            files = [line[len("add '"):-1] if line.startswith("add '") else line
                     for line in self._git("add", "-A", "--dry-run", "--", ".").stdout.splitlines() if line.strip()]
            if created or not has_head:
                question = (f"OmniCLI saves a git checkpoint after every task. To start, it would commit these "
                            f"{len(files)} existing file(s) as a baseline:")
            else:
                question = (f"{self.dir} has {len(files)} uncommitted change(s). OmniCLI would commit them as a "
                            "baseline before the team starts:")
            self.say(question)
            for f in files[:20]:
                self.say(f"  {f}")
            if len(files) > 20:
                self.say(c_dim(f"  ... and {len(files) - 20} more"))
            if not self.cfg.auto_approve and not self._ask_yes_no("Commit them as a baseline?"):
                self.git = False
                if created:
                    shutil.rmtree(self.dir / ".git", ignore_errors=True)  # only the repo we just made
                self.say(f"{BADGE_INFO} No git checkpoints this run (nothing was committed). "
                         "Use --no-git to skip this question next time.")
                return
            self._git("add", "-A", "--", ".")
            self._git(*self._git_ident, "commit", "-q", "-m", "omnicli: baseline before team run")

    def _ask_yes_no(self, question: str) -> bool:
        try:
            ans = self.input_fn(c_bold(c_yellow(question + " [y/N]: "))).strip().lower()
        except (KeyboardInterrupt, EOFError):
            return False
        return ans in ("y", "yes")

    def _refresh_protected_paths(self) -> List[str]:
        """Add files changed since planning and report any newly conflicting task scopes."""
        current = uncommitted_paths(self.dir)
        new_paths = [path for path in current if path not in self.protected_paths]
        if new_paths:
            self.protected_paths = list(dict.fromkeys(self.protected_paths + new_paths))
        return protected_scope_conflicts(self.tasks, new_paths)

    def _dirty_fingerprint(self) -> Dict[str, str]:
        """{path: content fingerprint} for every uncommitted, non-generated path in the main checkout."""
        prints: Dict[str, str] = {}
        for rel in uncommitted_paths(self.dir):
            if is_generated_path(rel):
                continue
            path = self.dir / rel
            try:
                if path.is_file():
                    st = path.stat()
                    prints[rel] = (hashlib.sha1(path.read_bytes()).hexdigest() if st.st_size <= 4_000_000
                                   else f"{st.st_size}:{st.st_mtime_ns}")
                else:
                    prints[rel] = "<dir>" if path.is_dir() else "<deleted>"
            except OSError:
                prints[rel] = "<unreadable>"
        return prints

    def _out_of_scope_changes(self, task: "Task", before: Optional[Dict[str, str]]) -> List[str]:
        """Files changed in the shared checkout while `task` ran that are outside its scope and not owned
        by another task. Those edits would otherwise skip the lead's review and stay in the project."""
        if before is None:
            return []
        after = self._dirty_fingerprint()
        with self.lock:
            others = [o.owns for o in self.tasks
                      if o is not task and o.owns and o.status in ("running", "done", "failed")]
        return [path for path in sorted(set(before) | set(after))
                if before.get(path) != after.get(path)
                and not file_in_scope(path, task.owns)
                and not any(file_in_scope(path, owns) for owns in others)]

    def _changed_files(self, scope: Optional[List[str]]) -> List[str]:
        # Stage only the task's declared paths. `git add -A .` also stages files that
        # another person or process may be editing in this shared checkout.
        if scope:
            pathspec = []
            for path in scope:
                if not path:
                    pathspec.append(".")
                elif os.path.lexists(self.dir / path) or self._git("ls-files", "--", ":(literal)" + path).stdout.strip():
                    pathspec.append(":(literal)" + path)
            if not pathspec:
                return []
        else:
            pathspec = ["."]
        self._git("add", "-A", "--", *pathspec)
        out = self._git("diff", "--cached", "--name-only", "--relative", "--", ".").stdout
        files = [f for f in out.splitlines() if f.strip()]
        if scope is not None:
            files = [f for f in files if file_in_scope(f, scope)]
        return files

    def collect_changes(self, scope: Optional[List[str]]) -> Tuple[List[str], str]:
        """(changed files, human-readable diff) since the last checkpoint, limited to scope if given."""
        if not self.git:
            return [], ""
        with self.git_lock:
            files = self._changed_files(scope)
            if not files:
                return [], ""
            spec = files if len(files) <= 300 else (scope or ["."])
            spec = [":(literal)" + path for path in spec] if scope else spec
            stat = self._git("diff", "--cached", "--stat", "--relative", "--", *spec).stdout
            diff = self._git("diff", "--cached", "--relative", "--", *spec).stdout
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + f"\n... (diff truncated, {len(diff)} chars total - read the files directly for more)"
        return files, f"{stat.strip()}\n\n{diff}".strip()

    def _task_workspace(self, task: Task) -> Path:
        """Create or reuse a detached Git worktree for one task."""
        if not self.cfg.use_worktrees:
            return self.dir
        if not self.git:
            raise RuntimeError("isolated workspaces require git checkpoints; remove --no-git or disable --worktrees")
        if task.workspace:
            existing = Path(task.workspace)
            if existing.is_dir() and (existing / ".git").exists():
                return existing
        dirty_paths = uncommitted_paths(self.dir)
        if dirty_paths:
            unexpected = [path for path in dirty_paths if path not in self.protected_paths]
            conflicts = protected_scope_conflicts([task], dirty_paths)
            if unexpected or conflicts:
                raise RuntimeError("isolated workspace cannot start: project files changed after planning or overlap "
                                   "this task's owned paths")
        workspace = self.state_dir / "worktrees" / task.id
        workspace.parent.mkdir(parents=True, exist_ok=True)
        with self.git_lock:
            has_head = self._git("rev-parse", "--verify", "-q", "HEAD").returncode == 0
            if not has_head:
                baseline = self._git(*self._git_ident, "commit", "--allow-empty", "-q", "-m",
                                     "omnicli: isolated-worktree baseline")
                if baseline.returncode:
                    raise RuntimeError("could not create the Git baseline for worktrees: {}".format(
                        (baseline.stderr or baseline.stdout).strip()))
            result = self._git("worktree", "add", "--detach", str(workspace), "HEAD")
        if result.returncode:
            raise RuntimeError("could not create task worktree: {}".format((result.stderr or result.stdout).strip()))
        task.workspace = str(workspace)
        self.save_state()
        return workspace

    def _collect_workspace_changes(self, workspace: Path, scope: Optional[List[str]]) -> Tuple[List[str], str, str]:
        """Stage a task worktree and return its files, diff, and binary patch against its base."""
        add = subprocess.run(["git", "add", "-A"], cwd=str(workspace), capture_output=True, text=True)
        if add.returncode:
            return [], "", ""
        pathspec = [":(literal)" + path for path in scope if path] if scope else ["."]
        if not pathspec:
            pathspec = ["."]
        files = subprocess.run(["git", "diff", "--cached", "--name-only", "--relative", "HEAD", "--"] + pathspec,
                               cwd=str(workspace), capture_output=True, text=True).stdout.splitlines()
        stat = subprocess.run(["git", "diff", "--cached", "--stat", "--relative", "HEAD", "--"] + pathspec,
                              cwd=str(workspace), capture_output=True, text=True).stdout
        diff = subprocess.run(["git", "diff", "--cached", "--relative", "HEAD", "--"] + pathspec,
                              cwd=str(workspace), capture_output=True, text=True).stdout
        patch = subprocess.run(["git", "diff", "--cached", "--binary", "HEAD", "--"] + pathspec,
                               cwd=str(workspace), capture_output=True, text=True).stdout
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + f"\n... (diff truncated, {len(diff)} chars total - read the files directly for more)"
        return [f for f in files if f.strip()], f"{stat.strip()}\n\n{diff}".strip(), patch

    def _checkpoint_workspace(self, task: Task, workspace: Path, scope: Optional[List[str]], label: str = "") -> str:
        """Apply the reviewed task patch to the main tree and checkpoint it there."""
        _files, _diff, patch = self._collect_workspace_changes(workspace, scope)
        if not patch:
            return ""
        with self.git_lock:
            applied = self._git("apply", "--index", "--binary", "-", stdin=patch)
            if applied.returncode:
                raise RuntimeError("could not merge task worktree changes: {}".format((applied.stderr or applied.stdout).strip()))
            return self.checkpoint(task, scope, label)

    def _remove_task_workspace(self, task: Task) -> None:
        if not task.workspace:
            return
        with self.git_lock:
            result = self._git("worktree", "remove", "--force", task.workspace)
        if result.returncode == 0:
            task.workspace = ""
            self.save_state()

    def checkpoint(self, task: Task, scope: Optional[List[str]], label: str = "") -> str:
        if not self.git:
            return ""
        with self.git_lock:
            files = self._changed_files(scope)
            if not files:
                return ""
            msg = f"{task.id}: {task.title} [{task.agent_used}]{(' ' + label) if label else ''}\n\nOmniCLI team checkpoint."
            res = self._git(*self._git_ident, "commit", "-q", "-m", msg, "--pathspec-from-file=-", stdin="\n".join(files) + "\n")
            if res.returncode != 0:
                self.say(f"{BADGE_WARN} checkpoint for {task.id} failed: {res.stderr.strip()[:200]}")
                return ""
            return self._git("rev-parse", "--short", "HEAD").stdout.strip()

    # ---------------------------------------------------------------- team
    def select_team(self) -> bool:
        order = [n for n in ag.PREFERRED_LEAD_ORDER if n in self.specs] + \
                [n for n in self.specs if n not in ag.PREFERRED_LEAD_ORDER]
        if self._health_override is not None:
            health = self._health_override
        else:
            self.say(f"{BADGE_INFO} Checking which AI CLIs are installed and logged in...")
            health = ag.check_health(self.specs, names=order, force=self.cfg.recheck)

        rows = []
        for n in order:
            h = health.get(n, {})
            if not h.get("installed", True) and not h.get("ok"):
                continue
            state = c_green("ready") if h.get("ok") else c_red("unavailable")
            rows.append([c_bold(c_cyan(n)), self.specs[n].get("name", n), state, c_dim(h.get("reason", ""))])
        if rows and self.verbose:
            print_table(["Agent", "CLI", "Status", "Detail"], rows, title="AI CLIs on this device")

        self.healthy = [n for n in order if health.get(n, {}).get("ok")]
        if not self.healthy:
            self.say(f"{BADGE_FAILED} No AI CLI is usable right now. Log in to at least one (e.g. `claude`, `codex login`, `agy`, `muse login`).")
            return False

        wanted_lead = self.cfg.lead or getattr(self, "_saved_lead", "")
        if wanted_lead and wanted_lead in self.healthy:
            self.lead = wanted_lead
        else:
            if wanted_lead:
                why = health.get(wanted_lead, {}).get("reason", "unknown agent")
                self.say(f"{BADGE_WARN} Lead '{wanted_lead}' is not usable ({why}); choosing another.")
            self.lead = self.healthy[0]

        wanted_workers = self.cfg.workers or getattr(self, "_saved_workers", None) or list(self.healthy)
        self.workers = []
        for w in wanted_workers:
            if str(w).lower() in ("me", "human") and self.cfg.human_worker:
                w = "human"
            if w == "human" and self.cfg.human_worker:
                self.workers.append(w)
            elif w in self.healthy:
                self.workers.append(w)
            else:
                why = health.get(w, {}).get("reason", "unknown agent")
                self.say(f"{BADGE_WARN} Worker '{w}' skipped: {why}")
        if not self.workers:
            self.workers = list(self.healthy)
        if self.cfg.human_worker and "human" not in self.workers:
            self.workers.append("human")
        self.say(f"\n{c_bold('Lead:')} {c_magenta(self.lead)}   {c_bold('Workers:')} {', '.join(c_cyan(w) for w in self.workers)}")
        self._report_agent_options()
        return True

    def _usable(self, name: str) -> bool:
        reset = self.quota_resets.get(name, {}).get("reset_at", 0)
        down = name in self.down and name not in self.quota_down
        return name in self.workers and not down and float(reset or 0) <= time.time()

    def _vendor(self, name: str) -> str:
        spec = self.specs.get(name, {})
        return str(spec.get("vendor") or ("custom:" + name.lower())).strip().lower()

    def _pick_worker(self, exclude: List[str]) -> Optional[str]:
        with self.lock:
            options = [w for w in self.workers if self._usable(w) and w not in exclude]
            if not options:
                return None
            load = {w: sum(1 for r in self.running.values() if r["agent"] == w) for w in options}
            return min(options, key=lambda w: (load[w], self.workers.index(w)))

    def _wait_for_worker(self, exclude: Optional[List[str]] = None) -> Optional[str]:
        """Wait for the earliest known quota reset when every available worker is capped."""
        excluded = exclude or []
        while not self.stopping:
            self.quota_resets = ledger.active_quota_resets()
            worker = self._pick_worker(excluded)
            if worker:
                return worker
            resets = [float(self.quota_resets.get(name, {}).get("reset_at", 0) or 0)
                      for name in self.workers if name not in self.down and name in self.quota_resets]
            if not resets:
                return None
            reset_at = min(resets)
            delay = max(0.0, reset_at - time.time())
            if delay <= 0:
                continue
            reset_text = datetime.datetime.fromtimestamp(reset_at).astimezone().strftime("%I:%M %p").lstrip("0")
            self.say(f"{BADGE_WARN} Work queued until an agent's usage resets at {reset_text}; waiting (Ctrl-C to pause).")
            time.sleep(min(30.0, delay))
        return None

    def _mark_down(self, name: str, reason: str):
        with self.lock:
            if name not in self.down:
                self.down[name] = reason
                self.say(f"{BADGE_WARN} {name} taken off the team for this run: {reason}")
        if self._health_override is None:
            ag.mark_unhealthy(name, reason)

    def _mark_quota_capped(self, name: str, reason: str, reset_at: float):
        self.quota_resets[name] = {"reset_at": float(reset_at), "reason": reason, "observed_at": time.time()}
        self.quota_down.add(name)
        self.down[name] = reason
        self.say(f"{BADGE_WARN} {name} usage resets at "
                 f"{datetime.datetime.fromtimestamp(reset_at).astimezone().strftime('%I:%M %p').lstrip('0')}; "
                 "routing work to another agent.")

    def _run_red_team(self, task: Task, builder: str, workspace: Path, on_line=None) -> None:
        """Ask an independent provider to add adversarial tests before normal verification."""
        if not self.cfg.red_team:
            return
        builder_vendor = self._vendor(builder)
        candidates = [name for name in self.healthy if name != builder and self._vendor(name) != builder_vendor
                      and name not in self.down]
        reviewer = next((name for name in candidates
                         if float(self.quota_resets.get(name, {}).get("reset_at", 0) or 0) <= time.time()), None)
        if not reviewer:
            self.say(f"{BADGE_WARN} {task.id}: no available different-vendor agent for red-team tests; continuing without them")
            return
        if not self._reserve_call():
            self.say(f"{BADGE_WARN} {task.id}: session budget reached; skipping red-team tests")
            return
        prompt = build_red_team_prompt(task, workspace)
        spec = self.specs[reviewer]
        run = ag.run_agent(spec, prompt, "edit", str(workspace), timeout=self.cfg.task_timeout_min * 60,
                           log_path=self.logs / f"{task.id}-red-team-{reviewer}-{len(task.attempts) + 1}.md",
                           on_line=on_line,
                           meta={"kind": "task", "run_dir": str(self.dir), "task_id": task.id})
        task.attempts.append({"agent": reviewer, "role": "red_team", "result": "tests generated" if run.ok else
                              "tests unavailable: " + run.failure_reason(), "duration_sec": round(run.duration, 1),
                              "at": datetime.datetime.now().isoformat(timespec="seconds")})
        if run.ok:
            self.say(f"{BADGE_INFO} {task.id}: {reviewer} added independent red-team tests")
        else:
            self.say(f"{BADGE_WARN} {task.id}: red-team test pass unavailable ({run.failure_reason()})")

    # ---------------------------------------------------------------- lead
    def ask_lead(self, kind: str, prompt: str, required_key: str,
                 exclude_vendor: Optional[str] = None, workspace: Optional[Path] = None,
                 validate: Optional[Callable[[Dict], str]] = None) -> Optional[Dict]:
        """Ask the lead for a JSON decision; on failure fall back to another healthy agent as lead.

        Returns None when no lead answers, including when the session budget is used up
        (then self.budget_reason says why).
        """
        candidates = [n for n in [self.lead] + [x for x in self.healthy if x != self.lead]
                      if exclude_vendor is None or self._vendor(n) != exclude_vendor]
        for name in candidates:
            if name in self.quota_down and float(self.quota_resets.get(name, {}).get("reset_at", 0) or 0) <= time.time():
                self.quota_down.discard(name)
                self.down.pop(name, None)
            reset_at = float(self.quota_resets.get(name, {}).get("reset_at", 0) or 0)
            if name in self.down or reset_at > time.time():
                continue
            p = prompt
            for attempt in (1, 2):
                if not self._reserve_call():
                    return None
                with self.lock:
                    self._lead_calls += 1
                    n = self._lead_calls
                spec = self.specs[name]
                lead_model = spec.get("_lead_model")
                lead_effort = spec.get("_lead_effort")
                if lead_model is not None or lead_effort is not None:
                    spec = dict(spec)
                    worker_model = spec.get("_model")
                    if lead_model == "default":
                        spec.pop("_model", None)
                        if lead_effort is None:
                            spec.pop("_effort", None)
                    else:
                        if lead_model:
                            spec["_model"] = lead_model
                        if lead_effort is None and lead_model and lead_model != worker_model:
                            spec.pop("_effort", None)
                    if lead_effort is not None:
                        spec["_effort"] = lead_effort
                run = ag.run_agent(spec, p, "readonly", str(workspace or self.dir),
                                   timeout=self.cfg.lead_timeout_min * 60,
                                   log_path=self.logs / f"lead-{n:03d}-{kind}-{name}.md",
                                   meta={"kind": kind, "run_dir": str(self.dir)})
                if self.stopping:
                    return None
                if not run.ok:
                    self.say(f"{BADGE_WARN} lead {name} failed during {kind}: {run.failure_reason()}")
                    if run.quota_problem and run.quota_reset_at:
                        self._mark_quota_capped(name, run.failure_reason(), run.quota_reset_at)
                    elif run.auth_problem or run.quota_problem or run.error:
                        self._mark_down(name, run.failure_reason())
                    break
                data = ag.extract_json(run.output, required_key)
                error = validate(data) if validate and data is not None else ""
                if data is not None and not error:
                    if name != self.lead:
                        self.say(f"{BADGE_WARN} {self.lead} could not lead; {c_magenta(name)} is now the lead.")
                        self.lead = name
                    return data
                p = prompt + "\n\n" + (error or "Your previous reply could not be parsed.") + " Reply with only the corrected JSON."
        resets = [float(self.quota_resets.get(name, {}).get("reset_at", 0) or 0)
                  for name in candidates if name not in self.down and
                  float(self.quota_resets.get(name, {}).get("reset_at", 0) or 0) > time.time()]
        if resets and not self.stopping:
            reset_at = min(resets)
            reset_text = datetime.datetime.fromtimestamp(reset_at).astimezone().strftime("%I:%M %p").lstrip("0")
            self.say(f"{BADGE_WARN} All available leads are at a usage cap; waiting for {reset_text}.")
            while not self.stopping and time.time() < reset_at:
                time.sleep(min(30.0, reset_at - time.time()))
                self.quota_resets = ledger.active_quota_resets()
            if not self.stopping:
                return self.ask_lead(kind, prompt, required_key, exclude_vendor=exclude_vendor,
                                     workspace=workspace, validate=validate)
        return None

    # ---------------------------------------------------------------- plan
    def plan(self) -> bool:
        self.say(f"\n{BADGE_INFO} {c_magenta(self.lead)} is planning the project...")
        have, missing = tool_report()
        plan_specs = dict(self.specs)
        if self.cfg.human_worker:
            plan_specs["human"] = {"name": "You", "strengths": "product judgment, hands-on implementation, and domain knowledge"}
        self.protected_paths = uncommitted_paths(self.dir)
        prompt = build_plan_prompt(self.goal, self.dir, plan_specs, self.workers, have, self.cfg.max_tasks, missing,
                                   ledger.skill_recommendations(), self.protected_paths)
        plan_workers = list(self.workers) + (["human"] if self.cfg.human_worker else [])
        for attempt in range(2):
            data = self.ask_lead("plan", prompt, "tasks")
            if not data or not isinstance(data.get("tasks"), list) or not data["tasks"]:
                if not self.budget_reason:
                    self.say(f"{BADGE_FAILED} The lead did not return a usable plan. Raw replies are in {self.logs}")
                return False
            tasks = normalize_tasks(data["tasks"], plan_workers, max_tasks=self.cfg.max_tasks, round_no=1)
            conflicts = protected_scope_conflicts(tasks, self.protected_paths)
            if conflicts:
                problem = "PLAN REJECTED FOR FILE OWNERSHIP CONFLICTS: " + "; ".join(conflicts)
            else:
                try:
                    self.workflows = workflows.from_plan(data, tasks)
                    self.tasks = tasks
                    self.project_name = (str(self.cfg.project_name or "").strip()
                                         or str(data.get("project_name") or self.dir.name))
                    self.plan_summary = str(data.get("summary") or "")
                    break
                except ValueError as ex:
                    problem = str(ex)
            self.say(f"{BADGE_WARN} {problem}")
            if attempt:
                return False
            prompt += "\n\n" + problem + "\nReturn a complete corrected plan."
        self.save_state()
        return bool(self.tasks)

    def show_plan(self, tasks: Optional[List[Task]] = None, title: str = "Team plan"):
        tasks = tasks if tasks is not None else self.tasks
        if self.plan_summary and tasks is self.tasks:
            self.say(f"\n{c_bold('Approach:')} {self.plan_summary}")
        if tasks is self.tasks and self.workflows:
            self.say(c_bold("User workflows:") + "\n" + workflows.describe(self.workflows))
        if tasks is self.tasks and self.protected_paths:
            if self.cfg.use_git:
                self.say(f"{BADGE_INFO} Detected {len(self.protected_paths)} active-edit path(s). OmniCLI will preserve them and run worker tasks in isolated Git worktrees.")
            else:
                self.say(f"{BADGE_WARN} Detected {len(self.protected_paths)} active-edit path(s). Git checkpoints are required to isolate worker tasks safely.")
        rows = []
        for t in tasks:
            rows.append([
                c_bold(t.id), c_cyan(t.assignee), t.title[:58],
                ",".join(t.depends_on) or "-",
                (", ".join(t.owns)[:28] or c_dim("(runs alone)")),
                (t.verify[:34] + ("…" if len(t.verify) > 34 else "") if t.verify else c_dim("-")),
            ])
        if self.verbose:
            print_table(["ID", "Worker", "Task", "After", "Owns", "Verify"], rows, title=title)
            checks = [t for t in tasks if t.verify]
            if checks:
                self.say(c_bold("Commands that will run on your machine to check tasks:"))
                for t in checks:
                    self.say(f"  {c_bold(t.id)}  {t.verify}")
            for t in tasks:
                if t.verify_note:
                    self.say(f"{BADGE_WARN} {t.id}: the lead's check command was dropped ({t.verify_note}); "
                             "the lead will review this task by reading the code.")

    def confirm(self) -> bool:
        if self.cfg.auto_approve:
            return True
        while True:
            try:
                ans = self.input_fn(c_bold(c_yellow("Start the team on this plan? [Y/n/e = edit who does what]: "))).strip().lower()
            except (KeyboardInterrupt, EOFError):
                return False
            if ans in ("e", "edit"):
                self.edit_assignments()
                self.show_plan()
                continue
            return ans in ("", "y", "yes")

    def apply_assignments(self, text: str) -> List[str]:
        """Apply 't2=codex t3=muse' to pending tasks. Returns messages describing what happened."""
        msgs: List[str] = []
        by_id = {t.id.lower(): t for t in self.tasks}
        for tok in text.replace(",", " ").split():
            if "=" not in tok:
                msgs.append(f"skipped '{tok}' (use task=agent, e.g. t2=codex)")
                continue
            tid, agent = (x.strip() for x in tok.split("=", 1))
            t = by_id.get(tid.lower())
            agent = agent.lower()
            if t is None:
                msgs.append(f"no task '{tid}' (tasks: {', '.join(x.id for x in self.tasks)})")
            elif t.status != "pending":
                msgs.append(f"{t.id} is already {t.status}")
            elif agent not in self.healthy:
                msgs.append(f"{agent} isn't ready (ready: {', '.join(self.healthy)})")
            else:
                if agent not in self.workers:
                    self.workers.append(agent)
                t.assignee = agent
                msgs.append(f"{t.id} -> {agent}")
        self.save_state()
        return msgs

    def edit_assignments(self):
        self.say(c_dim("Edit one item per line: t2=codex, t2 title \"API tests\", t2 after t1, "
                       "t2 owns src/api, or t2 verify \"python3 -m unittest\". Enter 'done' to finish."))
        try:
            text = self.input_fn(c_bold("Edit plan: ")).strip()
        except (KeyboardInterrupt, EOFError):
            return
        for m in self.apply_plan_edits(text):
            self.say(f"  {m}")

    def apply_plan_edits(self, text: str) -> List[str]:
        """Edit pending task fields using concise, shell-quoted plan commands."""
        messages = []
        by_id = {t.id.lower(): t for t in self.tasks}
        for line in str(text or "").splitlines():
            try:
                parts = shlex.split(line)
            except ValueError as ex:
                messages.append("could not parse {!r}: {}".format(line, ex))
                continue
            if not parts:
                continue
            if parts[0].lower() in ("done", "finish", "q"):
                break
            if len(parts) == 1 and "=" in parts[0]:
                messages.extend(self.apply_assignments(parts[0]))
                continue
            if len(parts) < 3:
                messages.append("use TASK FIELD VALUE, e.g. t2 title \"API tests\"")
                continue
            task = by_id.get(parts[0].lower())
            field_name = parts[1].lower()
            value = " ".join(parts[2:]).strip()
            if task is None:
                messages.append("no task {!r}".format(parts[0]))
                continue
            if task.status != "pending":
                messages.append("{} is already {}".format(task.id, task.status))
                continue
            if field_name == "title":
                task.title = value
            elif field_name == "verify":
                task.verify = "" if value.lower() in ("none", "-") else value
            elif field_name in ("after", "depends_on"):
                deps = [] if value.lower() in ("none", "-") else [x.strip() for x in value.split(",") if x.strip()]
                known = {t.id for t in self.tasks}
                unknown = [dep for dep in deps if dep not in known]
                if unknown or task.id in deps:
                    messages.append("{} has unknown or self dependencies: {}".format(task.id, ", ".join(unknown or [task.id])))
                    continue
                by_task_id = {item.id: item for item in self.tasks}

                def reaches(target_id, wanted, seen):
                    if target_id == wanted:
                        return True
                    if target_id in seen or target_id not in by_task_id:
                        return False
                    seen.add(target_id)
                    return any(reaches(parent, wanted, seen) for parent in by_task_id[target_id].depends_on)

                if any(reaches(dep, task.id, set()) for dep in deps):
                    messages.append("{} dependencies would create a cycle".format(task.id))
                    continue
                task.depends_on = list(dict.fromkeys(deps))
            elif field_name in ("owns", "files"):
                paths = [] if value.lower() in ("none", "-") else [x.strip() for x in value.split(",") if x.strip()]
                normalized = [_norm_path(path) for path in paths]
                if any(path is None for path in normalized):
                    messages.append("{} owns paths must stay inside the project".format(task.id))
                    continue
                task.owns = list(dict.fromkeys(path for path in normalized if path))
            else:
                messages.append("unknown field {!r}; use title, after, owns, or verify".format(field_name))
                continue
            messages.append("updated {} {}".format(task.id, field_name))
        self.save_state()
        return messages

    def approve_task_preview(self, task: Task, agent: str, files: List[str], changes: str):
        """Show the proposed task diff and return (approve|revise|reject, feedback)."""
        self.say(f"\n{c_bold('Task preview:')} {task.id} · {agent} · {task.title}")
        self.say("Changed files: " + (", ".join(files) if files else "(no tracked changes detected)"))
        self.say((changes or "(empty diff)")[:MAX_DIFF_CHARS])
        with self.lock:
            try:
                choice = self.input_fn("Keep this change? [Y/es, r/evise, n/o]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "reject", "approval input ended"
            if choice in ("", "y", "yes"):
                return "approve", ""
            if choice in ("r", "revise"):
                try:
                    note = self.input_fn("What should the worker change? ").strip()
                except (EOFError, KeyboardInterrupt):
                    note = ""
                return "revise", note or "Please revise the proposed changes."
            return "reject", "user rejected the proposed changes"

    # ---------------------------------------------------------------- execution
    def _deps_done(self, t: Task) -> bool:
        status = {x.id: x.status for x in self.tasks}
        return all(status.get(d) == "done" for d in t.depends_on)

    def _skip_blocked(self):
        changed = True
        while changed:
            changed = False
            status = {x.id: x.status for x in self.tasks}
            for t in self.tasks:
                if t.status == "pending" and any(status.get(d) in ("failed", "skipped") for d in t.depends_on):
                    t.status = "skipped"
                    t.review_note = "skipped because a task it depends on did not finish"
                    self.say(f"{BADGE_WARN} {t.id} skipped (depends on a task that did not finish)")
                    changed = True

    def _can_start(self, t: Task, running: List[Task]) -> bool:
        if not running:
            return True
        if not t.owns:
            return False
        for r in running:
            if not r.owns or paths_overlap(t.owns, r.owns):
                return False
        return True

    def execute(self) -> None:
        jobs = max(1, int(self.cfg.jobs))
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
        futures: Dict[concurrent.futures.Future, Task] = {}
        try:
            while True:
                with self.lock:
                    self._skip_blocked()
                    running = [t for t in self.tasks if t.status == "running"]
                    for t in [x for x in self.tasks if x.status == "pending" and self._deps_done(x)]:
                        if len(futures) >= jobs:
                            break
                        reason = self._budget_hit()
                        if reason:  # start nothing new; running tasks finish
                            self.budget_reason = self.budget_reason or reason
                            break
                        if not self._can_start(t, running):
                            if not t.owns:
                                break  # an exclusive task waits for the floor; don't starve it
                            continue
                        t.status = "running"
                        running.append(t)
                        futures[pool.submit(self._run_task_safe, t)] = t
                    self.save_state()
                if not futures:
                    break
                done, _ = concurrent.futures.wait(list(futures), timeout=30,
                                                  return_when=concurrent.futures.FIRST_COMPLETED)
                for f in done:
                    futures.pop(f, None)
                self._heartbeat()
            with self.lock:
                for t in self.tasks:
                    if t.status == "pending" and not self.budget_reason:  # budget-paused tasks stay resumable
                        t.status = "skipped"
                        t.review_note = "could not be scheduled (unresolved dependencies)"
                self.save_state()
        except KeyboardInterrupt:
            # stop agents first so worker threads return instead of reviewing half-done work
            self.stopping = True
            ag.kill_all()
            raise
        finally:
            pool.shutdown(wait=not self.stopping)

    def _heartbeat(self):
        now = time.time()
        with self.lock:
            for tid, r in self.running.items():
                if now - r["last_out"] >= 60 and now - r["last_beat"] >= 60:
                    r["last_beat"] = now
                    mins, secs = divmod(int(now - r["start"]), 60)
                    self.say(c_dim(f"  ... {tid} · {r['agent']} still working ({mins}m{secs:02d}s)"))

    def _run_task_safe(self, t: Task):
        try:
            self._run_task(t)
        except Exception as ex:  # a bug in one task must not kill the whole team
            with self.lock:
                t.status = "failed"
                t.review_note = f"orchestrator error: {ex}"
                self.say(f"{BADGE_FAILED} {t.id}: orchestrator error: {ex}")
        finally:
            with self.lock:
                self._record_task_outcome(t)
                self.running.pop(t.id, None)
                self.save_state()

    def _record_task_outcome(self, task: Task) -> None:
        if task.outcome_recorded or task.status not in ("done", "failed") or not task.attempts:
            return
        revisions = sum(1 for attempt in task.attempts
                        if attempt.get("result") in ("revise", "user requested revision"))
        agent = task.agent_used or next((attempt.get("agent") for attempt in reversed(task.attempts)
                                         if attempt.get("agent")), task.assignee)
        ledger.record_task_outcome({
            "ts": time.time(), "run_dir": str(self.dir), "task_id": task.id, "round": task.round,
            "category": task.category, "agent": agent, "status": task.status,
            "approved_first_try": task.status == "done" and revisions == 0,
            "revisions": revisions, "duration_sec": round(task.duration_sec, 1),
        })
        task.outcome_recorded = True

    def run_verify(self, t: Task, cwd: Optional[Path] = None) -> Optional[Tuple[int, str, str]]:
        """(exit code, output tail, missing-dependency explanation or '')."""
        if not t.verify:
            return None
        # checked again here: state.json could have been edited after planning
        from omnicli import settings as _settings
        from omnicli.safecmd import run_verify_command, verify_allowed
        run_dir = Path(cwd or self.dir)
        ok, why = verify_allowed(t.verify, _settings.get("verify_policy"), project_dir=run_dir)
        if not ok:
            self.say(f"{BADGE_WARN} {t.id}: not running check `{t.verify}`: it {why}")
            return None
        try:
            # strict: the checked argvs run WITHOUT a shell; on macOS each process is sandboxed
            code, out = run_verify_command(t.verify, run_dir, env=ag.agent_env(), timeout=600,
                                           policy=_settings.get("verify_policy"),
                                           sandbox=bool(_settings.get("verify_sandbox")))
            out = out.strip()
            return code, out[-3000:], missing_dependency(code, out, run_dir)
        except Exception as ex:
            return 1, f"could not run verify command: {ex}", ""

    def workflow_issues(self, commands, workspace):
        issues = {}
        for command in dict.fromkeys(commands):
            check = Task(id="workflow", title="", assignee="", instructions="", verify=command)
            problem = workflows.check_problem(command, self.run_verify(check, workspace))
            if problem:
                issues[command] = problem
        return issues

    def _run_task(self, t: Task):
        start = time.time()
        workspace = self._task_workspace(t)
        finished = [task for task in self.tasks if task.status in ("done", "failed") and task.id != t.id]
        linked_checks = workflows.interaction_commands(self.workflows, finished)
        previous = [cmd for cmd in linked_checks if cmd != t.verify]
        if t.passing_workflow_checks is None:
            issues = self.workflow_issues(previous, workspace)
            t.passing_workflow_checks = [cmd for cmd in previous if cmd not in issues]
            if previous:
                self.save_state()
        interaction = t.verify in linked_checks or bool(workflows.interaction_commands(self.workflows, [t]))
        tried: List[str] = []
        agent = "human" if t.assignee == "human" and self.cfg.human_worker else (
            t.assignee if self._usable(t.assignee) else self._pick_worker(exclude=[]))
        if agent and agent != t.assignee:
            self.say(f"{BADGE_WARN} {t.id}: {t.assignee} unavailable, giving it to {agent}")
        revision, feedback = 0, ""
        if t.attempts and t.attempts[-1].get("result") == "revise" and "workflow_regressions" in t.attempts[-1]:
            last = t.attempts[-1]
            revision, feedback = last.get("revision", 0) + 1, last["feedback"]
            if agent != "human":
                previous_builders = [a["agent"] for a in t.attempts if a.get("result") == "revise"]
                agent = self._pick_worker(exclude=previous_builders + ["human"]) or agent
        # In the shared checkout only the task's scope is reviewed and checkpointed, so remember what was
        # already changed when the task started (once, so undoing a stray edit on a retry counts as clean):
        # anything else changed outside the scope must not slip past the review.
        before_dirty = self._dirty_fingerprint() if workspace == self.dir and self.git and t.owns else None

        while True:
            if self.stopping:
                return
            if agent is None:
                agent = self._wait_for_worker()
                if agent:
                    self.say(f"{BADGE_INFO} {t.id} resumed with {c_cyan(agent)} after its quota reset")
                    continue
                with self.lock:
                    t.status = "failed"
                    t.review_note = "no worker could complete this task"
                    t.duration_sec = time.time() - start
                self.say(f"{BADGE_FAILED} {t.id}: no worker left to try")
                return

            if agent != "human" and not self._reserve_call():
                # budget used up before this worker call: not a failure - leave it for `omnicli resume`
                with self.lock:
                    t.status = "pending"
                self.say(f"{BADGE_WARN} {t.id} paused: budget reached ({self.budget_reason})")
                return

            spec = self.specs.get(agent, {})
            label = f"{t.id}·{agent}"
            with self.lock:
                self.running[t.id] = {"agent": agent, "start": time.time(), "last_out": time.time(), "last_beat": 0.0}
            kind = f"revision {revision}" if revision else "start"
            self.say(f"\n{c_bold(c_yellow('▶'))} {c_bold(t.id)} {c_cyan(agent)} {kind}: {c_bold(t.title)}")

            prompt = build_worker_prompt(t, self.goal, workspace, spec.get("name", agent), self.lead, self.tasks,
                                         feedback=feedback, revision=revision,
                                         previous_agents=[a for a in tried if a != agent],
                                         extra_dirs=spec.get("_add_dirs") or None,
                                         team_memory=ledger.read_team_memory(t.category, project=str(self.dir)),
                                         model=spec.get("_model", ""), effort=spec.get("_effort", ""),
                                         protected_paths=self.protected_paths, plan_summary=self.plan_summary,
                                         user_workflows=self.workflows)

            def on_line(line: str, _label=label, _tid=t.id):
                with self.lock:
                    if _tid in self.running:
                        self.running[_tid]["last_out"] = time.time()
                if line.strip() and self.cfg.stream_output:
                    self.say(f"  {c_dim('[' + _label + ']')} {line}")

            if agent == "human":
                self.say("\n" + prompt + "\n")
                self.say(f"{c_bold('Your task workspace:')} {workspace}")
                self.say("Make the changes in that directory, then return here and type 'done'. Type 'skip' to reassign it.")
                try:
                    answer = self.input_fn(c_bold(c_yellow("Your task is ready [done/skip]: "))).strip().lower()
                except (KeyboardInterrupt, EOFError):
                    answer = "skip"
                if answer not in ("done", "d", "yes", "y"):
                    run = ag.AgentRun(agent, 1, "", "", time.time() - start, error="human task skipped")
                else:
                    run = ag.AgentRun(agent, 0, "Completed by the user.", "", time.time() - start)
            else:
                run = ag.run_agent(spec, prompt, "edit", str(workspace), timeout=self.cfg.task_timeout_min * 60,
                                   log_path=self.logs / f"{t.id}-{agent}-{len(t.attempts) + 1}.md", on_line=on_line,
                                   meta={"kind": "task", "run_dir": str(self.dir), "task_id": t.id})
            if self.stopping:
                return
            if run.ok and agent != "human":
                self._run_red_team(t, agent, workspace, on_line)
                if self.stopping:
                    return
            attempt = {"agent": agent, "revision": revision, "exit_code": run.exit_code,
                       "duration_sec": round(run.duration, 1), "at": datetime.datetime.now().isoformat(timespec="seconds")}

            if not run.ok:
                attempt["result"] = f"agent error: {run.failure_reason()}"
                with self.lock:
                    t.attempts.append(attempt)
                self.say(f"{BADGE_FAILED} {t.id} · {agent}: {run.failure_reason()}")
                if run.quota_problem and run.quota_reset_at:
                    self._mark_quota_capped(agent, run.failure_reason(), run.quota_reset_at)
                elif run.auth_problem or run.quota_problem or run.error:
                    self._mark_down(agent, run.failure_reason())
                tried.append(agent)
                agent = self._pick_worker(exclude=tried)
                if agent:
                    self.say(f"{BADGE_INFO} {t.id} reassigned to {c_cyan(agent)}")
                continue

            summary = extract_summary(run.output)
            verify = self.run_verify(t, workspace)
            if verify is not None and verify[2]:
                self.say(f"  {BADGE_WARN} verify `{t.verify}` could not run here: {verify[2]}")
            elif verify is not None:
                badge = BADGE_SUCCESS if verify[0] == 0 else BADGE_FAILED
                self.say(f"  {badge} verify `{t.verify}` -> exit {verify[0]}")

            # Even serial tasks review and checkpoint only their declared scope. This
            # keeps unrelated or concurrent edits out of a task's checkpoint.
            scope = t.owns or None
            if workspace == self.dir:
                files, changes = self.collect_changes(scope)
            else:
                files, changes, _patch = self._collect_workspace_changes(workspace, scope)
            outside = self._out_of_scope_changes(t, before_dirty)
            if outside:
                changes = (changes + "\n\n" if changes else "") + (
                    "WARNING: the worker ALSO changed these files OUTSIDE this task's scope. They are not in the "
                    "diff above and will not be checkpointed; the task cannot be approved until they are undone:\n"
                    + "\n".join("- " + path for path in outside[:50]))

            issues = self.workflow_issues(previous, workspace)
            regressions = {cmd: reason for cmd, reason in issues.items() if cmd in t.passing_workflow_checks}
            # A revision may restore a previously broken journey before it finishes its own task.
            # Keep that gain as a requirement for the next revision, even after a pause.
            t.passing_workflow_checks = [cmd for cmd in previous if cmd not in issues or cmd in regressions]
            behavior_problem = workflows.check_problem(t.verify, verify) if interaction else ""
            if regressions or behavior_problem:
                details = ["Previously passed `{}`, now {}.".format(cmd, reason)
                           for cmd, reason in regressions.items()]
                if behavior_problem:
                    details.append("Task check `{}`: {}.".format(t.verify, behavior_problem))
                attempt["workflow_regressions"] = list(regressions)
                verdict = {"verdict": "revise", "summary": first_line(summary),
                           "feedback": "Repair the implementation and keep the acceptance checks intact.\n"
                                       + "\n".join(details)}
            elif not verify_passed(verify):
                verdict = {"verdict": "revise", "summary": first_line(summary),
                           "feedback": f"Repair the behavior behind `{t.verify}` (exit {verify[0]}). "
                                       f"Keep the acceptance check intact and rerun it.\n{verify[1]}"}
            elif self.cfg.review:
                self.say(f"  {c_dim('lead ' + self.lead + ' reviewing ' + t.id + '...')}")
                verdict = self.ask_lead("review", build_review_prompt(
                    t, self.goal, agent, summary, changes, verify, revision, self.cfg.retries), "verdict",
                    exclude_vendor=self._vendor(agent) if self.cfg.cross_vendor_review else None, workspace=workspace)
                if self.stopping:
                    return
                if verdict is None:
                    ok = bool(verify and verify[0] == 0 and not self.cfg.cross_vendor_review)
                    if self.budget_reason and not ok:
                        # needs a review or revision the budget doesn't allow: pause, don't fail
                        attempt.update({"result": "paused: budget reached before review", "files_changed": len(files),
                                        "verify_exit": verify[0] if verify else None})
                        with self.lock:
                            t.attempts.append(attempt)
                            t.status = "pending"
                        self.say(f"{BADGE_WARN} {t.id} paused: budget reached before its review ({self.budget_reason})")
                        return
                    verdict = {"verdict": "approve" if ok else "revise",
                               "feedback": "" if ok else "Review unavailable and no sufficient independent verification.",
                               "summary": first_line(summary)}
                    why = "budget reached" if self.budget_reason else "lead review unavailable"
                    self.say(f"{BADGE_WARN} {why} for {t.id}; using the verify result instead")
                    if self.budget_reason and ok:
                        with self.lock:
                            t.review_note = "not reviewed by the lead (budget reached); accepted on its verify result"
            else:
                verdict = {"verdict": "approve", "feedback": "", "summary": first_line(summary)}

            approved = str(verdict.get("verdict", "")).strip().lower() == "approve"
            feedback = str(verdict.get("feedback") or "").strip()
            if outside:
                # out-of-scope edits were never reviewed: this attempt cannot be approved
                shown = ", ".join(outside[:5]) + (" ..." if len(outside) > 5 else "")
                self.say(f"{BADGE_WARN} {t.id} changed {len(outside)} file(s) outside its scope ({shown}); "
                         "it cannot be approved until they are undone")
                approved = False
                feedback = ("Undo your changes to files outside this task's scope: " + ", ".join(outside[:20])
                            + ". Only edit: " + ", ".join(t.owns) + "." + (" " + feedback if feedback else ""))
            if approved and self.cfg.approve_tasks:
                decision, user_note = self.approve_task_preview(t, agent, files, changes)
                if decision == "revise":
                    attempt.update({"result": "user requested revision", "feedback": user_note,
                                    "files_changed": len(files), "verify_exit": verify[0] if verify else None})
                    with self.lock:
                        t.attempts.append(attempt)
                    if revision < self.cfg.retries:
                        revision += 1
                        feedback = user_note
                        self.say(f"{BADGE_WARN} {t.id}: user requested changes -> {first_line(feedback, 200)}")
                        continue
                    with self.lock:
                        t.status = "failed"
                        t.review_note = "user requested revision but no retries remain: " + user_note
                        t.duration_sec = time.time() - start
                    return
                if decision == "reject":
                    attempt.update({"result": "rejected by user", "feedback": user_note,
                                    "files_changed": len(files), "verify_exit": verify[0] if verify else None})
                    with self.lock:
                        t.attempts.append(attempt)
                        t.status = "failed"
                        t.review_note = user_note
                        t.duration_sec = time.time() - start
                    return
            attempt.update({"result": "approved" if approved else "revise", "feedback": feedback,
                            "files_changed": len(files), "verify_exit": verify[0] if verify else None})
            if not approved and feedback:
                ledger.record_team_lesson(feedback, category=t.category, source=t.id, project=str(self.dir))
            with self.lock:
                t.attempts.append(attempt)

            if approved or revision >= self.cfg.retries:
                with self.lock:
                    t.agent_used = agent
                    t.summary = str(verdict.get("summary") or "") + ("\n" + summary if summary else "")
                    if not approved:
                        t.status = "failed"
                        t.review_note = f"Acceptance still unmet after {revision + 1} attempt(s): " + (
                            feedback or "acceptance criteria remain unmet")
                        if outside:
                            t.review_note += (" | NOT reviewed or checkpointed, still in your project (inspect with "
                                              "`git diff` / `git status`): " + ", ".join(outside[:20]))
                    t.duration_sec = time.time() - start
                if approved:
                    if workspace != self.dir:
                        t.commit = self._checkpoint_workspace(t, workspace, scope)
                    else:
                        t.commit = self.checkpoint(t, scope)
                    with self.lock:
                        t.status = "done"
                    if workspace != self.dir:
                        self._remove_task_workspace(t)
                    extra = c_dim(f" ({t.review_note})") if t.review_note else ""
                    self.say(f"{BADGE_SUCCESS} {c_bold(t.id)} {t.title} - by {c_cyan(agent)}"
                             f"{c_dim(' @' + t.commit) if t.commit else ''}{extra}")
                else:
                    if workspace == self.dir:
                        t.commit = self.checkpoint(t, scope, "[needs work]")
                    self.say(f"{BADGE_FAILED} {c_bold(t.id)} {t.title}: {t.review_note}")
                return

            revision += 1
            if agent != "human" and (regressions or behavior_problem):
                previous_builders = [a["agent"] for a in t.attempts if a.get("result") == "revise"]
                alternate = self._pick_worker(exclude=tried + previous_builders + ["human"])
                if alternate:
                    self.say(f"{BADGE_INFO} {t.id}: giving the failed checks to {c_cyan(alternate)} for revision")
                    agent = alternate
            self.say(f"{BADGE_WARN} {t.id}: revision needed -> {first_line(feedback, 200)}")

    # ---------------------------------------------------------------- final
    def final_check(self) -> Dict:
        can_add = self.round < self.cfg.max_rounds
        checks: List[Tuple[str, Tuple[int, str, str]]] = []
        for cmd in dict.fromkeys(t.verify for t in self.tasks if t.verify and t.status in ("done", "failed")):
            res = self.run_verify(Task(id="final", title="final", assignee="", instructions="", verify=cmd))
            if res is not None:
                checks.append((cmd, res))
        if checks:
            passed = sum(1 for _, r in checks if r[0] == 0)
            self.say(f"{BADGE_INFO} re-ran {len(checks)} verify command(s) on the finished project: {passed} passed")
        self.say(f"\n{BADGE_INFO} {c_magenta(self.lead)} is checking the whole project (round {self.round})...")
        data = self.ask_lead("final", build_final_prompt(
            self.goal, self.dir, self.tasks, self.workers, self.specs, self.round, can_add, self.cfg.max_tasks,
            checks, self.workflows), "complete", validate=lambda reply: workflows.final_reply_error(reply, self.workflows))
        reviewed = data is not None
        if data is None:
            data = {"complete": False, "summary": "Final review unavailable.", "how_to_run": "",
                    "remaining_issues": ["The integrated result still needs review."], "followup_tasks": []}
        data["complete"] = data.get("complete") is True
        issues = data.get("remaining_issues")
        data["remaining_issues"] = [i for i in issues if isinstance(i, str) and i.strip()] if isinstance(issues, list) else []
        data = enforce_completion_gate(data, self.tasks, checks)
        rows = workflows.assess(self.workflows, self.tasks, checks, data)
        data["workflow_results"] = rows
        gaps = [r for r in rows if r["status"] != "met"]
        for row in gaps:
            data["remaining_issues"].append("{id} {scenario} — {status}: {evidence}".format(**row))
        if data["remaining_issues"] or data.get("followup_tasks"):
            data["complete"] = False
        if gaps:
            if reviewed and can_add and not data.get("followup_tasks") and not self.budget_reason:
                have, missing = tool_report()
                prompt = build_plan_prompt(self.goal, self.dir, self.specs, self.workers, have, self.cfg.max_tasks,
                                           missing, protected_paths=self.protected_paths)
                prompt += ("\nPlan only the repairs for these observed workflow gaps. Preserve passing workflows "
                           "and existing checks. For a workflow without a runnable check, add a task that exercises "
                           "the scenario and names it in that task's instructions.\n" +
                           "\n".join(data["remaining_issues"]))
                repair = self.ask_lead("repair-plan", prompt, "tasks")
                data["followup_tasks"] = (repair or {}).get("tasks", [])
        if not can_add:
            data["followup_tasks"] = []
        return data

    # ---------------------------------------------------------------- report
    def write_report(self) -> Path:
        dur = int(time.time() - self.started_at)
        complete = bool(self.final.get("complete"))
        lines = [
            f"# OmniCLI Team Report - {self.project_name or self.dir.name}",
            "",
            f"- **Goal:** {self.goal}",
            f"- **Project directory:** `{self.dir}`",
            f"- **Lead:** {self.lead}   **Workers:** {', '.join(self.workers)}",
            f"- **Outcome:** {'COMPLETE' if complete else 'INCOMPLETE'} after {self.round} round(s), {dur // 60}m{dur % 60:02d}s",
            "",
        ]
        if self.plan_summary:
            lines += ["## Approach", self.plan_summary, ""]
        if self.final.get("summary"):
            lines += ["## What was built", str(self.final["summary"]), ""]
        if self.final.get("how_to_run"):
            lines += ["## How to run", "```", str(self.final["how_to_run"]), "```", ""]
        lines += ["## Tasks", "", "| Task | Worker | Status | Attempts | Commit | Notes |", "|---|---|---|---|---|---|"]
        for t in self.tasks:
            note = (t.review_note or first_line(t.summary)).replace("|", "/")[:120]
            lines.append(f"| {t.id} {t.title.replace('|', '/')} | {t.agent_used or t.assignee} | {t.status} | "
                         f"{len(t.attempts)} | {t.commit or '-'} | {note} |")
        if isinstance(self.final.get("workflow_results"), list) and self.final["workflow_results"]:
            lines += ["", "## User workflows", ""]
            for row in self.final["workflow_results"]:
                line = "- **{status}** · {scenario} — {evidence}".format(**row)
                if row["verify"]:
                    line += " (check: `{}`)".format(row["verify"])
                lines.append(line)
        issues = self.final.get("remaining_issues") or []
        if issues:
            lines += ["", "## Remaining issues"] + [f"- {i}" for i in issues]
        if self.down:
            lines += ["", "## Agents that dropped out"] + [f"- {k}: {v}" for k, v in self.down.items()]
        lines += ["", f"Full prompts and agent output: `{self.logs}`", ""]
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.report_path.write_text("\n".join(lines), encoding="utf-8")
        self._report_written = True
        return self.report_path

    def print_summary(self):
        if not self.verbose:
            return
        rows = []
        for t in self.tasks:
            st = {"done": c_green("DONE"), "failed": c_red("FAILED"), "skipped": c_dim("SKIPPED")}.get(t.status, t.status)
            rows.append([t.id, t.title[:50], c_cyan(t.agent_used or t.assignee), st, str(len(t.attempts)),
                         f"{t.duration_sec / 60:.1f}m", t.commit or "-"])
        print_table(["ID", "Task", "Worker", "Status", "Tries", "Time", "Commit"], rows, title="Team results")
        complete = bool(self.final.get("complete"))
        head = c_green("PROJECT COMPLETE") if complete else c_yellow("PROJECT NOT FULLY COMPLETE")
        self.say(f"{c_bold(head)} - {self.final.get('summary', '')}")
        if self.final.get("how_to_run"):
            self.say(f"\n{c_bold('How to run:')}\n{self.final['how_to_run']}")
        for i in self.final.get("remaining_issues") or []:
            self.say(f"  {c_yellow('•')} {i}")
        self.say(f"\n{c_dim('Report: ' + str(self.report_path))}")

    # ---------------------------------------------------------------- main
    def run(self) -> int:
        """0 complete/planned, 1 could not start or plan, 2 incomplete, 3 budget reached, 130 Ctrl-C."""
        self._run_started = time.time()
        self.dir.mkdir(parents=True, exist_ok=True)
        resumed = False
        if self.cfg.resume:
            if not self.load_state():
                self.say(f"{BADGE_FAILED} Nothing to resume in {self.dir} (no .omnicli/team/state.json).")
                return 1
            resumed = True
            self.say(f"{BADGE_INFO} Resuming: {c_bold(self.goal)}")
        elif self.state_path.exists():
            self.say(f"{BADGE_WARN} {self.dir} already has a team run. Use --resume to continue it; starting a new plan.")

        if not self.select_team():
            return 1

        try:
            if not resumed or not self.tasks:
                self.phase = "planning"
                self.save_state()  # visible to `omnicli status` / `runs` while the lead plans
                if not self.plan():
                    if self.budget_reason:
                        return self._stop_for_budget()
                    self.phase = "failed"
                    if self.state_path.exists() or self.tasks:
                        self.save_state()
                    return 1
                self.phase = "planned"
                self.save_state()
                self.show_plan()
                if self.cfg.plan_only:
                    self.say(f"\n{BADGE_INFO} Plan saved. Run it with: omnicli resume {shlex.quote(str(self.dir))}")
                    return 0
                if not self.confirm():
                    self.say(f"{BADGE_INFO} Not started. The plan is saved; run `omnicli resume {shlex.quote(str(self.dir))}` to execute it.")
                    return 0
            else:
                self.show_plan(title="Resumed plan")
                if self.cfg.confirm_saved_plan and not self.confirm():
                    self.say(f"{BADGE_INFO} Not started. The plan is saved; run `omnicli resume {shlex.quote(str(self.dir))}` when ready.")
                    return 0
                if self.cfg.plan_only:
                    return 0

            conflicts = self._refresh_protected_paths()
            if conflicts:
                self.phase = "planned"
                self.save_state()
                self.say(f"{BADGE_FAILED} Files changed while the plan was waiting for approval, and the plan now overlaps them: "
                         + "; ".join(conflicts))
                self.say(c_dim("No tasks were started. Re-plan after the other edits finish."))
                return 1
            if self.protected_paths:
                if not self.cfg.use_git:
                    self.phase = "planned"
                    self.save_state()
                    self.say(f"{BADGE_FAILED} Active edits are protected. Enable Git checkpoints to run workers in isolated worktrees, or wait until those edits finish.")
                    return 1
                self.cfg.use_worktrees = True
            self.setup_git()  # after approval: nothing is created or committed for a plan you decline
            if self.cfg.use_worktrees and not self.git:
                self.phase = "planned"
                self.save_state()
                self.say(f"{BADGE_FAILED} Could not create isolated Git worktrees. The project was left unchanged.")
                return 1
            self.phase = "running"
            self.save_state()
            while True:
                todo = sum(1 for t in self.tasks if t.status == "pending")
                header = f"=== Round {self.round}: {todo} task(s) to do ==="
                self.say(f"\n{c_bold(c_magenta(header))}")
                self.execute()
                if self.stopping:
                    break
                self.budget_reason = self.budget_reason or self._budget_hit()
                if self.budget_reason:
                    return self._stop_for_budget()
                final = self.final_check()
                if self.budget_reason:  # the final check itself was cut short; redo it on resume
                    return self._stop_for_budget()
                self.final = final
                self.save_state()
                followups = self.final.get("followup_tasks") or []
                if self.final.get("complete") or not followups or self.round >= self.cfg.max_rounds:
                    break
                self.round += 1
                new = normalize_tasks(followups, [w for w in self.workers if w not in self.down] or self.workers,
                                      existing_ids=[t.id for t in self.tasks], prefix=f"r{self.round}-t",
                                      max_tasks=self.cfg.max_tasks, round_no=self.round)
                if not new:
                    break
                conflicts = protected_scope_conflicts(new, self.protected_paths)
                if conflicts:
                    issue = ("Follow-up work was not started because it overlaps files already being edited: "
                             + "; ".join(conflicts))
                    self.final["complete"] = False
                    self.final["remaining_issues"] = list(self.final.get("remaining_issues") or []) + [issue]
                    self.say(f"{BADGE_WARN} {issue}")
                    self.save_state()
                    break
                self.tasks.extend(new)
                self.save_state()
                self.say(f"\n{BADGE_INFO} The lead found more to do: {len(new)} follow-up task(s).")
                self.show_plan(new, title=f"Round {self.round} follow-ups")
        except KeyboardInterrupt:
            self.stopping = True
            ag.kill_all()
            with self.lock:
                for t in self.tasks:
                    if t.status == "running":
                        t.status = "pending"
                self.phase = "stopped"
                self.save_state()
            self.say(f"\n{BADGE_WARN} Stopped. Progress is saved; continue with: omnicli resume {shlex.quote(str(self.dir))}")
            return 130

        self.phase = "complete" if self.final.get("complete") else "incomplete"
        self.save_state()
        self.write_report()
        self.print_summary()
        self._notify("Team run finished", "{}: {}".format(self.project_name, self.phase))
        return 0 if self.final.get("complete") else 2
