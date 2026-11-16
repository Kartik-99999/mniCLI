"""Inspect or update the live budget limits of a team run."""

import json
import sys
from pathlib import Path

from omnicli import budgets
from omnicli.paths import project_state_dir

NAMES = ["budget"]


def add_parser(subparsers):
    parser = subparsers.add_parser("budget", help="Inspect or adjust a team's live call/time limits")
    parser.add_argument("-d", "--dir", default=".", help="Project directory (default: current directory)")
    parser.add_argument("--max-calls", type=int, help="Set the session call limit (0 = unlimited)")
    parser.add_argument("--max-minutes", type=float, help="Set the session time limit (0 = unlimited)")
    parser.set_defaults(func=handler)


def handler(args):
    target = Path(args.dir).expanduser().resolve()
    state_path = project_state_dir(target) / "state.json"
    if not state_path.is_file():
        print("No team run in {}; start one with `omnicli team`.".format(target), file=sys.stderr)
        return 1
    values = {}
    if args.max_calls is not None:
        if args.max_calls < 0:
            print("--max-calls must be 0 (unlimited) or more", file=sys.stderr)
            return 2
        values["max_calls"] = args.max_calls
    if args.max_minutes is not None:
        if args.max_minutes < 0:
            print("--max-minutes must be 0 (unlimited) or more", file=sys.stderr)
            return 2
        values["max_minutes"] = args.max_minutes
    if not values:
        print(json.dumps(budgets.read_control(target), indent=2, sort_keys=True))
        return 0
    current = budgets.write_control(target, values)
    print("Updated live budget for {}: {}".format(target, json.dumps(current, sort_keys=True)))
    print("The running team applies changes before its next agent call.")
    return 0
