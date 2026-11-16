"""
`omnicli config` and `omnicli init` - like Claude Code's /config and /init.

    omnicli config                       same as `omnicli config list`
    omnicli config list [--json]         every setting, its value and where it comes from
    omnicli config get KEY               effective value (plain, for scripts); KEY may be models.claude
    omnicli config set KEY VALUE         e.g. `config set lead claude`, `config set workers claude,agy`
    omnicli config unset KEY             back to the default
    omnicli config path                  where the config file lives
    omnicli config edit                  open it in $VISUAL / $EDITOR, then validate it
    omnicli config reset [-y]            delete it (everything back to defaults)

    omnicli init [--force]               create ~/.omnicli/config.json + agents.json templates

All values go through omnicli/settings.py, which `omnicli team` falls back to for any
flag that is not passed, so a `config set` changes team defaults immediately.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from omnicli import paths, settings
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

NAMES = ["config", "init"]

# Settings `omnicli init` writes into a fresh config.json (the ones people usually tune).
INIT_KEYS = ["lead", "workers", "jobs", "rounds", "retries", "max_tasks", "task_timeout"]

# Smallest sensible value for numeric settings; cli.py would silently clamp or misbehave below these.
_MIN = {
    "jobs": 1, "rounds": 1, "max_tasks": 1,
    "retries": 0, "max_calls": 0, "max_minutes": 0,
    "task_timeout": 0.1, "lead_timeout": 0.1,
}

# Keys whose default (None) means "let OmniCLI choose"; `config set lead auto` restores that.
_AUTO_KEYS = {"lead", "workers"}

AGENTS_TEMPLATE: Dict[str, Any] = {
    "agents": {},
    "disabled": [],
    "_help": [
        "OmniCLI agent overrides. Only the 'agents' and 'disabled' keys are read; keys starting with _ are ignored.",
        "Built-in agents: claude, codex, agy, muse (run `omnicli agents` to see them and their health).",
        "Override part of a built-in agent: \"agents\": {\"claude\": {\"strengths\": \"careful reviews, refactors\"}}",
        "Change the flags an agent runs with: \"agents\": {\"agy\": {\"edit\": [\"-p\", \"{prompt}\", \"--mode\", \"accept-edits\"]}}",
        "  'readonly' = lead plan/review (must not modify files), 'edit' = worker build mode.",
        "  Placeholders: {prompt} (the prompt text), {prompt_file} (path to a file holding it), {dir} (project dir).",
        "Add a custom agent: \"agents\": {\"mybot\": {\"name\": \"My Bot\", \"bin\": \"mybot\", \"strengths\": \"python scripts\",",
        "  \"readonly\": [\"ask\", \"{prompt}\"], \"edit\": [\"do\", \"--yes-edits\", \"{prompt}\"]}}",
        "Hide an agent from team mode: \"disabled\": [\"agy\"]",
        "A full example is in '_example' below: copy its entries into 'agents' / 'disabled' to use them.",
    ],
    "_example": {
        "agents": {
            "claude": {"strengths": "architecture, careful reviews, refactoring"},
            "mybot": {
                "name": "My Bot",
                "bin": "mybot",
                "strengths": "python scripts and tests",
                "readonly": ["ask", "{prompt}"],
                "edit": ["do", "--workspace", "{dir}", "{prompt_file}"],
            },
        },
        "disabled": ["agy"],
    },
}


# --------------------------------------------------------------------------- parser

def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "config",
        help="View or change OmniCLI defaults (lead, workers, jobs, ...) in ~/.omnicli/config.json",
        description="View or change the defaults `omnicli team` uses for any flag you do not pass.",
    )
    # A different dest than `list --json`: argparse lets a subparser's default overwrite the parent's value.
    p.add_argument("--json", dest="json_top", action="store_true", help="Same as `config list --json`")
    p.set_defaults(func=handle_config, config_action=None)
    sub = p.add_subparsers(dest="config_action", metavar="ACTION")

    s = sub.add_parser("list", aliases=["ls", "show"], help="Show every setting, its value and source")
    s.add_argument("--json", action="store_true", help="Machine-readable output")
    s.set_defaults(func=handle_config, config_action="list")

    s = sub.add_parser("get", help="Print the effective value of one setting (e.g. jobs, models.claude)")
    s.add_argument("key")
    s.set_defaults(func=handle_config, config_action="get")

    s = sub.add_parser("set", help="Save a default, e.g. `config set lead claude` or `config set workers claude,agy`")
    s.add_argument("key")
    s.add_argument("value", nargs="+", help="New value (lists: comma-separated or several words)")
    s.set_defaults(func=handle_config, config_action="set")

    s = sub.add_parser("unset", help="Remove your value so the default applies again")
    s.add_argument("key")
    s.set_defaults(func=handle_config, config_action="unset")

    s = sub.add_parser("path", help="Print the config file location")
    s.set_defaults(func=handle_config, config_action="path")

    s = sub.add_parser("edit", help="Open the config file in $VISUAL / $EDITOR, then validate it")
    s.set_defaults(func=handle_config, config_action="edit")

    s = sub.add_parser("reset", help="Delete the config file (all settings back to defaults)")
    s.add_argument("-y", "--yes", action="store_true", help="Do not ask for confirmation")
    s.set_defaults(func=handle_config, config_action="reset")

    pi = subparsers.add_parser(
        "init",
        help="Set up ~/.omnicli: config.json with common defaults and an agents.json template",
        description="Create ~/.omnicli/config.json and ~/.omnicli/agents.json if they are missing.",
    )
    pi.add_argument("--force", action="store_true",
                    help="Back up existing files to <name>.bak and rewrite them from defaults")
    pi.set_defaults(func=handle_init)


def handle_config(args) -> int:
    action = getattr(args, "config_action", None) or "list"
    if action in ("ls", "show"):
        action = "list"
    fn = {
        "list": _cmd_list, "get": _cmd_get, "set": _cmd_set, "unset": _cmd_unset,
        "path": _cmd_path, "edit": _cmd_edit, "reset": _cmd_reset,
    }.get(action)
    if fn is None:
        return _fail(f"unknown config action '{action}'", "omnicli config --help")
    return fn(args)


# --------------------------------------------------------------------------- helpers

def _fail(msg: str, hint: Optional[str] = None) -> int:
    print(f"{BADGE_FAILED} {msg}")
    if hint:
        print(c_dim(f"  try: {hint}"))
    return 1


def _type_name(typ) -> str:
    return {str: "text", list: "list", int: "whole number", float: "number", bool: "true/false", dict: "per-agent"}.get(typ, typ.__name__)


def fmt_value(value: Any) -> str:
    """Human-readable value: lists comma-joined, bools true/false, None as (auto)."""
    if value is None:
        return "(auto)"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        s = repr(value)
        return s[:-2] if s.endswith(".0") else s
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "(none)"
    if isinstance(value, dict):
        return json.dumps(value) if value else "{}"
    return str(value)


def _plain(value: Any) -> str:
    """Script-friendly value for `config get`: None -> empty, lists comma-joined, dicts JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        s = repr(value)
        return s[:-2] if s.endswith(".0") else s
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _was(value: Any) -> str:
    return "auto" if value is None else fmt_value(value)


