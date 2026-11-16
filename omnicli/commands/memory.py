"""Inspect and manage short lessons reused by future team runs."""

import json

from omnicli import ledger
from omnicli.ui import c_bold, c_dim, print_table

NAMES = ["memory"]


def add_parser(subparsers):
    parser = subparsers.add_parser("memory", help="Show reusable lessons from previous team runs")
    parser.add_argument("--category", choices=ledger.SKILL_CATEGORIES, help="Filter by task category")
    parser.add_argument("--json", action="store_true", help="Print machine-readable lessons")
    parser.set_defaults(func=handler)


def handler(args):
    rows = ledger.read_team_memory(getattr(args, "category", None))
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif not rows:
        print("No team lessons saved yet. Lessons from review revisions will appear here.")
    else:
        from pathlib import Path
        print_table(["Category", "Project", "Lesson"],
                    [[c_bold(row.get("category", "other")), Path(row["project"]).name if row.get("project") else c_dim("(none)"),
                      row.get("lesson", "")] for row in rows], title="Team memory")
        print(c_dim("Stored locally in ~/.omnicli/team_memory.jsonl. Lessons are reused only in their own project "
                    "(share them everywhere with: omnicli config set memory_scope global)."))
    return 0
