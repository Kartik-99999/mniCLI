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
ACTION_RE = re.compile(r"^\s*[`*_]*ACTION[`*_]*\s*:[`*_]*\s*[`*_]*(\{.*\})[`*_]*\s*$", re.I)

# Commands the AI may run for you. Anything that changes things still asks you itself
# (undo/resume/review --fix confirm), and -y/--yes is always stripped from AI proposals.
# What the AI may run for you, as a strict schema: {command: ({flag: value_rule}, max positional args)}.
# value_rule is None for a plain switch, or a regex the flag's value must fully match. Anything not
# listed (other flags, -y/--yes, -o/--output, --add-dir, --fix, combined short flags like -qy, "--x=y"
# forms) is refused, so text the AI reads (e.g. a hostile file in the project) can't smuggle options in.
_WORD = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
AI_COMMAND_SCHEMA = {
    "status":   ({"--json": None}, 1),
    "runs":     ({"--all": None, "--json": None}, 0),
    "usage":    ({"--days": r"\d{1,4}", "--since": r"\d{4}-\d{2}-\d{2}", "--by": r"agent|kind|run|day",
                  "--agent": _WORD, "--json": None}, 0),
    "logs":     ({"--agent": _WORD, "--lead": None, "--prompt": None, "--stderr": None, "--list": None,
                  "--tail": r"\d{1,5}", "--json": None}, 1),
    "export":   ({"--format": r"md|json|html"}, 1),
    "doctor":   ({"--json": None}, 0),
    "agents":   ({}, 0),
    "config":   ({}, 2),
    "undo":     ({"--list": None, "--json": None}, 1),
    "rewind":   ({"--list": None, "--json": None}, 1),
    "continue": ({}, 0),
    "resume":   ({}, 1),
    "review":   ({"--json": None, "--lead": _WORD}, 40),
}
AI_ALLOWED_COMMANDS = set(AI_COMMAND_SCHEMA)
AI_CONFIG_SUBCOMMANDS = {"list", "get", "path", None}
# AI-proposed commands that change files or spend AI quota: always ask the user first.
AI_CONFIRM_COMMANDS = {"undo", "rewind", "continue", "resume", "review"}
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
                  "lead-model", "effort", "lead-effort", "team", "setup", "office"}


def suggest(word: str, choices) -> "Optional[str]":
    """Closest known command for a typo (e.g. 'ussage' -> 'usage'), or None."""
    import difflib
    hits = difflib.get_close_matches(word.lower(), sorted(choices), n=1, cutoff=0.7)
    return hits[0] if hits else None


def slash_command_matches(line: str, commands=None) -> List[str]:
    """Slash commands available for readline completion at the start of a chat line."""
    if not line.startswith("/") or any(ch.isspace() for ch in line):
        return []
    choices = commands if commands is not None else (LOCAL_COMMANDS | PASSTHROUGH)
    prefix = line.lower()
    priority = {name: index for index, name in enumerate(SLASH_COMMAND_ORDER)}
    ordered = sorted(choices, key=lambda name: (priority.get(name, len(priority)), name))
    return ["/" + name for name in ordered if ("/" + name).startswith(prefix)]


def _readline_command_completer(text: str, state: int):
    if state == 0:
        try:
            import readline
            line = readline.get_line_buffer()
        except Exception:
            return None
        matches = slash_command_matches(line)
        if text:
            matches = [value for value in matches if value.lower().startswith(text.lower())]
        _readline_command_completer.matches = matches
    matches = getattr(_readline_command_completer, "matches", [])
    return matches[state] if state < len(matches) else None


SLASH_DESCRIPTIONS = {
    "agents": "show available AI coding CLIs",
    "build": "start a team build directly",
    "clear": "clear the chat conversation",
    "config": "view or change OmniCLI settings",
    "continue": "continue the latest unfinished run",
    "doctor": "check OmniCLI and agent setup",
    "effort": "choose an AI's reasoning effort",
    "exit": "leave chat",
    "export": "export a run report",
    "help": "show chat commands",
    "lead": "choose the AI that plans and reviews",
    "lead-effort": "choose the lead model's reasoning effort",
    "lead-model": "choose the model used for lead calls",
    "logs": "view agent prompts and output",
    "model": "choose the model used for worker tasks",
    "models": "list models each CLI supports",
    "new": "switch to a new project folder",
    "office": "open the animated live team view",
    "project": "show or switch the project folder",
    "resume": "resume a saved team run",
    "review": "ask an AI to review the project",
    "rewind": "undo a task checkpoint",
    "runs": "list saved team runs",
    "setup": "configure the team and defaults",
    "status": "show live team progress",
    "team": "view, build, or restore a custom team",
    "undo": "undo a task checkpoint",
    "usage": "show agent usage and time",
    "workers": "choose which AIs build tasks",
}
SLASH_COMMAND_ORDER = ["model", "team", "status", "office", "workers", "lead", "lead-model", "lead-effort", "effort",
                      "build", "help", "models", "setup"]