def _check_key(key: str) -> Optional[str]:
    """Error message for an unknown / malformed key, or None if it is fine."""
    top, dot, sub = key.partition(".")
    if top not in settings.SCHEMA:
        return f"unknown setting '{key}'. Known settings: {', '.join(settings.SCHEMA)}"
    typ = settings.SCHEMA[top][0]
    if dot and typ is not dict:
        return f"'{top}' has no sub-keys (only {', '.join(k for k, v in settings.SCHEMA.items() if v[0] is dict)} do)"
    if dot and not sub:
        return f"missing agent name after '{top}.', e.g. {top}.claude"
    return None


def _value_problem(key: str, value: Any) -> Optional[str]:
    """Type/range problem with a stored value, or None."""
    typ = settings.SCHEMA[key][0]
    default = settings.SCHEMA[key][1]
    if value is None and default is None:
        return None
    if typ is bool:
        ok = isinstance(value, bool)
    elif typ is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif typ is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif typ is list:
        ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
    elif typ is dict:
        ok = isinstance(value, dict) and all(isinstance(v, str) for v in value.values())
    else:
        ok = isinstance(value, str)
    if not ok:
        want = {list: "a list of agent ids, e.g. [\"claude\", \"agy\"]",
                dict: "an object of agent -> text, e.g. {\"claude\": \"opus\"}"}.get(typ, _type_name(typ))
        return f"'{key}' should be {want}, got {json.dumps(value)}"
    return _range_problem(key, value)


