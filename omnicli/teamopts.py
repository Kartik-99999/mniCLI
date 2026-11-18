"""
Options shared by every command that runs the team: `omnicli team`, `omnicli resume`,
`omnicli continue` (and anything added later).

One place defines the flags and one place maps flags + saved settings
(~/.omnicli/config.json, see omnicli/settings.py) onto a TeamConfig, so those commands
can never drift apart. An explicit flag always wins over a saved setting.
"""

from pathlib import Path
from typing import Optional

from omnicli import settings


def add_run_options(p) -> None:
    """Flags with the same meaning on team / resume / continue."""
    p.add_argument("--lead", help="AI CLI that plans and reviews (default: `lead` setting, else best available)")
    p.add_argument("--workers", help="Comma-separated AI CLIs that do the work (default: `workers` setting, else all ready)")
    p.add_argument("-j", "--jobs", type=int, default=None, help="Tasks that may run at the same time (default 2)")
    p.add_argument("--rounds", type=int, default=None, help="Max work rounds incl. lead follow-ups (default 2)")
    p.add_argument("--retries", type=int, default=None, help="Revision attempts per task after a lead review (default 1)")
    p.add_argument("--max-tasks", type=int, default=None, help="Most tasks the lead may plan per round (default 10)")
    p.add_argument("--task-timeout", type=float, default=None, metavar="MIN",
                   help="Minutes before a worker is stopped (default 25)")
    p.add_argument("--no-review", action="store_true", help="Skip the lead's per-task review (verify commands still run)")
    p.add_argument("--no-git", action="store_true", help="Do not create git checkpoints after each task")
    p.add_argument("--recheck", action="store_true", help="Re-ping every AI CLI instead of using cached health")
    p.add_argument("-y", "--yes", action="store_true", help="Start without asking to confirm the plan")
    p.add_argument("--approve-tasks", action="store_true",
                   help="Preview and approve each lead-reviewed task before its checkpoint")
    p.add_argument("--notify", action="store_true", help="Send a desktop notification when the run finishes or pauses")
    p.add_argument("--worktrees", action="store_true",
                   help="Run each task in an isolated Git worktree and merge approved changes at checkpoints")
    p.add_argument("--cross-vendor-review", action="store_true",
                   help="Require a reviewer from a different AI provider than the task's worker")
    p.add_argument("--red-team", action="store_true",
                   help="Ask a different AI provider to add adversarial tests before task review")
    p.add_argument("--human", action="store_true",
                   help="Let the lead assign tasks to you; complete them in the shown workspace")
    p.add_argument("--model", action="append", metavar="AGENT=MODEL", default=None,
                   help="Worker model for one agent, e.g. --model claude=sonnet (repeatable; default: `models.<agent>` setting)")
    p.add_argument("--lead-model", action="append", metavar="AGENT=MODEL", default=None,
                   help="Lead model, e.g. --lead-model claude=opus (repeatable; default: `lead_models.<agent>`, then worker model)")
    p.add_argument("--effort", action="append", metavar="AGENT=LEVEL", default=None,
                   help="Reasoning effort for one agent, e.g. --effort agy=high (repeatable; default: `effort.<agent>`)")
    p.add_argument("--lead-effort", action="append", metavar="AGENT=LEVEL", default=None,
                   help="Lead reasoning effort, e.g. --lead-effort codex=xhigh (repeatable; default: compatible worker effort or CLI default)")
    p.add_argument("--max-calls", type=int, default=None, metavar="N",
                   help="Stop after N AI calls this session; progress is saved (0 = no limit)")
    p.add_argument("--max-minutes", type=float, default=None, metavar="M",
                   help="Stop after M minutes this session; progress is saved (0 = no limit)")
    p.add_argument("-q", "--quiet", action="store_true", help="Don't stream each agent's raw output (milestones still print)")
    p.add_argument("--add-dir", action="append", metavar="DIR", default=None,
                   help="Extra directory workers may use (repeatable; claude/codex/agy only)")