def choose_slash_completion(line: str, matches: List[str], selected: int) -> Optional[str]:
    """Return the completion to insert, or None when Enter should run an exact command."""
    if not matches:
        return None
    candidate = matches[selected % len(matches)]
    if candidate.lower() == line.lower():
        return None
    return candidate + " "


def _menu_rows(matches: List[str], selected: int, width: int, height: int) -> List[str]:
    """Compact command dropdown with a highlighted row and keyboard hints."""
    if not matches:
        return []
    option_rows = max(1, min(8, height - 5))
    start = min(max(0, selected - option_rows + 1), max(0, len(matches) - option_rows))
    shown = matches[start:start + option_rows]
    content_width = max(20, width - 2)
    rows = ["  Slash commands"]
    for offset, command in enumerate(shown):
        index = start + offset
        marker = "›" if index == selected else " "
        label = command.ljust(16)
        description = SLASH_DESCRIPTIONS.get(command[1:], "run this OmniCLI command")
        row = f" {marker} {label} {description}"[:content_width]
        if index == selected and not os.environ.get("NO_COLOR"):
            row = "\033[44m\033[97m" + row.ljust(content_width) + "\033[0m"
        rows.append(row)
    if len(matches) > len(shown):
        rows.append(f"  {start + 1}-{start + len(shown)} of {len(matches)} commands")
    rows.append("  ↑/↓ choose · Enter select · Esc close")
    return rows


def _cell_width(char: str) -> int:
    import unicodedata
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def _input_view(line: str, cursor: int, width: int) -> Tuple[str, int]:
    """Horizontal viewport for the single-row input; return visible text and caret column."""
    prefix = [0]
    for char in line:
        prefix.append(prefix[-1] + _cell_width(char))
    cursor_width = prefix[cursor]
    left_target = max(0, cursor_width - width + 1)
    left = 0
    while left < cursor and prefix[left + 1] <= left_target:
        left += 1
    right = left
    while right < len(line) and prefix[right + 1] - prefix[left] <= width:
        right += 1
    return line[left:right], cursor_width - prefix[left]