def _range_problem(key: str, value: Any) -> Optional[str]:
    lo = _MIN.get(key)
    if lo is not None and isinstance(value, (int, float)) and value < lo:
        return f"'{key}' must be at least {fmt_value(lo)} (got {fmt_value(value)})"
    return None


def file_problems(path: Optional[Path] = None) -> List[str]:
    """Problems with the config file (missing file is fine). Unknown keys are reported but harmless."""
    path = path or paths.settings_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "null")
    except ValueError as ex:
        return [f"not valid JSON ({ex}); OmniCLI is ignoring the whole file and using defaults"]
    except OSError as ex:
        return [f"cannot read it ({ex})"]
    if not isinstance(data, dict):
        return ["must be a JSON object like {\"jobs\": 3}; OmniCLI is ignoring it and using defaults"]
    out = []
    for k, v in data.items():
        if k not in settings.SCHEMA:
            out.append(f"unknown setting '{k}' (ignored)")
            continue
        p = _value_problem(k, v)
        if p:
            out.append(p)
    return out


def _unreadable(path: Path) -> Optional[str]:
    """Why a config file must not be overwritten by set/unset (it would lose the user's text)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "null")
    except (ValueError, OSError) as ex:
        return f"{path} is not valid JSON ({ex})"
    if not isinstance(data, dict):
        return f"{path} is not a JSON object"
    return None


def _known_agents() -> List[str]:
    try:
        from omnicli import agents as ag
        return list(ag.load_agent_specs().keys())
    except Exception:
        return []


def _rows() -> List[Dict[str, Any]]:
    """One entry per setting: key, value (effective), default, source, type, description."""
    raw = settings.load_raw()
    eff = settings.load()
    out = []
    for key, (typ, default, desc) in settings.SCHEMA.items():
        out.append({
            "key": key,
            "value": eff.get(key),
            "default": default,
            "source": "user" if key in raw else "default",
            "type": _type_name(typ),
            "description": desc,
        })
    return out


# --------------------------------------------------------------------------- config actions

def _cmd_list(args) -> int:
    path = paths.settings_path()
    rows = _rows()
    problems = file_problems(path)
    if getattr(args, "json", False) or getattr(args, "json_top", False):
        print(json.dumps({
            "path": str(path),
            "exists": path.exists(),
            "settings": {r["key"]: {k: v for k, v in r.items() if k != "key"} for r in rows},
            "problems": problems,
        }, indent=2))
        return 0

    table = []
    for r in rows:
        src = c_green("user") if r["source"] == "user" else c_dim("default")
        val = r["value"]
        if isinstance(val, dict) and val:
            for sub, subval in sorted(val.items()):
                table.append([c_bold(c_cyan(f"{r['key']}.{sub}")), fmt_value(subval), src, c_dim(r["description"])])
            continue
        shown = fmt_value(val)
        if val is None or (isinstance(val, dict) and not val):
            shown = c_dim(shown)
        table.append([c_bold(c_cyan(r["key"])), shown, src, c_dim(r["description"])])
    print_table(["Key", "Value", "Source", "Description"], table, title="OmniCLI settings (defaults for `omnicli team`)")
    for p in problems:
        print(f"{BADGE_WARN} config file: {p}")
    if problems:
        print(c_dim("  fix with: omnicli config edit   (or start over: omnicli config reset)"))
    where = str(path) if path.exists() else f"{path} (not created yet - all defaults; `omnicli init` creates it)"
    print(c_dim(f"Config file: {where}"))
    print(c_dim("change with: omnicli config set <key> <value>   (undo: omnicli config unset <key>)"))
    return 0


def _cmd_get(args) -> int:
    key = args.key.strip()
    err = _check_key(key)
    if err:
        return _fail(err, "omnicli config list")
    print(_plain(settings.get(key)))
    return 0


def _cmd_set(args) -> int:
    key = args.key.strip()
    err = _check_key(key)
    if err:
        return _fail(err, "omnicli config list")
    path = paths.settings_path()
    bad = _unreadable(path)
    if bad:
        return _fail(f"{bad}; not overwriting it.", "omnicli config edit   (or: omnicli config reset)")

    top = key.partition(".")[0]
    typ = settings.SCHEMA[top][0]
    words = [w for w in args.value]
    raw = ",".join(words) if typ is list else " ".join(words)
    old = settings.get(key)

    if top in _AUTO_KEYS and "." not in key and raw.strip().lower() in ("auto", "none", "default"):
        settings.unset(key)
        print(f"{BADGE_SUCCESS} {c_bold(key)} = auto {c_dim('(was: ' + _was(old) + ')')}")
        return 0

    try:
        value = settings.coerce(key, raw)
        problem = _range_problem(top, value)
        if problem:
            raise settings.SettingsError(problem)
        if typ is list and not value:
            raise settings.SettingsError(f"'{key}' needs at least one agent id, e.g. claude,agy (or `auto`)")
        if typ is str and not str(value).strip():
            raise settings.SettingsError(f"'{key}' cannot be empty (use `omnicli config unset {key}`)")
        value = settings.set_value(key, raw)
    except settings.SettingsError as ex:
        return _fail(f"cannot set {key}: {ex}", f"omnicli config set {key} <{_type_name(typ)}>")
    except OSError as ex:
        return _fail(f"could not write {path}: {ex}")

    if top in _AUTO_KEYS:
        known = _known_agents()
        ids = value if isinstance(value, list) else [value]
        unknown = [a for a in ids if known and a not in known]
        if unknown:
            print(f"{BADGE_WARN} not a known agent: {', '.join(unknown)} (known: {', '.join(known)}). "
                  f"Saved anyway; add it in {paths.agents_config_path()} or check `omnicli agents`.")
    print(f"{BADGE_SUCCESS} {c_bold(key)} = {fmt_value(value)} {c_dim('(was: ' + _was(old) + ')')}")
    return 0


def _cmd_unset(args) -> int:
    key = args.key.strip()
    err = _check_key(key)
    if err:
        return _fail(err, "omnicli config list")
    path = paths.settings_path()
    bad = _unreadable(path)
    if bad:
        return _fail(f"{bad}; not overwriting it.", "omnicli config edit   (or: omnicli config reset)")
    old = settings.get(key)
    try:
        changed = settings.unset(key)
    except OSError as ex:
        return _fail(f"could not write {path}: {ex}")
    now = settings.get(key)
    if changed:
        print(f"{BADGE_SUCCESS} {c_bold(key)} removed; back to default {fmt_value(now)} {c_dim('(was: ' + _was(old) + ')')}")
    else:
        print(f"{BADGE_INFO} {c_bold(key)} was not set; already the default {fmt_value(now)}")
    return 0


def _cmd_path(args) -> int:
    print(paths.settings_path())
    return 0


def _editor() -> Optional[str]:
    for var in ("VISUAL", "EDITOR"):
        v = (os.environ.get(var) or "").strip()
        if v:
            return v
    return None


def _run_editor(cmd: List[str]) -> int:
    """Run the user's editor and wait for it (tests patch this)."""
    return subprocess.call(cmd)


