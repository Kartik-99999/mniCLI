"""Create a GitHub pull request for a finished OmniCLI run."""

import shutil
import subprocess
import sys
from pathlib import Path

from omnicli import runs

NAMES = ["pr", "pull-request"]


def add_parser(subparsers):
    parser = subparsers.add_parser("pr", aliases=["pull-request"], help="Create a GitHub pull request for a team run")
    parser.add_argument("ref", nargs="?", help="Run number, project path, or goal/name fragment")
    parser.add_argument("-d", "--dir", help="Project directory (takes precedence over REF)")
    parser.add_argument("--base", help="Base branch")
    parser.add_argument("--title", help="Pull request title (defaults to the team goal)")
    parser.add_argument("--body", help="Pull request description (defaults to the run summary)")
    parser.add_argument("--draft", action="store_true", help="Open as a draft pull request")
    parser.add_argument("-y", "--yes", action="store_true", help="Create without an interactive confirmation")
    parser.set_defaults(func=handler)


def resolve(args):
    if args.dir:
        folder = Path(args.dir).expanduser().resolve()
        state = runs.load_state(folder)
        return folder, state
    ref = args.ref
    if not ref:
        ref = str(Path.cwd())
    record = runs.find_run(ref)
    if record is None:
        raise ValueError("no team run matches {!r}; see `omnicli runs`".format(ref))
    folder = Path(record["dir"]).expanduser().resolve()
    return folder, runs.load_state(folder)


def create_pr(folder, state, base=None, title=None, body=None, draft=False):
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("GitHub CLI `gh` is not installed; install it and run `gh auth login` first")
    if not state:
        raise ValueError("no readable OmniCLI run state in {}".format(folder))
    check = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(folder),
                           capture_output=True, text=True)
    if check.returncode or check.stdout.strip() != "true":
        raise ValueError("{} is not inside a Git repository".format(folder))
    goal = str(state.get("goal") or "OmniCLI project").strip()
    pr_title = title or goal
    if not pr_title:
        raise ValueError("a pull request title is required")
    if body is None:
        tasks = state.get("tasks") or []
        lines = ["## OmniCLI team run", "", "**Goal:** {}".format(goal),
                 "**Result:** {}".format(state.get("phase", "unknown")), "",
                 "### Tasks"]
        for task in tasks:
            if isinstance(task, dict):
                lines.append("- [{}] {} ({})".format(task.get("status", "pending"),
                                                      task.get("title") or task.get("id", "task"),
                                                      task.get("agent_used") or task.get("assignee") or "unassigned"))
        final = state.get("final") or {}
        if final.get("summary"):
            lines += ["", "### Summary", str(final["summary"])]
        body = "\n".join(lines)
    command = [gh, "pr", "create", "--title", pr_title, "--body", body]
    if base:
        command += ["--base", base]
    if draft:
        command.append("--draft")
    return subprocess.run(command, cwd=str(folder), capture_output=True, text=True)


def handler(args):
    try:
        folder, state = resolve(args)
        if not state:
            raise ValueError("no readable OmniCLI run state in {}".format(folder))
        if not args.yes:
            title = args.title or str(state.get("goal") or "OmniCLI project").strip()
            print("Project: {}".format(folder))
            print("Title:   {}".format(title))
            if args.base:
                print("Base:    {}".format(args.base))
            if args.draft:
                print("Type:    draft")
            try:
                answer = input("Create this PR? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer not in ("y", "yes"):
                print("Pull request creation cancelled.")
                return 0
        result = create_pr(folder, state, base=args.base, title=args.title, body=args.body, draft=args.draft)
    except (ValueError, RuntimeError, OSError) as ex:
        print("Pull request error: {}".format(ex), file=sys.stderr)
        return 1
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode:
        print("Could not create pull request: {}".format(output or "gh exited with code {}".format(result.returncode)),
              file=sys.stderr)
        return result.returncode
    print(output or "Pull request created.")
    return 0