def read_chat_line(prompt: str) -> str:
    """Read a chat line with an inline, arrow-selectable slash-command menu on TTYs."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return input(prompt)
    try:
        import readline
        import shutil
        import termios
        import tty
        import codecs
    except Exception:
        return input(prompt)

    fd = sys.stdin.fileno()
    try:
        original = termios.tcgetattr(fd)
    except Exception:
        return input(prompt)
    prompt_width = len(re.sub(r"\x1b\[[0-9;]*m", "", prompt))
    line, cursor = "", 0
    selected, last_matches, menu_hidden = 0, [], False
    history_index, saved_line = None, ""
    width, height = shutil.get_terminal_size((100, 24))
    decoder = codecs.getincrementaldecoder("utf-8")()

    def read_char():
        while True:
            raw = os.read(fd, 1)
            if not raw:
                return ""
            char = decoder.decode(raw)
            if char:
                return char

    def matches_now():
        return [] if menu_hidden else slash_command_matches(line)

    def render():
        nonlocal selected, last_matches
        matches = matches_now()
        if matches != last_matches:
            selected = 0
        last_matches = matches
        available = max(1, width - prompt_width - 1)
        visible, caret = _input_view(line, cursor, available)
        rows = _menu_rows(matches, selected, width, height)
        # The cursor is always on the single-row input line. Clear the old menu below it,
        # draw the current contents, then put the cursor back at the editing position.
        sys.stdout.write("\r\033[J" + prompt + visible)
        for row in rows:
            sys.stdout.write("\r\n" + row)
        if rows:
            sys.stdout.write("\033[{}A".format(len(rows)))
        sys.stdout.write("\r")
        if prompt_width + caret:
            sys.stdout.write("\033[{}C".format(prompt_width + caret))
        sys.stdout.flush()

    def clear_and_finish(text: str):
        sys.stdout.write("\r\033[J" + prompt + text + "\r\n")
        sys.stdout.flush()

    def read_escape():
        import select
        if not select.select([fd], [], [], 0.08)[0]:
            return "ESC"
        first = os.read(fd, 1).decode("ascii", "ignore")
        if first != "[":
            return "ESC"
        sequence = ""
        while len(sequence) < 4:
            if not select.select([fd], [], [], 0.08)[0]:
                break
            char = os.read(fd, 1).decode("ascii", "ignore")
            sequence += char
            if char.isalpha() or char == "~":
                break
        return "[" + sequence

    try:
        tty.setraw(fd)
        render()
        while True:
            char = read_char()
            if char in ("\r", "\n"):
                matches = matches_now()
                completion = choose_slash_completion(line, matches, selected)
                if completion is not None:
                    line = completion
                    cursor = len(line)
                    menu_hidden = False
                    render()
                    continue
                clear_and_finish(line)
                if line:
                    readline.add_history(line)
                return line
            if char == "\x03":
                clear_and_finish("")
                raise KeyboardInterrupt
            if char == "\x04":
                if not line:
                    clear_and_finish("")
                    raise EOFError
                if cursor < len(line):
                    line = line[:cursor] + line[cursor + 1:]
            elif char == "\x1b":
                key = read_escape()
                matches = matches_now()
                if key == "ESC":
                    menu_hidden = bool(matches)
                elif key == "[A":
                    if matches:
                        selected = (selected - 1) % len(matches)
                    else:
                        history_index = max(1, (history_index or readline.get_current_history_length() + 1) - 1)
                        saved_line = line if history_index == readline.get_current_history_length() else saved_line
                        line = readline.get_history_item(history_index) or ""
                        cursor = len(line)
                elif key == "[B":
                    if matches:
                        selected = (selected + 1) % len(matches)
                    elif history_index is not None:
                        history_index += 1
                        if history_index > readline.get_current_history_length():
                            history_index, line = None, saved_line
                        else:
                            line = readline.get_history_item(history_index) or ""
                        cursor = len(line)
                elif key == "[C":
                    cursor = min(len(line), cursor + 1)
                elif key == "[D":
                    cursor = max(0, cursor - 1)
                elif key in ("[H", "[1~"):
                    cursor = 0
                elif key in ("[F", "[4~"):
                    cursor = len(line)
                elif key == "[3~" and cursor < len(line):
                    line = line[:cursor] + line[cursor + 1:]
                menu_hidden = False if key != "ESC" else menu_hidden
            elif char == "\t":
                matches = matches_now()
                if matches:
                    line = matches[selected] + " "
                    cursor = len(line)
                    menu_hidden = False
            elif char in ("\x7f", "\x08"):
                if cursor:
                    line = line[:cursor - 1] + line[cursor:]
                    cursor -= 1
                menu_hidden = False
            elif char == "\x01":  # Ctrl-A
                cursor = 0
            elif char == "\x05":  # Ctrl-E
                cursor = len(line)
            elif char == "\x15":  # Ctrl-U
                line, cursor = "", 0
                menu_hidden = False
            elif char == "\x17":  # Ctrl-W
                left = line[:cursor].rstrip()
                cut = len(left.rsplit(" ", 1)[0]) if " " in left else 0
                line = line[:cut] + line[cursor:]
                cursor = cut
                menu_hidden = False
            elif char.isprintable():
                line = line[:cursor] + char + line[cursor:]
                cursor += len(char)
                history_index = None
                menu_hidden = False
            render()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


HELP = """\
Talk in plain English - the lead AI answers, and asks before the team builds anything.
  build a todo app with FastAPI and SQLite      why did t2 fail?      add tests for the API