def _cmd_edit(args) -> int:
    path = paths.settings_path()
    editor = _editor()
    if not editor:
        print(f"{BADGE_INFO} No editor set. The config file is: {c_bold(str(path))}")
        print(c_dim("  set one, e.g.:  export EDITOR=nano   (or: export VISUAL=\"code -w\")  then run: omnicli config edit"))
        print(c_dim("  or change values directly: omnicli config set <key> <value>"))
        return 1
    try:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(settings.load_raw(), indent=2) + "\n", encoding="utf-8")
    except OSError as ex:
        return _fail(f"could not create {path}: {ex}")
    try:
        cmd = shlex.split(editor) + [str(path)]
    except ValueError:
        cmd = [editor, str(path)]
    print(f"{BADGE_INFO} Opening {path} with {cmd[0]} ...")
    try:
        code = _run_editor(cmd)
    except (OSError, ValueError) as ex:
        return _fail(f"could not start editor '{editor}': {ex}", "export EDITOR=nano")
    if code:
        print(f"{BADGE_WARN} editor exited with code {code}")
    problems = file_problems(path)
    if problems:
        print(f"{BADGE_WARN} {path} has problems:")
        for p in problems:
            print(f"  {c_yellow('-')} {p}")
        print(c_dim("  fix with: omnicli config edit   (or start over: omnicli config reset)"))
        return 1
    print(f"{BADGE_SUCCESS} {path} is valid. See it with: omnicli config list")
    return 0


