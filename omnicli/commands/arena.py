"""Blind, isolated bake-offs for one saved team task."""

import datetime
import json
import random
import subprocess
import time
from pathlib import Path

from omnicli import agents, runs
from omnicli.conductor import MAX_DIFF_CHARS, TeamConfig, build_worker_prompt, verify_passed
from omnicli.ui import BADGE_FAILED, BADGE_INFO, BADGE_WARN, c_bold, c_cyan

NAMES = ["arena"]


def add_parser(subparsers):
    parser = subparsers.add_parser("arena", help="Blindly compare 2-3 agents on a saved task")
    parser.add_argument("ref", help="Saved run index, project path, or name")
    parser.add_argument("task", help="Task id to try, such as t3")
    parser.add_argument("--agents", required=True, help="Two or three comma-separated agent ids")
    parser.add_argument("--lead", help="Agent to choose the winner (default: saved run lead)")
    parser.set_defaults(func=handler)


def blind_labels(names, rng=None):
    labels = [chr(ord("A") + index) for index in range(len(names))]
    pairs = list(zip(labels, names))
    (rng or random.SystemRandom()).shuffle(pairs)
    return pairs


def anonymize_candidates(results, rng=None):
    labels = blind_labels([row["agent"] for row in results], rng)
    by_agent = {name: label for label, name in labels}
    return labels, {by_agent[row["agent"]]: row for row in results}


def _git(root, *args, **kwargs):
    return subprocess.run(["git"] + list(args), cwd=str(root), capture_output=True, text=True, **kwargs)


def _task_base(root, tasks, index):
    from omnicli.gitutil import safe_rev
    task = tasks[index]
    if safe_rev(task.get("commit")):
        result = _git(root, "rev-parse", "--verify", safe_rev(task["commit"]) + "^")
        if result.returncode == 0:
            return result.stdout.strip()
    for earlier in reversed(tasks[:index]):
        if safe_rev(earlier.get("commit")):
            return safe_rev(earlier["commit"])
    result = _git(root, "rev-parse", "HEAD")
    if result.returncode:
        raise ValueError("arena requires a Git project with at least one commit")
    return result.stdout.strip()


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
    from omnicli.conductor import Task
    agents_task = Task.from_dict(state["tasks"][task_index])
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
    base = _task_base(root, state["tasks"], task_index)

    from omnicli.conductor import Conductor
    cfg = TeamConfig(goal=state.get("goal", ""), project_dir=project, lead=lead or state.get("lead"),
                     workers=list(agent_names), auto_approve=True, use_git=False)
    conductor = Conductor(cfg)
    if not conductor.select_team():
        raise ValueError("no usable AI CLI is available to run the arena")
    missing = [name for name in agent_names if name not in conductor.healthy]
    if missing:
        raise ValueError("arena agents are unavailable: {}".format(", ".join(missing)))

    arena_root = root / ".omnicli" / "team" / "arena" / str(int(time.time()))
    arena_root.mkdir(parents=True, exist_ok=False)
    results = []
    try:
        for index, name in enumerate(agent_names):
            worktree = arena_root / ("candidate-{}".format(index + 1))
            added = _git(root, "worktree", "add", "--detach", str(worktree), base)
            if added.returncode:
                raise ValueError("could not create arena worktree: {}".format((added.stderr or added.stdout).strip()))
            workspace = worktree / project_subdir
            prompt = build_worker_prompt(agents_task, state.get("goal", ""), workspace, "Anonymous arena participant",
                                         conductor.lead, [agents_task])
            if not conductor._reserve_call():
                results.append({"agent": name, "ok": False, "verify": None,
                                "error": "session budget reached before this candidate"})
                continue
            spec = conductor.specs[name]
            run = agents.run_agent(spec, prompt, "edit", str(workspace),
                                   timeout=cfg.task_timeout_min * 60,
                                   log_path=conductor.logs / "arena-{}-{}.md".format(task_id, index + 1),
                                   meta={"kind": "task", "run_dir": str(project), "task_id": task_id})
            verify = conductor.run_verify(agents_task, workspace) if run.ok else None
            add = _git(worktree, "add", "-A")
            diff = _git(worktree, "diff", "--binary", base, "--") if add.returncode == 0 else None
            stat = _git(worktree, "diff", "--stat", base, "--") if add.returncode == 0 else None
            results.append({"agent": name, "ok": run.ok, "verify": verify, "error": run.failure_reason() if not run.ok else "",
                            "diff": (diff.stdout if diff and diff.returncode == 0 else ""),
                            "summary": (stat.stdout if stat and stat.returncode == 0 else "")})

        ready = [row for row in results if row["ok"]]
        if not ready:
            raise ValueError("no arena candidate completed successfully")
        labels, blind = anonymize_candidates(ready)
        prompt_rows = []
        for label, row in blind.items():
            verification = "not configured" if row["verify"] is None else ("PASS" if verify_passed(row["verify"]) else
                                                                           "FAIL: " + str(row["verify"][1])[:1500])
            prompt_rows.append("CANDIDATE {}\nVerify: {}\nChanges:\n{}\nDiff:\n{}".format(
                label, verification, row["summary"][:2000], row["diff"][:MAX_DIFF_CHARS]))
        lead_prompt = """[OmniCLI blind arena decision]
Several independent implementations of the same task were built in isolated copies. The candidates are
anonymized; do not guess who wrote them. Choose the best implementation based on correctness, maintainability,
and the listed verification result. Prefer a passing candidate. If none pass, return winner as an empty string.
Reply only with JSON: {{"winner":"A","reason":"short explanation"}}.

TASK: {} — {}
{}
""".format(task_id, agents_task.title, "\n\n".join(prompt_rows))
        decision = conductor.ask_lead("arena", lead_prompt, "winner")
        chosen_label = str((decision or {}).get("winner", "")).strip().upper()
        chosen = blind.get(chosen_label)
        if not chosen:
            raise ValueError("the lead did not select a valid passing arena candidate")
        if chosen["verify"] is not None and not verify_passed(chosen["verify"]):
            raise ValueError("the lead selected a candidate whose verification failed; no changes were applied")
        winning_label = next(label for label, name in labels if name == chosen["agent"])
        patch = chosen["diff"]
        if patch:
            applied = _git(root, "apply", "--index", "--binary", "-", input=patch)
            if applied.returncode:
                raise ValueError("could not apply the winning patch: {}".format((applied.stderr or applied.stdout).strip()))
        result_dir = project / ".omnicli" / "team" / "arena-results"
        result_dir.mkdir(parents=True, exist_ok=True)
        record = {"task": task_id, "base": base, "winner": chosen["agent"], "blind_label": winning_label,
                  "reason": (decision or {}).get("reason", ""),
                  "candidates": [{"label": label, "agent": row["agent"], "ok": row["ok"],
                                  "verify_exit": row["verify"][0] if row["verify"] else None,
                                  "summary": row["summary"][:2000]} for label, row in labels]}
        (result_dir / "{}.json".format(datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))).write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        return record
    finally:
        for index in range(len(agent_names)):
            worktree = arena_root / ("candidate-{}".format(index + 1))
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