def build_team_config(args, goal: str, project_dir: Path, resume: bool, project_name: Optional[str] = None):
    """TeamConfig from parsed args + saved settings. Raises ValueError with a user-facing message."""
    from omnicli import conductor as c

    saved_models, saved_lead_models = settings.get("models"), settings.get("lead_models")
    saved_effort, saved_lead_effort = settings.get("effort"), settings.get("lead_effort")
    models = {str(k): str(v) for k, v in saved_models.items()} if isinstance(saved_models, dict) else {}
    lead_models = {str(k): str(v) for k, v in saved_lead_models.items()} if isinstance(saved_lead_models, dict) else {}
    effort = {str(k): str(v) for k, v in saved_effort.items()} if isinstance(saved_effort, dict) else {}
    lead_effort = {str(k): str(v) for k, v in saved_lead_effort.items()} if isinstance(saved_lead_effort, dict) else {}
    models.update(c.parse_agent_values(getattr(args, "model", None), "--model", "MODEL"))
    lead_models.update(c.parse_agent_values(getattr(args, "lead_model", None), "--lead-model", "MODEL"))
    effort.update(c.parse_agent_values(getattr(args, "effort", None), "--effort", "LEVEL"))
    lead_effort.update(c.parse_agent_values(getattr(args, "lead_effort", None), "--lead-effort", "LEVEL"))
    add_dirs = c.resolve_add_dirs(getattr(args, "add_dir", None))

    max_calls = int(settings.pick(getattr(args, "max_calls", None), "max_calls") or 0)
    max_minutes = float(settings.pick(getattr(args, "max_minutes", None), "max_minutes") or 0)
    if max_calls < 0 or max_minutes < 0:
        raise ValueError("--max-calls and --max-minutes must be 0 (no limit) or more")

    workers_arg = getattr(args, "workers", None)
    workers = [w.strip() for w in workers_arg.split(",") if w.strip()] if workers_arg else settings.get("workers")
    if workers:
        workers = ["human" if str(w).lower() in ("me", "human") else str(w).lower() for w in workers]
    human_worker = bool(getattr(args, "human", False) or (workers and "human" in workers))
    use_git = False if getattr(args, "no_git", False) else bool(settings.get("git"))
    use_worktrees = bool(getattr(args, "worktrees", False))
    if use_worktrees and not use_git:
        raise ValueError("--worktrees requires git checkpoints; remove --no-git and enable the git setting")
    return c.TeamConfig(
        goal=goal,
        project_dir=project_dir,
        lead=settings.pick(getattr(args, "lead", None), "lead"),
        workers=workers or None,
        jobs=max(1, int(settings.pick(getattr(args, "jobs", None), "jobs"))),
        max_rounds=max(1, int(settings.pick(getattr(args, "rounds", None), "rounds"))),
        retries=max(0, int(settings.pick(getattr(args, "retries", None), "retries"))),
        max_tasks=max(1, int(settings.pick(getattr(args, "max_tasks", None), "max_tasks"))),
        task_timeout_min=float(settings.pick(getattr(args, "task_timeout", None), "task_timeout")),
        lead_timeout_min=float(settings.get("lead_timeout")),
        review=False if getattr(args, "no_review", False) else bool(settings.get("review")),
        use_git=use_git,
        auto_approve=bool(getattr(args, "yes", False) or settings.get("auto_approve")),
        approve_tasks=bool(getattr(args, "approve_tasks", False)),
        notify=bool(getattr(args, "notify", False)),
        use_worktrees=use_worktrees,
        cross_vendor_review=bool(getattr(args, "cross_vendor_review", False)),
        red_team=bool(getattr(args, "red_team", False)),
        human_worker=human_worker,
        plan_only=bool(getattr(args, "plan_only", False)),
        resume=resume,
        recheck=bool(getattr(args, "recheck", False)),
        models=models,
        lead_models=lead_models,
        effort=effort,
        lead_effort=lead_effort,
        max_calls=max_calls,
        max_minutes=max_minutes,
        stream_output=not getattr(args, "quiet", False),
        add_dirs=add_dirs,
        project_name=project_name,
    )
