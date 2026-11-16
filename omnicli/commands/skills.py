"""Show the local task-history leaderboard for installed AI agents."""

import json

from omnicli import ledger
from omnicli.ui import c_bold, c_cyan, c_dim, print_table

NAMES = ["skills"]


def add_parser(subparsers):
    parser = subparsers.add_parser("skills", help="Show which agents perform best by task category")
    parser.add_argument("--category", choices=ledger.SKILL_CATEGORIES, help="Show one task category")
    parser.add_argument("--json", action="store_true", help="Print machine-readable rankings")
    parser.set_defaults(func=handler)


def handler(args):
    rows = ledger.skill_rankings()
    if args.category:
        rows = [row for row in rows if row["category"] == args.category]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("No completed task history yet. Run `omnicli team` to start building local skill rankings.")
        return 0
    display = [[c_bold(row["category"]), c_cyan(row["agent"]), str(row["tasks"]),
                "{}/{} ({:.0%})".format(row["first_try"], row["tasks"], row["first_try_rate"]),
                "{:.1f}".format(row["avg_revisions"]), "{:.1f}m".format(row["avg_seconds"] / 60)]
               for row in rows]
    print_table(["Category", "Agent", "Tasks", "First-pass", "Avg revisions", "Avg time"], display,
                title="Local AI skill history")
    print(c_dim("Rankings stay in ~/.omnicli/skill_outcomes.jsonl; only aggregate stats are shared with the planning lead."))
    return 0