/build <goal>        start the team now (no AI call to decide)
/status  /runs  /logs [task]  /usage  /export  /undo <task>  /continue  /review  /doctor  /agents  /config
/project [dir]       show or switch the project folder (/new <dir> for a folder that doesn't exist yet)
/team                show the active team; /team custom builds a lineup
/team save|use|list  save, restore, or list named team lineups and per-CLI models
/lead [agent]        choose the lead AI (plans, reviews, and is who you talk to here)
/workers             choose which AIs work on tasks
/model [agent]       set worker model       /lead-model [agent] set lead model separately
/effort [agent]      set worker reasoning effort
/lead-effort [agent] set lead reasoning effort separately
/office [run|path]   open the live team office in your browser (chat starts it for you)
/models              list every AI's available models           /setup           guided setup of all of it
/clear               forget the conversation so far
!<command>           run a shell command in the project folder, e.g. !ls  !git log --oneline
/exit                quit (or Ctrl-D)
Type / to open the command menu; use ↑/↓ to choose and Enter to select."""


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
    """Validate an AI-proposed command against AI_COMMAND_SCHEMA. Returns (argv or None, reason if refused)."""
    if not args or not isinstance(args, list):
        return None, "empty command"
    args = [str(a) for a in args if str(a).strip()]
    if args and args[0] == "omnicli":
        args = args[1:]
    if not args or args[0] not in AI_COMMAND_SCHEMA:
        return None, f"'{args[0] if args else ''}' isn't something I can run from chat"
    cmd, rest = args[0], args[1:]
    flags, max_pos = AI_COMMAND_SCHEMA[cmd]
    positionals: List[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("-"):
            if tok not in flags:
                return None, f"the option '{tok}' isn't allowed from chat - type the command yourself if you want it"
            rule = flags[tok]
            if rule is not None:
                if i + 1 >= len(rest) or not re.fullmatch(rule, rest[i + 1]):
                    return None, f"'{tok}' needs a valid value"
                i += 1
        else:
            positionals.append(tok)
        i += 1
    if len(positionals) > max_pos:
        return None, f"too many arguments for '{cmd}'"
    if cmd == "config":
        sub = positionals[0] if positionals else None
        if sub not in AI_CONFIG_SUBCOMMANDS or (sub != "get" and len(positionals) > 1):
            return None, "changing settings needs you to type /config set ... yourself"
    if project is not None and cmd in DIR_COMMANDS:
        args = args + ["-d", str(project)]
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
1. Answer and discuss. For a research request, investigate the relevant project files and accessible primary
   sources. Cite the file or document supporting important claims. Say which details are inferred or unverified.
   For a quick factual question, answer directly.
2. Start the team building - when the user asks to create, build, add, change or fix code, reply with
   one short sentence about what you'll have the team do, then on the LAST line:
   ACTION: {{"type": "build", "goal": "<precise, self-contained goal: what to build or change, language/framework, tests>", "dir": "<folder, or empty to use the current project>"}}
   For a change to an existing project, write the goal as a change ("In the existing todo app, add ...").
   For a brand-new project with no current folder, suggest a short folder name in "dir" (e.g. "todo-app").
3. Run an OmniCLI command for them - last line:
   ACTION: {{"type": "command", "args": ["status"]}}
   Allowed (no other options): status, runs [--all], usage [--by agent|kind|run|day] [--days N],
   logs [TASK] [--tail N], export [--format md|json|html], doctor, agents, config list|get KEY,
   undo TASK, continue, resume [REF], review [focus text]. The user confirms undo/continue/resume/review.
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
                 make_conductor: Optional[Callable] = None, interactive: Optional[bool] = None,
                 office_mode: str = "off"):
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
        self._office_servers = []
        self.office_mode = office_mode   # "on" = start the live office + show its link, "open" = also open it
        self.office_url: Optional[str] = None
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
            spec = self.ag.with_saved_lead_options(self.specs[name], name)
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

    def do_action(self, action: Dict, user_request: str = "") -> None:
        if action.get("type") == "build":
            goal = str(action.get("goal") or "").strip()
            if user_request and user_request.strip() != goal:
                goal = user_request + ("\n\nBuild brief from the conversation:\n" + goal if goal else "")
            self.start_build(goal, str(action.get("dir") or ""),
                             images=getattr(self, "_current_images", []))
        elif action.get("type") == "command":
            argv, why = sanitize_command(action.get("args") or [], self.project)
            if argv is None:
                self.out(f"{BADGE_WARN} Skipped the suggested command: {why}.")
                return
            if argv[0] in AI_CONFIRM_COMMANDS and not self._ask(
                    f"The AI wants to run `omnicli {' '.join(shlex.quote(a) for a in argv)}`. Run it?", default_yes=False):
                self.out(c_dim("Okay, not run."))
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
            self.do_action(action, user_request=line)
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
        elif cmd in ("lead", "workers", "model", "lead-model", "effort", "lead-effort", "team", "setup"):
            self.team_command(cmd, args)
        elif cmd == "build":
            self.start_build(" ".join(args), images=getattr(self, "_current_images", []))
        elif cmd == "office":
            self.open_office(args)
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

    def start_live_office(self) -> None:
        """Start the chat's own office server. It follows self.project, so whatever project the chat
        works on (including a run it starts later) shows up live; before any run it shows the team at rest."""
        from omnicli.commands import office
        try:
            server, url = office.start_preferred(lambda: self.project, fallback=self._office_idle)
        except OSError as ex:
            self.out(f"{BADGE_WARN} The live office could not start ({ex}); chat works as usual.")
            return
        self._office_servers.append(server)
        self.office_url = url
        if self.office_mode == "open":
            self._open_in_browser(url)

    def _office_idle(self, project):
        from omnicli.commands import office
        wanted = settings.get("workers") or []
        workers = [w for w in wanted if w in self.ready] or list(self.ready)
        return office.idle_snapshot(project, self.lead, [w for w in workers if w != self.lead])

    @staticmethod
    def _open_in_browser(url: str) -> bool:
        import webbrowser
        try:
            return bool(webbrowser.open(url))
        except (webbrowser.Error, OSError):
            return False

    def stop_live_office(self) -> None:
        for server in self._office_servers:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        self._office_servers = []
        self.office_url = None

    def open_office(self, args: List[str]) -> None:
        from omnicli.commands import office

        if not args and self.office_url:
            # the chat's own office already follows this project: just show it
            if self._open_in_browser(self.office_url):
                self.out(f"{BADGE_INFO} Live office opened: {self.office_url}  (chat remains available here)")
            else:
                self.out(f"{BADGE_INFO} Live office: {self.office_url}  - open that link in your browser.")
            return
        if len(args) > 1:
            self.out(f"{BADGE_WARN} Usage: /office [run number, project path, or name]")
            return
        ref, dir_opt = (args[0], None) if args else (None, None)
        if ref:
            candidate = Path(ref).expanduser()
            if candidate.is_dir():
                ref, dir_opt = None, str(candidate)
        elif self.project is not None:
            dir_opt = str(self.project)
        project_dir, problem = office.status.resolve_run(ref, dir_opt, cwd=str(self.project or Path.cwd()))
        if project_dir is None:
            self.out(f"{BADGE_WARN} {problem}")
            return
        try:
            server, url = office.start_background(project_dir)
        except OSError as ex:
            self.out(f"{BADGE_WARN} Could not open the office view: {ex}")
            return
        self._office_servers.append(server)
        import webbrowser
        try:
            opened = webbrowser.open(url)
        except (webbrowser.Error, OSError):
            opened = False
        if opened:
            self.out(f"{BADGE_INFO} Live office opened: {url}  (chat remains available here)")
        else:
            self.out(f"{BADGE_INFO} Live office is running at {url}; open that link in your browser.  "
                     "(chat remains available here)")

    # ---------------------------------------------------------- team choices
    def team_command(self, cmd: str, args: List[str]) -> None:
        from omnicli.commands import models_cmd as mc
        from omnicli.picker import CANCEL, choose_one
        if self.lead is None or not self.ready:
            self.select_lead()
        ready = self.ready
        if cmd == "team":
            if args:
                action = args[0].lower()
                if action in ("custom", "configure") and len(args) == 1:
                    mc.run_custom_team(self.specs, ready, self._health or {},
                                       input_fn=self.input_fn, out=self.out)
                    self.requested_lead = None
                    self.select_lead()
                    return
                if action == "save" and len(args) == 2:
                    from omnicli import profiles
                    try:
                        profiles.save(args[1])
                    except profiles.ProfileError as ex:
                        self.out(f"{BADGE_WARN} Could not save team: {ex}")
                        return
                    self.out(f"{BADGE_INFO} Saved team '{args[1]}' with its lead, workers, and per-CLI models.")
                    return
                if action == "use" and len(args) == 2:
                    from omnicli import profiles
                    try:
                        profiles.apply(args[1])
                    except profiles.ProfileError as ex:
                        self.out(f"{BADGE_WARN} Could not load team: {ex}")
                        return
                    self.requested_lead = None
                    self.select_lead()
                    self.out(f"{BADGE_INFO} Loaded team '{args[1]}'.")
                    return
                if action == "list" and len(args) == 1:
                    from omnicli import profiles
                    try:
                        names = sorted(profiles.load_all())
                    except profiles.ProfileError as ex:
                        self.out(f"{BADGE_WARN} Could not list teams: {ex}")
                        return
                    if not names:
                        self.out(c_dim("No saved teams yet. Use /team save <name>."))
                    else:
                        self.out("Saved teams: " + ", ".join(names))
                    return
                self.out(f"{BADGE_WARN} Usage: /team [custom | save <name> | use <name> | list]")
                return
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
        # model / lead-model / effort: which agent?
        name = args[0] if args else None
        if name is None:
            key = "model_args" if cmd in ("model", "lead-model") else "effort_args"
            candidates = [(n, self.specs[n].get("name", n)) for n in ready if self.specs[n].get(key)]
            if not candidates:
                self.out(f"{BADGE_WARN} No ready AI supports choosing a {cmd}.")
                return
            role = "lead" if cmd in ("lead-model", "lead-effort") else "worker"
            name = choose_one(f"Which AI's {role} model do you want to change?" if cmd in ("model", "lead-model")
                              else f"Which AI's {role} effort do you want to change?" if cmd in ("effort", "lead-effort")
                              else f"Which AI's {cmd} do you want to change?", candidates, current=self.lead,
                              input_fn=self.input_fn, out=self.out)
            if name is CANCEL:
                return
            name = name or self.lead
        if name not in self.specs:
            self.out(f"{BADGE_WARN} Unknown AI '{name}'. Known: {', '.join(self.specs)}")
            return
        if cmd in ("model", "lead-model"):
            is_lead_model = cmd == "lead-model"
            choice = mc.pick_model(name, self.specs[name], self.input_fn, self.out,
                                   setting_key="lead_models" if is_lead_model else "models",
                                   role_label="lead planning and review" if is_lead_model else "worker tasks",
                                   inherit_worker=is_lead_model)
            if isinstance(choice, str) and name != self.lead:
                self.out(f"  Chat is still with {self.lead}; use /lead {name} to switch.")
        else:
            lead_effort = cmd == "lead-effort"
            mc.pick_effort(name, self.specs[name], self.input_fn, self.out,
                           setting_key="lead_effort" if lead_effort else "effort",
                           role_label="lead planning and review" if lead_effort else "worker tasks")

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
        if self.office_url:
            self.out(f"{c_bold('Live office')} {c_cyan(self.office_url)}  "
                     + c_dim("watch your AI team work in the browser · /office opens it"))
        self.out(c_dim("Type / for command suggestions · arrow keys choose · !cmd runs a shell command · /exit or Ctrl-D to quit\n"))

    def loop(self) -> int:
        _enable_history()
        self.select_lead()
        if self.office_mode in ("on", "open"):
            self.start_live_office()
        self.welcome()
        while True:
            try:
                prompt = c_green(c_bold("you › "))
                if self.interactive and self.input_fn is input:
                    line = read_chat_line(prompt)
                else:
                    line = self.input_fn(prompt)
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
        self.stop_live_office()
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
        readline.set_completer(_readline_command_completer)
        readline.set_completer_delims(" \t\n")
        if "libedit" in (readline.__doc__ or "").lower():
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
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
    p.add_argument("--no-office", action="store_true",
                   help="Don't start the live office view (default: the `office` setting, normally on)")
    p.set_defaults(func=run)


def run(args) -> int:
    project = None
    if getattr(args, "dir", None):
        project = Path(args.dir).expanduser().resolve()
    else:
        project = default_project()
    mode = "off" if getattr(args, "no_office", False) else str(settings.get("office") or "on").lower()
    if mode not in ("on", "open", "off"):
        mode = "on"
    return ChatSession(project=project, lead=getattr(args, "lead", None), office_mode=mode).loop()
