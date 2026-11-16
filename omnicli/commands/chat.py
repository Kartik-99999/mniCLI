"""
`omnicli chat` (and plain `omnicli` in a terminal) - talk to OmniCLI like a chatbot.

    you › build a todo app with FastAPI and SQLite
    you › why did t2 fail?
    you › add dark mode to the frontend
    you › /status

Plain text goes to the lead AI (read-only): it answers questions about the current
project and, when you ask for code to be built or changed, proposes a team run that you
confirm before anything starts. `/commands` run instantly without an AI call, and
`!cmd` runs a shell command in the project folder.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from omnicli import paths, runs, settings
from omnicli.ui import (
    BADGE_FAILED,
    BADGE_INFO,
    BADGE_WARN,
    c_bold,
    c_cyan,
    c_dim,
    c_green,
    c_magenta,
    c_yellow,
)

NAMES = ["chat"]

HISTORY_TURNS = 8          # conversation turns sent to the AI each message
TURN_CHARS = 1500          # each remembered turn is trimmed to this
ACTION_RE = re.compile(r"^\s*`*\s*ACTION\s*:\s*(\{.*\})\s*`*\s*$", re.I)

# Commands the AI may run for you. Anything that changes things still asks you itself
# (undo/resume/review --fix confirm), and -y/--yes is always stripped from AI proposals.
AI_ALLOWED_COMMANDS = {"status", "runs", "usage", "logs", "export", "doctor", "agents", "config",
                       "undo", "rewind", "continue", "resume", "review"}
AI_CONFIG_SUBCOMMANDS = {"list", "get", "path", None}
# Commands that take -d DIR; chat fills in the current project automatically.
DIR_COMMANDS = {"status", "logs", "export", "undo", "rewind", "review", "usage"}
# Slash commands passed straight through to the matching `omnicli <command>`.
PASSTHROUGH = {"status", "runs", "usage", "logs", "undo", "rewind", "export", "review", "doctor",
               "agents", "config", "continue", "resume", "init", "models"}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
# a dropped file arrives as  /path/with\ escaped\ spaces.png  or  '/quoted path.png'  or  "/quoted.png"
_PATH_RE = re.compile(r"""'([^'\n]+)'|"([^"\n]+)"|((?:~|/)(?:\\.|[^\s\\'"])+)""")
_IMAGE_REF_RE = re.compile(r"\[Image #(\d+)\]")


def find_image_paths(line: str) -> List[Tuple[int, int, str]]:
    """(start, end, real path) for every existing image file path in the typed/dropped text."""
    found = []
    for m in _PATH_RE.finditer(line):
        raw = m.group(1) or m.group(2)
        cand = raw if raw is not None else re.sub(r"\\(.)", r"\1", m.group(3))
        p = Path(cand).expanduser()
        try:
            if p.suffix.lower() in IMAGE_EXTS and p.is_file():
                found.append((m.start(), m.end(), str(p.resolve())))
        except OSError:
            continue
    return found


LOCAL_COMMANDS = {"help", "exit", "quit", "clear", "project", "new", "build", "lead", "workers", "model",
                  "effort", "team", "setup"}


def suggest(word: str, choices) -> "Optional[str]":
    """Closest known command for a typo (e.g. 'ussage' -> 'usage'), or None."""
    import difflib
    hits = difflib.get_close_matches(word.lower(), sorted(choices), n=1, cutoff=0.7)
    return hits[0] if hits else None


HELP = """\
Talk in plain English - the lead AI answers, and asks before the team builds anything.
  build a todo app with FastAPI and SQLite      why did t2 fail?      add tests for the API

/build <goal>        start the team now (no AI call to decide)
/status  /runs  /logs [task]  /usage  /export  /undo <task>  /continue  /review  /doctor  /agents  /config
/project [dir]       show or switch the project folder (/new <dir> for a folder that doesn't exist yet)
/team                show who leads, who works, and each AI's model/effort
/lead [agent]        choose the lead AI (plans, reviews, and is who you talk to here)
/workers             choose which AIs work on tasks
/model [agent]       choose an AI's model from its own list     /effort [agent]  its reasoning effort
/models              list every AI's available models           /setup           guided setup of all of it
/clear               forget the conversation so far
!<command>           run a shell command in the project folder, e.g. !ls  !git log --oneline
/exit                quit (or Ctrl-D)"""


