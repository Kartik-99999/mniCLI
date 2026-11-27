"""Blind, isolated bake-offs for one saved team task."""

import datetime
import json
import random
import subprocess
import time
from pathlib import Path

from omnicli import agents, runs, settings, workflows
from omnicli.conductor import MAX_DIFF_CHARS, Task, TeamConfig, build_worker_prompt
from omnicli.ui import BADGE_INFO, c_bold, c_cyan

NAMES = ["arena"]


def add_parser(subparsers):
    parser = subparsers.add_parser("arena", help="Blindly compare 2-3 agents on a saved task")
    parser.add_argument("ref", help="Saved run index, project path, or name")
    parser.add_argument("task", help="Task id to try, such as t3")
    parser.add_argument("--agents", required=True, help="Two or three comma-separated agent ids")
    parser.add_argument("--lead", help="Agent to choose the winner (default: saved run lead)")
    parser.set_defaults(func=handler)


def blind_labels(names, rng=None):
    shuffled = list(names)
    (rng or random.SystemRandom()).shuffle(shuffled)
    return [(chr(ord("A") + index), name) for index, name in enumerate(shuffled)]


def anonymize_candidates(results, rng=None):
    labels = blind_labels([row["agent"] for row in results], rng)
    by_agent = {row["agent"]: row for row in results}
    return labels, {label: by_agent[name] for label, name in labels}


def _git(root, *args, **kwargs):
    return subprocess.run(["git"] + list(args), cwd=str(root), capture_output=True, text=True, **kwargs)