def _cmd_reset(args) -> int:
    path = paths.settings_path()
    if not path.exists():
        print(f"{BADGE_INFO} No config file at {path}; settings are already default.")
        return 0
    if not getattr(args, "yes", False):
        try:
            ans = input(c_bold(c_yellow(f"Delete {path} and go back to all defaults? [y/N]: "))).strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
            print()
        if ans not in ("y", "yes"):
            print(f"{BADGE_INFO} Kept {path}.")
            return 0
    try:
        path.unlink()
    except OSError as ex:
        return _fail(f"could not delete {path}: {ex}")
    print(f"{BADGE_SUCCESS} Deleted {path}; every setting is back to its default. See: omnicli config list")
    return 0


# --------------------------------------------------------------------------- init

def init_config_data() -> Dict[str, Any]:
    d = settings.defaults()
    return {k: d[k] for k in INIT_KEYS}


def backup_path(path: Path) -> Path:
    """<name>.bak, or <name>.<timestamp>.bak if that already exists."""
    bak = path.with_name(path.name + ".bak")
    if not bak.exists():
        return bak
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.{stamp}.bak")
    n = 1
    while bak.exists():
        bak = path.with_name(f"{path.name}.{stamp}-{n}.bak")
        n += 1
    return bak


def _write_file(path: Path, data: Dict[str, Any], force: bool) -> Tuple[str, Optional[Path]]:
    """Returns (status, backup) where status is 'written', 'replaced' or 'skipped'."""
    if path.exists() and not force:
        return "skipped", None
    backup = None
    if path.exists():
        backup = backup_path(path)
        path.replace(backup)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return ("replaced" if backup else "written"), backup


def handle_init(args) -> int:
    home = paths.omni_home()
    force = bool(getattr(args, "force", False))
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        return _fail(f"could not create {home}: {ex}", "set OMNICLI_HOME to a writable folder")

    print(f"\n{BADGE_INFO} Setting up OmniCLI in {c_bold(str(home))}")
    files = [
        (paths.settings_path(), init_config_data(), "defaults for `omnicli team` (lead, workers, jobs, ...)"),
        (paths.agents_config_path(), AGENTS_TEMPLATE, "AI CLI adapter overrides / custom agents"),
    ]
    failed = False
    for path, data, what in files:
        try:
            status, backup = _write_file(path, data, force)
        except OSError as ex:
            print(f"  {c_red('failed  ')} {path}: {ex}")
            failed = True
            continue
        if status == "skipped":
            print(f"  {c_dim('skipped ')} {path} {c_dim('(already exists; --force backs it up and rewrites it)')}")
            problem = _existing_problem(path)
            if problem:
                print(f"  {BADGE_WARN} {path.name}: {problem}")
        else:
            label = "replaced" if status == "replaced" else "wrote   "
            print(f"  {c_green(label)} {path} {c_dim('- ' + what)}")
            if backup:
                print(f"            {c_dim('old file backed up to ' + str(backup))}")

    print(f"\n{c_bold('Next steps:')}")
    print(f"  omnicli agents                 {c_dim('# which AI CLIs are installed and logged in')}")
    print(f"  omnicli config list            {c_dim('# see / change defaults: omnicli config set lead claude')}")
    print(f"  omnicli team \"<goal>\" -d DIR   {c_dim('# let a lead AI plan it and the others build it')}")
    return 1 if failed else 0


def _existing_problem(path: Path) -> Optional[str]:
    if path == paths.settings_path():
        probs = file_problems(path)
        return "; ".join(probs) if probs else None
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "null")
    except (ValueError, OSError) as ex:
        return f"not valid JSON ({ex}); OmniCLI ignores it"
    if not isinstance(data, dict):
        return "must be a JSON object; OmniCLI ignores it"
    return None