# --------------------------------------------------------------------------- pure helpers

def parse_reply(text: str) -> Tuple[str, Optional[Dict]]:
    """Split the AI's answer into (visible reply, action or None). The action is the last
    `ACTION: {...}` line; invalid JSON is ignored (the reply is still shown)."""
    lines = (text or "").strip().splitlines()
    action, keep = None, []
    for line in lines:
        m = ACTION_RE.match(line)
        if m:
            try:
                obj = json.loads(m.group(1))
                if isinstance(obj, dict) and obj.get("type") in ("build", "command"):
                    action = obj
                    continue
            except ValueError:
                pass
        keep.append(line)
    reply = "\n".join(keep).strip()
    reply = re.sub(r"\n?```\s*$", "", reply).strip() if reply.endswith("```") and reply.count("```") % 2 else reply
    return reply, action


def sanitize_command(args: List[str], project: Optional[Path]) -> Tuple[Optional[List[str]], str]:
    """Validate an AI-proposed command. Returns (argv or None, reason if refused)."""
    if not args or not isinstance(args, list):
        return None, "empty command"
    args = [str(a) for a in args if str(a).strip()]
    if args and args[0] == "omnicli":
        args = args[1:]
    if not args or args[0] not in AI_ALLOWED_COMMANDS:
        return None, f"'{args[0] if args else ''}' isn't something I can run from chat"
    if args[0] == "config":
        sub = args[1] if len(args) > 1 else None
        if sub not in AI_CONFIG_SUBCOMMANDS:
            return None, "changing settings needs you to type /config set ... yourself"
    args = [a for a in args if a not in ("-y", "--yes")]  # you always confirm destructive steps
    if project is not None and args[0] in DIR_COMMANDS and not ({"-d", "--dir", "--run"} & set(args)):
        args += ["-d", str(project)]
    return args, ""


def summarize_state(project: Optional[Path]) -> str:
    if project is None:
        return "No project folder selected yet."
    state = runs.load_state(project)
    if not state:
        return f"{project} has no OmniCLI team run yet."
    tasks = state.get("tasks") or []
    done = sum(1 for t in tasks if t.get("status") == "done")
    lines = [f"Team run goal: {state.get('goal', '')}",
             f"Phase: {state.get('phase', '?')} · {done}/{len(tasks)} tasks done · lead {state.get('lead', '?')}"]
    for t in tasks[:20]:
        note = (t.get("review_note") or "").strip()
        lines.append(f"- {t.get('id')} [{t.get('status')}] ({t.get('agent_used') or t.get('assignee')}) "
                     f"{t.get('title', '')}" + (f" - {note[:120]}" if note else ""))
    final = state.get("final") or {}
    if final.get("summary"):
        lines.append(f"Lead's final summary: {str(final['summary'])[:600]}")
    for issue in (final.get("remaining_issues") or [])[:5]:
        lines.append(f"Remaining issue: {str(issue)[:200]}")
    return "\n".join(lines)


