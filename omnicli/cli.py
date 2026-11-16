"""
Command Line Interface for OmniCLI: The Universal CLI Orchestrator.
Connects all command-line tools on the device to complete projects from a single command.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from omnicli import __version__
from omnicli.engine import ExecutionEngine
from omnicli.models import Pipeline, Step
from omnicli.planner import ProjectPlanner
from omnicli.recipes import RecipeManager
from omnicli.scanner import CliScanner
from omnicli.ui import (
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
    print_banner,
    print_panel,
    print_table,
)


def handle_scan(args, scanner: CliScanner):
    """Scans device CLIs and prints an interactive inventory table."""
    force = getattr(args, "refresh", False)
    category_filter = getattr(args, "category", None)

    print(f"\n{BADGE_INFO} Scanning device command-line tools (refresh={force})...")
    tools_dict = scanner.scan(force_refresh=force)
    installed = scanner.get_installed()

    if category_filter:
        tools_list = [t for t in installed if t.category == category_filter]
        title = f"Installed CLIs in Category '{category_filter}' ({len(tools_list)})"
    else:
        tools_list = installed
        title = f"Discovered Installed CLIs on Device ({len(installed)} / {len(tools_dict)})"

    # Sort by category and name
    tools_list.sort(key=lambda t: (t.category, t.name))

    headers = ["Tool", "Category", "Version / Status", "Path"]
    rows = []
    for t in tools_list:
        v_str = t.version or "installed"
        if len(v_str) > 35:
            v_str = v_str[:32] + "..."
        p_str = t.path
        if len(p_str) > 35:
            p_str = "..." + p_str[-32:]
        rows.append([
            c_bold(c_cyan(t.name)),
            c_dim(t.category),
            v_str,
            c_dim(p_str),
        ])

    print_table(headers, rows, title=title)
    print(f"{c_green('✓')} All {len(installed)} CLIs are indexed and ready to orchestrate in pipelines.\n")


def handle_project(args, scanner: CliScanner):
    """Builds and executes an ordered project pipeline from a single high-level command or intent."""
    intent = " ".join(args.goal).strip()
    if not intent:
        print(f"{BADGE_WARN} Please provide a project goal or instruction.")
        print("Example: omnicli project 'create a react app with tailwind and git repo'")
        return 1

    project_name = getattr(args, "name", None)
    target_dir = getattr(args, "dir", None) or os.getcwd()
    auto_approve = getattr(args, "yes", False)

    print(f"\n{BADGE_INFO} Analyzing single project command: {c_bold(c_cyan(repr(intent)))}")
    planner = ProjectPlanner(scanner=scanner)
    pipeline = planner.plan_project_intent(intent, project_name=project_name, target_dir=target_dir)

    # Print plan outline
    print(f"\n{c_bold('Execution Plan for Project:')} {c_green(pipeline.name)}")
    print(f"{c_dim('Description:')} {pipeline.description}")
    print(f"{c_dim('Target Directory:')} {pipeline.working_dir or target_dir}\n")

    headers = ["Step #", "CLI Tool", "Command", "Description"]
    rows = []
    for idx, s in enumerate(pipeline.steps, start=1):
        cmd_prev = s.command
        if len(cmd_prev) > 45:
            cmd_prev = cmd_prev[:42] + "..."
        rows.append([
            str(idx),
            c_bold(c_magenta(s.cli)),
            cmd_prev,
            s.description or s.name,
        ])
    print_table(headers, rows, title="Ordered CLI Execution Sequence")

    # Prompt user unless --yes
    if not auto_approve:
        try:
            choice = input(f"{c_bold(c_yellow('Execute this multi-CLI project plan now? [Y/n]: '))}").strip().lower()
            if choice and choice not in ["y", "yes"]:
                print(f"{BADGE_INFO} Execution cancelled by user.")
                return 0
        except KeyboardInterrupt:
            print(f"\n{BADGE_INFO} Execution cancelled.")
            return 0

    engine = ExecutionEngine(scanner=scanner)
    result = engine.run(pipeline, working_dir=pipeline.working_dir or target_dir, parallel=getattr(args, "parallel", False))
    return 0 if result.success else 1


def handle_chain(args, scanner: CliScanner):
    """Runs commands in sequential order passing outputs dynamically."""
    chain_parts = args.commands
    # Join if multiple
    chain_str = " ".join(chain_parts) if isinstance(chain_parts, list) else str(chain_parts)

    print(f"\n{BADGE_INFO} Orchestrating CLI chain: {c_cyan(chain_str)}")
    planner = ProjectPlanner(scanner=scanner)
    pipeline = planner.parse_chain_notation(chain_str)

    engine = ExecutionEngine(scanner=scanner)
    result = engine.run(pipeline)
    return 0 if result.success else 1


def handle_run(args, scanner: CliScanner):
    """Runs a pipeline definition file (JSON/YAML/TOML)."""
    file_path = Path(args.file).resolve()
    if not file_path.exists():
        print(f"{c_red('Error:')} Pipeline file not found: {file_path}")
        return 1

    try:
        content = file_path.read_text(encoding="utf-8")
        if file_path.suffix in [".json"]:
            data = json.loads(content)
        elif file_path.suffix in [".toml"]:
            import tomllib
            data = tomllib.loads(content)
        else:
            # Fallback json parse
            data = json.loads(content)
        pipeline = Pipeline.from_dict(data)
    except Exception as ex:
        print(f"{c_red('Error:')} Failed to parse pipeline file: {ex}")
        return 1

    engine = ExecutionEngine(scanner=scanner)
    result = engine.run(pipeline, working_dir=args.dir, parallel=getattr(args, "parallel", False))
    return 0 if result.success else 1


def handle_recipe(args, scanner: CliScanner):
    """Manages project recipes."""
    rm = RecipeManager()
    action = args.recipe_action

    if action == "list":
        recipes = rm.list_recipes()
        headers = ["Recipe Name", "Type", "Steps", "Description"]
        rows = []
        for r in recipes:
            rows.append([
                c_bold(c_cyan(r["name"])),
                c_dim(r["type"]),
                str(r["steps_count"]),
                r["description"],
            ])
        print_table(headers, rows, title="Available Project Recipes")
        print("To run a recipe, use: omnicli recipe run <name>")
        return 0

    elif action == "run":
        name = args.name
        pipeline = rm.get_pipeline(name)
        if not pipeline:
            print(f"{c_red('Error:')} Recipe '{name}' not found. Run 'omnicli recipe list' to see options.")
            return 1

        print(f"\n{BADGE_INFO} Running recipe: {c_bold(c_cyan(pipeline.name))}")
        engine = ExecutionEngine(scanner=scanner)
        result = engine.run(pipeline, working_dir=args.dir, parallel=getattr(args, "parallel", False))
        return 0 if result.success else 1

    return 0


def _slugify(text: str, words: int = 4) -> str:
    import re
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"a", "an", "the", "with", "and", "for", "to", "of", "in", "on", "build", "create", "make", "me"}
    picked = [t for t in tokens if t not in stop][:words] or ["project"]
    return "-".join(picked)


def handle_team(args, scanner: CliScanner):
    """One command -> a lead AI CLI plans the project and directs the other AI CLIs until it is complete."""
    from omnicli import conductor as conductor_mod
    from omnicli import runs, settings

    want_json = bool(getattr(args, "json", False))

    def fail(msg: str, target=None, code: int = 1) -> int:
        print(msg)
        if want_json:
            import re
            plain = re.sub(r"\x1b\[[0-9;]*m", "", msg)
            print(json.dumps({"dir": str(target) if target else None, "phase": None, "complete": False,
                              "exit_code": code,
                              "tasks": {"done": 0, "failed": 0, "skipped": 0, "pending": 0, "total": 0},
                              "report": None, "calls": 0, "minutes": 0.0, "error": plain}), flush=True)
        return code

    goal = " ".join(args.goal or []).strip()
    resume = bool(args.resume)
    if getattr(args, "continue_run", False):
        if goal:
            return fail(f"{BADGE_WARN} -c/--continue resumes your most recent unfinished run and takes no goal. "
                        "Start a new run without -c, or pick a run with: omnicli resume")
        resume = True
        if not args.dir:
            rec = runs.most_recent_run(unfinished_only=True)
            if not rec:
                return fail(f"{BADGE_INFO} No unfinished team run to continue. Start one with: "
                            'omnicli team "<goal>" -d DIR')
            if rec.get("alive"):
                return fail(f"{BADGE_WARN} The most recent run ({rec['dir']}) is still running in process "
                            f"{rec.get('pid')}. Check it with: omnicli status", rec["dir"])
            args.dir = rec["dir"]
            print(f"{BADGE_INFO} Continuing {c_bold(rec.get('project_name') or Path(rec['dir']).name)} "
                  f"({rec.get('phase', '?')})")

    if not goal and not resume:
        return fail(f"{BADGE_WARN} Give the team a goal, e.g.:\n"
                    '  omnicli team "build a todo web app with a FastAPI backend, SQLite and tests" -d ./todo-app')

    if args.dir:
        target = Path(args.dir).expanduser().resolve()
    elif resume:
        target = Path.cwd().resolve()
    else:
        target = (Path.cwd() / _slugify(goal)).resolve()

    if target in (Path.home().resolve(), Path("/").resolve()):
        return fail(f"{c_red('Refusing to run:')} {target} is your home/root directory. Point --dir at a project folder.",
                    target)

    from omnicli.teamopts import build_team_config

    name = (getattr(args, "name", None) or "").strip() or None
    try:
        cfg = build_team_config(args, goal=goal, project_dir=target, resume=resume, project_name=name)
    except ValueError as ex:
        return fail(f"{c_red('Error:')} {ex}", target)
    print(f"{BADGE_INFO} Project directory: {c_bold(str(target))}")
    team = conductor_mod.Conductor(cfg)
    rc = team.run()
    if want_json:  # exactly one JSON object, always the last stdout line
        print(json.dumps(team.result_summary(rc)), flush=True)
    return rc

KNOWN_SUBCOMMANDS = {
    "project", "plan", "start",
    "chain", "pipe",
    "scan", "inventory", "list-clis",
    "run", "exec",
    "recipe",
    "team", "crew", "conduct",
}


def _known_subcommands() -> set:
    from omnicli.commands import command_names
    return KNOWN_SUBCOMMANDS | command_names()


def main(argv: Optional[List[str]] = None) -> int:
    args_list = sys.argv[1:] if argv is None else argv

    # Transparent Normal CLI Passthrough Mode:
    # If the user runs `omnicli git status` or `omnicli npm test` or `omnicli docker ps`,
    # execute it directly as a normal CLI command with OmniCLI tracking and metrics.
    if args_list and not args_list[0].startswith("-"):
        first_token = args_list[0]
        known = _known_subcommands()
        if first_token not in known:
            # A typo of an omnicli command (e.g. `omnicli ussage`) - not a real program to pass through
            import difflib
            import shutil
            if not shutil.which(first_token) and "->" not in " ".join(args_list):
                guess = difflib.get_close_matches(first_token.lower(), sorted(known), n=1, cutoff=0.7)
                if guess:
                    print(f"{c_red('Unknown command:')} {first_token}. Did you mean: omnicli {guess[0]}"
                          + (" " + " ".join(args_list[1:]) if args_list[1:] else ""))
                    return 2
            scanner = CliScanner()
            full_cmd = " ".join(args_list)
            if "->" in full_cmd:
                return handle_chain(argparse.Namespace(commands=args_list), scanner)
            else:
                engine = ExecutionEngine(scanner=scanner)
                pipe = Pipeline(
                    name="direct-run",
                    description=f"Direct execution of '{full_cmd}'",
                    steps=[
                        Step(
                            id=f"run_{first_token}",
                            name=f"Run {first_token}",
                            cli=first_token,
                            command=full_cmd,
                            description=f"Execute '{full_cmd}'",
                        )
                    ],
                )
                res = engine.run(pipe)
                return 0 if res.success else (res.step_results.get(f"run_{first_token}").exit_code if res.step_results else 1)

    parser = argparse.ArgumentParser(
        prog="omnicli",
        description="OmniCLI: Connect all command-line tools on your device to complete projects.",
    )
    parser.add_argument("--version", action="version", version=f"omnicli v{__version__}")
    parser.add_argument("--no-banner", action="store_true", help="Suppress the welcome banner")

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # 1. Project command (Single command -> full project orchestration)
    p_proj = subparsers.add_parser(
        "project",
        aliases=["plan", "start"],
        help="Give 1 project command/goal and orchestrate all necessary CLIs to complete it",
    )
    p_proj.add_argument("goal", nargs="+", help="Natural description or goal for the project")
    p_proj.add_argument("-n", "--name", help="Name of the project")
    p_proj.add_argument("-d", "--dir", help="Target directory for the project")
    p_proj.add_argument("-y", "--yes", action="store_true", help="Auto-approve execution without confirmation")
    p_proj.add_argument("-p", "--parallel", action="store_true", help="Execute independent steps concurrently in parallel")

    # 2. Chain command (Inline sequential pipeline)
    p_chain = subparsers.add_parser(
        "chain",
        aliases=["pipe"],
        help="Chain multiple CLIs sequentially with output passing (e.g., 'cmd1 -> cmd2 {prev}')",
    )
    p_chain.add_argument("commands", nargs="+", help="Commands separated by '->'")

    # 3. Scan command (CLI discovery)
    p_scan = subparsers.add_parser(
        "scan",
        aliases=["inventory", "list-clis"],
        help="Scan device and display all discovered CLI tools and their health",
    )
    p_scan.add_argument("-r", "--refresh", action="store_true", help="Force re-scan and update cache")
    p_scan.add_argument("-c", "--category", help="Filter by category (scm, package_manager, runtime, container, build, etc.)")

    # 4. Run command (Execute pipeline file)
    p_run = subparsers.add_parser(
        "run",
        aliases=["exec"],
        help="Run a declarative multi-CLI pipeline configuration file (JSON/TOML)",
    )
    p_run.add_argument("file", help="Path to pipeline file")
    p_run.add_argument("-d", "--dir", help="Working directory")
    p_run.add_argument("-p", "--parallel", action="store_true", help="Execute independent steps concurrently in parallel")

    # 5. Recipe command
    p_rec = subparsers.add_parser("recipe", help="Manage and run multi-CLI project recipes")
    p_rec_subs = p_rec.add_subparsers(dest="recipe_action", required=True)
    p_rec_subs.add_parser("list", help="List all built-in and custom recipes")
    p_rec_run = p_rec_subs.add_parser("run", help="Execute a recipe")
    p_rec_run.add_argument("name", help="Name of recipe to run")
    p_rec_run.add_argument("-d", "--dir", help="Target working directory")
    p_rec_run.add_argument("-p", "--parallel", action="store_true", help="Execute independent steps concurrently in parallel")

    # 6. Team command (lead AI CLI directs the other AI CLIs)
    p_team = subparsers.add_parser(
        "team",
        aliases=["crew", "conduct"],
        help="Give 1 command: a lead AI CLI plans the project and directs the other AI CLIs until it is complete",
    )
    p_team.add_argument("goal", nargs="*", help="What to build (one sentence or a paragraph)")
    p_team.add_argument("-d", "--dir", help="Project directory (default: a new folder named after the goal)")
    p_team.add_argument("--plan-only", action="store_true", help="Only make and save the plan; run it later with `omnicli resume`")
    p_team.add_argument("--resume", action="store_true", help="Continue the saved team run in --dir (or current dir)")
    p_team.add_argument("-c", "--continue", dest="continue_run", action="store_true",
                        help="Resume your most recent unfinished team run (no goal or --dir needed)")
    p_team.add_argument("--json", action="store_true",
                        help="After the run, print one JSON summary object as the last line of output")
    from omnicli.teamopts import add_run_options
    add_run_options(p_team)
    p_team.add_argument("--name", help="Project name shown in runs/status/report (default: the lead's choice)")

    # 7+. Pluggable commands: agents, usage, runs/resume/continue, status, logs, doctor,
    #     config/init, undo, export, review  (see omnicli/commands/__init__.py)
    from omnicli.commands import register_all
    register_all(subparsers)

    args = parser.parse_args(argv)

    # The banner is for people: never print it into pipes, files or --json output
    # (`omnicli export > run.md`, `$(omnicli config get lead)`, `omnicli status --json | jq`).
    if (not args.no_banner and args.subcommand != "--version" and sys.stdout.isatty()
            and not getattr(args, "json", False)):
        print_banner()

    if not args.subcommand:
        # Like `claude`: plain `omnicli` in a terminal opens the chat; anything else gets the help.
        if sys.stdin.isatty() and sys.stdout.isatty():
            from omnicli.commands import chat
            return chat.run(argparse.Namespace(dir=None, lead=None))
        parser.print_help()
        return 0

    scanner = CliScanner()

    # Route subcommands (including aliases)
    sub = args.subcommand
    if sub in ["project", "plan", "start"]:
        return handle_project(args, scanner)
    elif sub in ["chain", "pipe"]:
        return handle_chain(args, scanner)
    elif sub in ["scan", "inventory", "list-clis"]:
        return handle_scan(args, scanner)
    elif sub in ["run", "exec"]:
        return handle_run(args, scanner)
    elif sub == "recipe":
        return handle_recipe(args, scanner)
    elif sub in ["team", "crew", "conduct"]:
        return handle_team(args, scanner)
    elif getattr(args, "func", None) is not None:
        return int(args.func(args) or 0)

    return 0


if __name__ == "__main__":
    sys.exit(main())