def run_arena(ref, task_id, agent_names, lead=None):
    if len(agent_names) not in (2, 3) or len(set(agent_names)) != len(agent_names):
        raise ValueError("arena needs two or three different agent ids")
    rec = runs.find_run(ref)
    if not rec:
        raise ValueError("no run matches {!r}; see `omnicli runs`".format(ref))
    project = Path(rec["dir"]).resolve()
    state = runs.load_state(project)
    if not state or not isinstance(state.get("tasks"), list):
        raise ValueError("the selected run has no readable task plan")
    task_index = next((i for i, row in enumerate(state["tasks"]) if row.get("id") == task_id), None)
    if task_index is None:
        raise ValueError("task {!r} is not in this run".format(task_id))
    tasks = [Task.from_dict(row) for row in state["tasks"]]
    agents_task = tasks[task_index]
    journeys = workflows.from_plan(state, tasks) if state.get("workflows") else []
    previous = workflows.interaction_commands(journeys, [t for t in tasks if t.status in ("done", "failed")])
    checks = [cmd for cmd in previous if cmd != agents_task.verify]
    root_result = _git(project, "rev-parse", "--show-toplevel")
    if root_result.returncode:
        raise ValueError("arena requires the project to be inside a Git repository")
    root = Path(root_result.stdout.strip()).resolve()
    try:
        project_subdir = project.relative_to(root)
    except ValueError:
        project_subdir = Path(".")
    status = _git(root, "status", "--porcelain")
    if status.returncode or status.stdout.strip():
        raise ValueError("the Git project must have a clean working tree before an arena; checkpoint or stash changes first")
    from omnicli.gitutil import safe_rev
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode:
        raise ValueError("arena requires a Git project with at least one commit")
    head_id = head.stdout.strip()
    # Candidates start from the CURRENT project with the task's own checkpoint undone, so the state
    # they build and verify is exactly the state that is applied if they win.
    task_commit = safe_rev(state["tasks"][task_index].get("commit"))

    from omnicli.conductor import Conductor
    cfg = TeamConfig(goal=state.get("goal", ""), project_dir=project, lead=lead or state.get("lead"),
                     workers=list(agent_names), auto_approve=True, use_git=False,
                     models=settings.get("models"), lead_models=settings.get("lead_models"),
                     effort=settings.get("effort"), lead_effort=settings.get("lead_effort"))
    conductor = Conductor(cfg)
    if not conductor.select_team():
        raise ValueError("no usable AI CLI is available to run the arena")
    missing = [name for name in agent_names if name not in conductor.healthy]
    if missing:
        raise ValueError("arena agents are unavailable: {}".format(", ".join(missing)))

    arena_root = root / ".omnicli" / "team" / "arena" / str(int(time.time()))
    arena_root.mkdir(parents=True, exist_ok=False)
    results = []
    preserve = []
    try:
        if checks:
            baseline = arena_root / "baseline"
            added = _git(root, "worktree", "add", "--detach", str(baseline), head_id)
            if added.returncode:
                raise ValueError("could not create the arena baseline worktree")
            issues = conductor.workflow_issues(checks, baseline / project_subdir)
            preserve = [cmd for cmd in checks if cmd not in issues]
        for index, name in enumerate(agent_names):
            worktree = arena_root / ("candidate-{}".format(index + 1))
            added = _git(root, "worktree", "add", "--detach", str(worktree), head_id)
            if added.returncode:
                raise ValueError("could not create arena worktree: {}".format((added.stderr or added.stdout).strip()))
            workspace = worktree / project_subdir
            if task_commit:
                undone = _git(worktree, "revert", "--no-commit", task_commit)
                if undone.returncode:
                    _git(worktree, "revert", "--abort")
                    raise ValueError("can't redo {}: later work changed the same lines it wrote, so it can't be undone "
                                     "cleanly. Undo those later tasks first (omnicli undo) or start a new task."
                                     .format(task_id))
            start_tree = _git(worktree, "write-tree")
            if start_tree.returncode:
                raise ValueError("could not snapshot the arena starting point")
            base = start_tree.stdout.strip()
            prompt = build_worker_prompt(agents_task, state.get("goal", ""), workspace, "Anonymous arena participant",
                                         conductor.lead, tasks, plan_summary=state.get("plan_summary", ""),
                                         user_workflows=journeys)
            if not conductor._reserve_call():
                results.append({"agent": name, "ok": False, "verify": None,
                                "error": "session budget reached before this candidate"})
                continue
            spec = conductor.specs[name]
            run = agents.run_agent(spec, prompt, "edit", str(workspace),
                                   timeout=cfg.task_timeout_min * 60,
                                   log_path=conductor.logs / "arena-{}-{}.md".format(task_id, index + 1),
                                   meta={"kind": "task", "run_dir": str(project), "task_id": task_id})
            # Freeze the proposal before running the checks so their generated files never enter the patch.
            add = _git(worktree, "add", "-A")
            tree = _git(worktree, "write-tree") if add.returncode == 0 else None
            tree_id = tree.stdout.strip() if tree and tree.returncode == 0 else ""
            verify = conductor.run_verify(agents_task, workspace) if run.ok else None
            issues = conductor.workflow_issues(checks, workspace) if run.ok else {}
            passed = [cmd for cmd in checks if cmd not in issues] if run.ok else []
            if agents_task.verify:
                problem = workflows.check_problem(agents_task.verify, verify)
                if problem:
                    issues[agents_task.verify] = problem
                else:
                    passed.append(agents_task.verify)
            diff = _git(worktree, "diff", "--binary", base, tree_id) if tree_id else None
            stat = _git(worktree, "diff", "--stat", base, tree_id) if tree_id else None
            results.append({"agent": name, "ok": run.ok and bool(tree_id), "verify": verify, "issues": issues,
                            "passed": passed,
                            "error": run.failure_reason() if not run.ok else ("" if tree_id else "could not snapshot"),
                            "diff": (diff.stdout if diff and diff.returncode == 0 else ""),
                            "summary": (stat.stdout if stat and stat.returncode == 0 else ""),
                            "tree": tree_id})

        _, blind = anonymize_candidates(results)
        required = set(preserve)
        if agents_task.verify:
            required.add(agents_task.verify)
        eligible = workflows.strongest_candidates(blind, required)
        if not eligible:
            reasons = ["{}: {}".format(label, row.get("error") or str(row.get("issues")))
                       for label, row in blind.items()]
            raise ValueError("no eligible arena candidate; " + " | ".join(reasons))
        prompt_rows = []
        for label, row in eligible.items():
            verification = "Passed: {}\nOther check results: {}".format(
                ", ".join(row["passed"]) or "(none)", row["issues"] or "(none)")
            prompt_rows.append("CANDIDATE {}\nVerify: {}\nChanges:\n{}\nDiff:\n{}".format(
                label, verification, row["summary"][:2000], row["diff"][:MAX_DIFF_CHARS]))
        lead_prompt = """[OmniCLI blind arena decision]
These candidates survived the executable checks. Compare them against the user's goal and acceptance criteria.
Use specific evidence in the diff or check results to explain your choice. For an editor, consider what a person
can create, edit, save and reopen, and whether the controls are discoverable. Consider edge cases and the cost
of maintaining the implementation. If none meet the task, return winner as an empty string.
Reply only with JSON: {{"winner":"A","reason":"short explanation"}}.

GOAL: {}
TASK: {} — {}
INSTRUCTIONS: {}
ACCEPTANCE: {}
USER WORKFLOWS:
{}
{}
""".format(cfg.goal, task_id, agents_task.title, agents_task.instructions, agents_task.acceptance,
           workflows.describe(journeys), "\n\n".join(prompt_rows))
        if len(eligible) == 1 and next(iter(eligible.values()))["passed"]:
            decision = {"winner": next(iter(eligible)), "reason": "Strongest coverage of verified behaviors."}
        else:
            decision = conductor.ask_lead("arena", lead_prompt, "winner")
        chosen_label = str((decision or {}).get("winner", "")).strip().upper()
        chosen = eligible.get(chosen_label)
        if not chosen:
            raise ValueError("the lead did not select a valid passing arena candidate")
        # The project must still be where the arena started, then it becomes exactly the winner's tree.
        now_head = _git(root, "rev-parse", "--verify", "HEAD").stdout.strip()
        now_status = _git(root, "status", "--porcelain")
        if now_head != head_id or now_status.returncode or now_status.stdout.strip():
            raise ValueError("the project changed while the arena ran; nothing was applied. Run the arena again.")
        made = _git(root, "diff", "--binary", "HEAD", chosen["tree"])
        if made.returncode:
            raise ValueError("could not prepare the winning changes: {}".format((made.stderr or made.stdout).strip()))
        patch = made.stdout
        if patch:
            applied = _git(root, "apply", "--index", "--binary", "-", input=patch)
            if applied.returncode:
                raise ValueError("could not apply the winning patch: {}".format((applied.stderr or applied.stdout).strip()))
        result_dir = project / ".omnicli" / "team" / "arena-results"
        result_dir.mkdir(parents=True, exist_ok=True)
        record = {"task": task_id, "base": head_id, "undid_task_commit": task_commit or "",
                  "winner": chosen["agent"], "blind_label": chosen_label, "winner_tree": chosen["tree"],
                  "preserved_checks": preserve,
                  "reason": (decision or {}).get("reason", ""),
                  "candidates": [{"label": label, "agent": row["agent"], "ok": row["ok"],
                                  "verify_exit": row["verify"][0] if row["verify"] else None,
                                  "shortlisted": label in eligible, "passed": row.get("passed", []),
                                  "issues": row.get("issues", {}),
                                  "error": row.get("error", ""), "summary": row.get("summary", "")[:2000]}
                                 for label, row in blind.items()]}
        (result_dir / "{}.json".format(datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))).write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        return record
    finally:
        for name in ["baseline"] + ["candidate-{}".format(i + 1) for i in range(len(agent_names))]:
            worktree = arena_root / name
            if worktree.exists():
                _git(root, "worktree", "remove", "--force", str(worktree))


def handler(args):
    names = [name.strip().lower() for name in args.agents.split(",") if name.strip()]
    try:
        result = run_arena(args.ref, args.task, names, getattr(args, "lead", None))
    except (ValueError, OSError) as ex:
        print("Arena error: {}".format(ex))
        return 1
    print("{} Candidate {} won the blind bake-off; its changes are staged in the project.".format(
        c_bold(BADGE_INFO), c_cyan(result["blind_label"])))
    print("Winner: {}. Reason: {}".format(result["winner"], result.get("reason") or "the lead preferred this implementation"))
    print("Run `git diff --cached` to inspect the selected patch before committing it.")
    return 0