def build_chat_prompt(message: str, history: List[Tuple[str, str]], project: Optional[Path],
                      workers_block: str, attachments: Optional[Dict[int, str]] = None) -> str:
    from omnicli.conductor import list_tree

    convo = "\n".join(f"{'User' if role == 'user' else 'You' if role == 'assistant' else 'OmniCLI'}: {text}"
                      for role, text in history[-HISTORY_TURNS * 2:]) or "(this is the first message)"
    tree = list_tree(project, max_entries=80) if project is not None and project.is_dir() else "(no folder)"
    images = ""
    if attachments:
        images = ("\nIMAGES THE USER ATTACHED (open the file to look at it; refer to them as [Image #N]):\n"
                  + "\n".join(f"[Image #{n}] = {path}" for n, path in sorted(attachments.items())) + "\n"
                  + "When a build should follow an image, mention it as [Image #N] in the goal - OmniCLI passes "
                    "the image to the team.\n")
    return f"""[OmniCLI chat]
You are OmniCLI's assistant, chatting with the user in their terminal. OmniCLI runs a TEAM of AI
coding CLIs (listed below) that can build and change code. You yourself are READ-ONLY: never create,
edit or delete files and never run commands that change anything. You may read files in the project
folder to answer questions.

CURRENT PROJECT FOLDER: {project or "(none - ask what to build or where)"}
{summarize_state(project)}

FILES:
{tree}

TEAM (AI CLIs that are ready):
{workers_block}
{images}
WHAT YOU CAN DO
1. Answer and discuss - questions about the project, the team's work, failures, next steps. Be concise.
2. Start the team building - when the user asks to create, build, add, change or fix code, reply with
   one short sentence about what you'll have the team do, then on the LAST line:
   ACTION: {{"type": "build", "goal": "<precise, self-contained goal: what to build or change, language/framework, tests>", "dir": "<folder, or empty to use the current project>"}}
   For a change to an existing project, write the goal as a change ("In the existing todo app, add ...").
   For a brand-new project with no current folder, suggest a short folder name in "dir" (e.g. "todo-app").
3. Run an OmniCLI command for them - last line:
   ACTION: {{"type": "command", "args": ["status"]}}
   Allowed: status, runs, usage, logs [TASK], export [--format html], doctor, agents, config list|get KEY,
   undo TASK (the user will be asked to confirm), continue, resume, review [focus text].
Use at most ONE ACTION line, only when the user clearly wants that. Otherwise just answer.

CONVERSATION SO FAR:
{convo}

User: {message}
"""


def _slug(text: str) -> str:
    stop = {"a", "an", "the", "with", "and", "for", "to", "of", "in", "on", "build", "create", "make", "me",
            "please", "app", "using"}
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in stop][:4]
    return "-".join(words) or "project"


# --------------------------------------------------------------------------- spinner

class _Spinner:
    def __init__(self, label: str, enabled: bool):
        self.label, self.enabled = label, enabled
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None

    def __enter__(self):
        if self.enabled:
            self._t = threading.Thread(target=self._spin, daemon=True)
            self._t.start()
        return self

    def _spin(self):
        frames, start, i = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", time.time(), 0
        while not self._stop.wait(0.12):
            sys.stdout.write(f"\r{c_dim(frames[i % len(frames)] + ' ' + self.label + f' {int(time.time() - start)}s')}")
            sys.stdout.flush()
            i += 1

    def __exit__(self, *exc):
        self._stop.set()
        if self._t:
            self._t.join()
            sys.stdout.write("\r\x1b[2K")
            sys.stdout.flush()
        return False


# --------------------------------------------------------------------------- session

