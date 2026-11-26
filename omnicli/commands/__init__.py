"""
Pluggable OmniCLI subcommands.

Each module listed in COMMAND_MODULES lives in omnicli/commands/<module>.py and exposes:

    NAMES: list[str]                 every subcommand name + alias it registers
                                     (cli.py uses this to tell subcommands from
                                     passthrough shell commands like `omnicli git status`)
    def add_parser(subparsers) -> None
                                     add its argparse subparser(s); each one must call
                                     parser.set_defaults(func=<handler>)
    handler(args) -> int             the exit code

A module that is missing or fails to import is skipped with a warning, so one broken
command can never take the whole CLI down.
"""

import importlib
import sys
from typing import List, Set

COMMAND_MODULES = [
    "agents_cmd",   # omnicli agents [list|add|remove|enable|disable|permissions]
    "usage",        # omnicli usage
    "sessions",     # omnicli runs / resume / continue
    "status",       # omnicli status
    "logs",         # omnicli logs
    "doctor",       # omnicli doctor
    "config_cmd",   # omnicli config / init
    "undo",         # omnicli undo
    "export",       # omnicli export
    "review",       # omnicli review
    "chat",         # omnicli chat (and plain `omnicli` in a terminal)
    "models_cmd",   # omnicli models / setup
    "tune",         # compare reasoning effort on scored examples
    "profiles",     # omnicli profiles / profile
    "compare",      # omnicli compare
    "budget",       # omnicli budget
    "pr",           # omnicli pr / pull-request
    "visual",       # omnicli visual screenshot + review
    "office",       # omnicli office animated live team view
    "skills",       # omnicli skills leaderboard
    "memory",       # omnicli memory lessons
    "timeline",     # omnicli timeline flight recorder
    "arena",        # omnicli arena bake-off
]


def load_modules() -> List:
    mods = []
    for name in COMMAND_MODULES:
        try:
            mods.append(importlib.import_module(f"omnicli.commands.{name}"))
        except ModuleNotFoundError as ex:
            if ex.name != f"omnicli.commands.{name}":
                print(f"[omnicli] command '{name}' unavailable: {ex}", file=sys.stderr)
        except Exception as ex:
            print(f"[omnicli] command '{name}' failed to load: {ex}", file=sys.stderr)
    return mods


def command_names() -> Set[str]:
    names: Set[str] = set()
    for m in load_modules():
        names.update(getattr(m, "NAMES", []))
    return names


def register_all(subparsers) -> Set[str]:
    names: Set[str] = set()
    for m in load_modules():
        try:
            m.add_parser(subparsers)
            names.update(getattr(m, "NAMES", []))
        except Exception as ex:
            print(f"[omnicli] command module {m.__name__} could not register: {ex}", file=sys.stderr)
    return names