class ChatSession:
    def __init__(self, project: Optional[Path] = None, lead: Optional[str] = None,
                 input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print,
                 specs: Optional[Dict] = None, health: Optional[Dict] = None,
                 run_cli: Optional[Callable[[List[str]], int]] = None,
                 make_conductor: Optional[Callable] = None, interactive: Optional[bool] = None):
        from omnicli import agents as ag
        self.ag = ag
        self.project = Path(project).expanduser().resolve() if project is not None else None
        self.input_fn = input_fn
        self.out = out
        self.specs = specs if specs is not None else ag.load_agent_specs()
        self._health = health
        self.run_cli = run_cli or self._default_run_cli
        self.make_conductor = make_conductor or self._default_conductor
        self.interactive = sys.stdout.isatty() if interactive is None else interactive
        self.history: List[Tuple[str, str]] = []
        self.attachments: Dict[int, str] = {}   # [Image #N] -> stored copy of the image
        self.last_answerer: Optional[str] = None
        self.requested_lead = lead
        self.lead: Optional[str] = None
        self.ready: List[str] = []
        self.down: Dict[str, str] = {}

    # ---------------------------------------------------------- plumbing
    @staticmethod
    def _default_run_cli(argv: List[str]) -> int:
        from omnicli import cli
        try:
            return int(cli.main(["--no-banner"] + argv) or 0)
        except SystemExit as ex:  # argparse errors / --help
            return int(ex.code or 0) if isinstance(ex.code, int) else 1

    @staticmethod
    def _default_conductor(cfg):
        from omnicli.conductor import Conductor
        return Conductor(cfg)

    def _ask(self, question: str, default_yes: bool = True) -> bool:
        try:
            ans = self.input_fn(c_bold(c_yellow(question + (" [Y/n] " if default_yes else " [y/N] ")))).strip().lower()
        except (EOFError, KeyboardInterrupt):
            self.out("")
            return False
        return ans in ("y", "yes") or (default_yes and ans == "")

    def select_lead(self) -> bool:
        if self._health is None:
            self.out(c_dim("Checking which AIs are ready..."))
            self._health = self.ag.check_health(self.specs)
        order = [n for n in self.ag.PREFERRED_LEAD_ORDER if n in self.specs] + \
                [n for n in self.specs if n not in self.ag.PREFERRED_LEAD_ORDER]
        self.ready = [n for n in order if self._health.get(n, {}).get("ok") and n not in self.down]
        want = self.requested_lead or settings.get("lead")
        if want and want in self.ready:
            self.lead = want
        elif self.ready:
            if want and want not in self.ready:
                self.out(f"{BADGE_WARN} {want} isn't ready ({self._health.get(want, {}).get('reason', 'unknown agent')}); "
                         f"talking to {self.ready[0]} instead.")
            self.lead = self.ready[0]
        else:
            self.lead = None
        return self.lead is not None

    def _workers_block(self) -> str:
        return "\n".join(f"- {w}: {self.specs[w].get('name', w)} - {self.specs[w].get('strengths', '')}"
                         for w in self.ready) or "(none ready)"

    # ---------------------------------------------------------- AI
    def ask_ai(self, message: str, images: Optional[List[str]] = None) -> Tuple[Optional[str], Optional[Dict]]:
        images = list(images or [])
        prompt = build_chat_prompt(message, self.history, self.project, self._workers_block(), self.attachments)
        cwd = str(self.project) if self.project is not None and self.project.is_dir() else str(Path.cwd())
        log_dir = paths.omni_home() / "chat" / "logs"
        timeout = float(settings.get("lead_timeout") or 10) * 60
        candidates = [n for n in ([self.lead] + [r for r in self.ready if r != self.lead]) if n and n not in self.down]
        if images:
            capable = [n for n in candidates if self.specs[n].get("sees_images")]
            if not capable:
                self.out(f"{BADGE_WARN} None of the ready AIs can view images; sending only the file path.")
            else:
                if capable[0] != self.lead:
                    self.out(c_dim(f"{self.lead} can't view images headlessly, so {capable[0]} will answer this one."))
                candidates = capable
        for name in candidates:
            if name in self.down:
                continue
            spec = self.ag.with_saved_options(self.specs[name], name)
            if images:
                spec["_images"] = images
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            with _Spinner(f"{name} is thinking...", self.interactive):
                run = self.ag.run_agent(spec, prompt, "readonly", cwd, timeout=timeout,
                                        log_path=log_dir / f"{stamp}-{name}.md",
                                        meta={"kind": "chat", "run_dir": str(self.project or "")})
            if run.ok:
                self.last_answerer = name
                return parse_reply(run.output)
            reason = run.failure_reason()
            self.out(f"{BADGE_WARN} {name} couldn't answer: {reason}")
            if run.auth_problem or getattr(run, "quota_problem", False) or run.error or run.timed_out:
                self.down[name] = reason
                if name == self.lead:
                    self.select_lead()
            nxt = next((c for c in candidates if c not in self.down and c != name
                        and candidates.index(c) > candidates.index(name)), None)
            if nxt:
                self.out(c_dim(f"Asking {nxt} instead..."))
        return None, None

    # ---------------------------------------------------------- images
    def take_images(self, line: str) -> Tuple[str, List[str]]:
        """Replace dropped image paths with [Image #N]; keep a safe copy of each (macOS deletes
        screenshot temp files quickly). Returns (text to show/send, stored image paths)."""
        import shutil
        hits = find_image_paths(line)
        if not hits:
            return line, []
        store = paths.omni_home() / "chat" / "attachments"
        store.mkdir(parents=True, exist_ok=True)
        parts, stored, last = [], [], 0
        for start, end, src in hits:
            n = len(self.attachments) + 1
            dest = store / f"{datetime.datetime.now():%Y%m%d-%H%M%S}-{n}{Path(src).suffix.lower()}"
            try:
                shutil.copy2(src, dest)
            except OSError:
                dest = Path(src)
            self.attachments[n] = str(dest)
            stored.append(str(dest))
            parts += [line[last:start], f"[Image #{n}]"]
            last = end
        parts.append(line[last:])
        text = re.sub(r"[ \t]{2,}", " ", "".join(parts)).strip()
        return text, stored

    def images_in(self, text: str) -> List[str]:
        """Stored paths of every [Image #N] mentioned in text."""
        return [self.attachments[int(n)] for n in dict.fromkeys(_IMAGE_REF_RE.findall(text or ""))
                if int(n) in self.attachments]

    def _redraw_input(self, raw: str, shown: str) -> None:
        """Replace the long pasted path the terminal echoed with the short [Image #N] form."""
        if not self.interactive:
            return
        import shutil as _sh
        cols = max(20, _sh.get_terminal_size((100, 20)).columns)
        rows = max(1, -(-(len("you › ") + len(raw)) // cols))
        sys.stdout.write(f"\x1b[{rows}F\x1b[J")
        sys.stdout.flush()
        self.out(c_green(c_bold("you › ")) + shown)

    # ---------------------------------------------------------- actions
    def run_command(self, argv: List[str]) -> int:
        self.out(c_dim("$ omnicli " + " ".join(shlex.quote(a) for a in argv)))
        rc = self.run_cli(argv)
        self.history.append(("system", f"ran `omnicli {' '.join(argv)}` (exit {rc})"))
        return rc

    def resolve_build_dir(self, dir_hint: str) -> Path:
        """Where a build goes. New projects started from your home folder go under ~/omnicli-projects."""
        home = Path.home().resolve()
        base = home / "omnicli-projects" if Path.cwd().resolve() == home else Path.cwd().resolve()
        hint = (dir_hint or "").strip()
        if not hint:
            return self.project if self.project is not None else (base / "project").resolve()
        p = Path(hint).expanduser()
        if p.is_absolute():
            return p.resolve()
        if self.project is not None and hint.rstrip("/") == self.project.name:
            return self.project
        return (base / p).resolve()

    def start_build(self, goal: str, dir_hint: str = "", images: Optional[List[str]] = None) -> int:
        goal = (goal or "").strip()
        if not goal:
            self.out(f"{BADGE_WARN} Tell me what to build, e.g. /build a todo app with FastAPI and tests")
            return 1
        target = self.resolve_build_dir(dir_hint or ("" if self.project is not None else _slug(goal)))
        if target in (Path.home().resolve(), Path("/").resolve()):
            self.out(f"{BADGE_FAILED} I won't build directly in {target}. Pick a folder with /new <dir>.")
            return 1
        state = runs.load_state(target)
        resume = False
        if state:
            phase = state.get("phase", "")
            if phase == "running" and runs.pid_alive(state.get("pid", 0)):
                self.out(f"{BADGE_WARN} A team is already working in {target}. Watch it with /status.")
                return 1
            if phase in ("planned", "stopped", "incomplete", "failed", "running", "planning"):
                self.out(f"{BADGE_INFO} {target.name} has an unfinished run: {c_bold(str(state.get('goal', ''))[:100])}")
                if self._ask("Continue that run instead of starting a new one?"):
                    resume = True
        self.out("")
        self.out(f"  {c_bold('Goal:')}   {goal if not resume else state.get('goal', '')}")
        self.out(f"  {c_bold('Folder:')} {target}")
        if not self._ask("Start the team?"):
            self.out(c_dim("Okay, not started."))
            return 0
        if state and not resume:
            backup = target / ".omnicli" / "team" / f"state.prev-{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
            try:
                (target / ".omnicli" / "team" / "state.json").replace(backup)
            except Exception:
                pass
        from omnicli.teamopts import build_team_config
        images = list(dict.fromkeys((images or []) + self.images_in(goal)))
        if images and not resume:
            goal += "\n\nReference images from the user (open these files to look at them):\n" + "\n".join(
                f"- [Image #{n}] {pth}" for n, pth in sorted(self.attachments.items()) if pth in images)
        cfg = build_team_config(argparse.Namespace(), goal="" if resume else goal, project_dir=target, resume=resume)
        if images:
            cfg.images = images
            self.out(c_dim(f"  The team gets {len(images)} reference image(s)."))
        rc = int(self.make_conductor(cfg).run() or 0)
        self.project = target
        after = runs.load_state(target) or {}
        tasks = after.get("tasks") or []
        done = sum(1 for t in tasks if t.get("status") == "done")
        self.history.append(("system", f"the team ran in {target}: phase {after.get('phase', '?')}, "
                                       f"{done}/{len(tasks)} tasks done (exit {rc})"))
        self.out(c_dim(f"\nBack in chat · project {target}"))
        return rc

    def do_action(self, action: Dict) -> None:
        if action.get("type") == "build":
            self.start_build(str(action.get("goal") or ""), str(action.get("dir") or ""),
                             images=getattr(self, "_current_images", []))
        elif action.get("type") == "command":
            argv, why = sanitize_command(action.get("args") or [], self.project)
            if argv is None:
                self.out(f"{BADGE_WARN} Skipped the suggested command: {why}.")
                return
            self.run_command(argv)

    # ---------------------------------------------------------- input handling
    def set_project(self, arg: str, must_exist: bool) -> None:
        if not arg:
            self.out(f"Project: {self.project or '(none)'}")
            self.out(c_dim("  " + summarize_state(self.project).replace("\n", "\n  ")))
            return
        p = Path(arg).expanduser()
        p = (p if p.is_absolute() else Path.cwd() / p).resolve()
        if must_exist and not p.is_dir():
            ref = runs.find_run(arg)
            if ref:
                p = Path(ref["dir"])
            else:
                self.out(f"{BADGE_WARN} {p} doesn't exist. Use /new {arg} to start a project there.")
                return
        if p in (Path.home().resolve(), Path("/").resolve()):
            self.out(f"{BADGE_WARN} Pick a project folder, not {p}.")
            return
        self.project = p
        self.out(f"{BADGE_INFO} Project: {c_bold(str(p))}" + ("" if p.exists() else c_dim("  (will be created)")))

    def handle(self, line: str) -> bool:
        """Process one input line. Returns False when the session should end."""
        line = line.strip()
        if not line:
            return True
        if line.startswith("!"):
            cmd = line[1:].strip()
            if cmd:
                cwd = str(self.project) if self.project is not None and self.project.is_dir() else None
                subprocess.run(cmd, shell=True, cwd=cwd)
            return True
        raw = line
        line, images = self.take_images(line)
        self._current_images = images
        if images:
            self._redraw_input(raw, line)
            self.out(c_dim(f"  attached {len(images)} image(s)"))
        if line.startswith("/"):
            return self.slash(line[1:])
        self.history.append(("user", line[:TURN_CHARS]))
        if not self.lead and not self.select_lead():
            self.out(f"{BADGE_FAILED} No AI is ready to chat. Check with /agents or /doctor.")
            return True
        reply, action = self.ask_ai(line, images)
        if reply is None and action is None:
            self.out(c_dim("No answer this time - try again, or use a /command."))
            return True
        if reply:
            self.out(f"{c_magenta(c_bold(self.last_answerer or self.lead or 'omnicli'))} › {reply}")
            self.history.append(("assistant", reply[:TURN_CHARS]))
        if action:
            self.do_action(action)
        return True

    def slash(self, rest: str) -> bool:
        try:
            parts = shlex.split(rest)
        except ValueError:
            parts = rest.split()
        if not parts:
            self.out(HELP)
            return True
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("exit", "quit", "q", "bye"):
            return False
        if cmd in ("help", "h", "?"):
            self.out(HELP)
        elif cmd == "clear":
            self.history.clear()
            self.out(c_dim("Conversation cleared."))
        elif cmd in ("project", "cd"):
            self.set_project(" ".join(args), must_exist=True)
        elif cmd == "new":
            if not args:
                self.out(f"{BADGE_WARN} Usage: /new <folder>")
            else:
                self.set_project(" ".join(args), must_exist=False)
        elif cmd in ("lead", "workers", "model", "effort", "team", "setup"):
            self.team_command(cmd, args)
        elif cmd == "build":
            self.start_build(" ".join(args), images=getattr(self, "_current_images", []))
        elif cmd in PASSTHROUGH:
            argv = [cmd] + args
            if self.project is not None and cmd in DIR_COMMANDS and not ({"-d", "--dir", "--run"} & set(args)):
                argv += ["-d", str(self.project)]
            self.run_command(argv)
        else:
            guess = suggest(cmd, LOCAL_COMMANDS | PASSTHROUGH)
            if guess and self._ask(f"Unknown command /{cmd}. Did you mean /{guess}?"):
                return self.slash(" ".join([guess] + [shlex.quote(a) for a in args]))
            if not guess:
                self.out(f"{BADGE_WARN} Unknown command /{cmd}. Type /help.")
        return True

    # ---------------------------------------------------------- team choices
    def team_command(self, cmd: str, args: List[str]) -> None:
        from omnicli.commands import models_cmd as mc
        from omnicli.picker import CANCEL, choose_one
        if self.lead is None or not self.ready:
            self.select_lead()
        ready = self.ready
        if cmd == "team":
            for line in mc.describe_team(self.specs, ready, self._health or {}):
                self.out("  " + line)
            self.out(c_dim(f"  You're talking to {self.lead or '(no AI ready)'} in this chat."))
            return
        if cmd == "setup":
            mc.run_setup(self.specs, ready, self._health or {}, input_fn=self.input_fn, out=self.out)
            self.requested_lead = None
            self.select_lead()
            return
        if cmd == "lead":
            if args:
                if args[0] not in ready:
                    self.out(f"{BADGE_WARN} {args[0]} isn't ready. Ready: {', '.join(ready) or 'none'}")
                    return
                from omnicli import settings as st
                st.set_value("lead", args[0])
                choice = args[0]
                self.out(f"  lead = {args[0]} (saved)")
            else:
                choice = mc.pick_lead(self.specs, ready, self.input_fn, self.out)
            if isinstance(choice, str):
                self.requested_lead = choice
                self.select_lead()
                self.out(f"Now talking to {c_bold(self.lead)} - it will also lead team builds.")
            return
        if cmd == "workers":
            mc.pick_workers(self.specs, ready, self.input_fn, self.out)
            return
        # model / effort: which agent?
        name = args[0] if args else None
        if name is None:
            key = "model_args" if cmd == "model" else "effort_args"
            candidates = [(n, self.specs[n].get("name", n)) for n in ready if self.specs[n].get(key)]
            if not candidates:
                self.out(f"{BADGE_WARN} No ready AI supports choosing a {cmd}.")
                return
            name = choose_one(f"Choose the {cmd} for which AI?", candidates, current=self.lead,
                              input_fn=self.input_fn, out=self.out)
            if name is CANCEL:
                return
            name = name or self.lead
        if name not in self.specs:
            self.out(f"{BADGE_WARN} Unknown AI '{name}'. Known: {', '.join(self.specs)}")
            return
        if cmd == "model":
            mc.pick_model(name, self.specs[name], self.input_fn, self.out)
        else:
            mc.pick_effort(name, self.specs[name], self.input_fn, self.out)

    # ---------------------------------------------------------- loop
    def welcome(self) -> None:
        state_line = ""
        if self.project is not None:
            st = runs.load_state(self.project)
            if st:
                tasks = st.get("tasks") or []
                done = sum(1 for t in tasks if t.get("status") == "done")
                state_line = c_dim(f"  ({st.get('phase', '?')}, {done}/{len(tasks)} tasks)")
        self.out(f"{c_bold('OmniCLI chat')} · talking to {c_magenta(self.lead or '(no AI ready)')} · "
                 f"project {c_cyan(str(self.project) if self.project else '(none yet)')}{state_line}")
        self.out(c_dim("Type what you want in plain English, e.g. \"build a todo app with FastAPI and tests\" "
                       "or \"why did t2 fail?\""))
        self.out(c_dim("/help for commands · !cmd runs a shell command · /exit or Ctrl-D to quit\n"))

    def loop(self) -> int:
        _enable_history()
        self.select_lead()
        self.welcome()
        while True:
            try:
                line = self.input_fn(c_green(c_bold("you › ")))
            except EOFError:
                self.out("")
                break
            except KeyboardInterrupt:
                self.out(c_dim("\n(Ctrl-C) - type /exit or press Ctrl-D to quit"))
                continue
            try:
                if not self.handle(line):
                    break
            except KeyboardInterrupt:
                self.out(c_dim("\nInterrupted - back in chat."))
        self.out(c_dim("Bye!"))
        return 0


def _enable_history() -> None:
    try:
        import atexit
        import readline
        hist = paths.omni_home() / "chat_history"
        hist.parent.mkdir(parents=True, exist_ok=True)
        try:
            readline.read_history_file(str(hist))
        except Exception:
            pass
        readline.set_history_length(1000)
        atexit.register(lambda: _save_history(readline, hist))
    except Exception:
        pass


def _save_history(readline, hist: Path) -> None:
    try:
        readline.write_history_file(str(hist))
    except Exception:
        pass


def default_project(cwd: Optional[Path] = None) -> Optional[Path]:
    """The folder you launched from - unless that's your home or / (then: none yet)."""
    cwd = (cwd or Path.cwd()).resolve()
    if cwd in (Path.home().resolve(), Path("/").resolve()):
        return None
    return cwd


# --------------------------------------------------------------------------- CLI

def add_parser(subparsers) -> None:
    p = subparsers.add_parser("chat", help="Talk to OmniCLI like a chatbot (also: just run `omnicli`)")
    p.add_argument("-d", "--dir", help="Project folder (default: the current folder unless it's your home)")
    p.add_argument("--lead", help="Which AI to talk to (default: your `lead` setting, else the best ready one)")
    p.set_defaults(func=run)


def run(args) -> int:
    project = None
    if getattr(args, "dir", None):
        project = Path(args.dir).expanduser().resolve()
    else:
        project = default_project()
    return ChatSession(project=project, lead=getattr(args, "lead", None)).loop()
