"""
`omnicli agents` - list the AI CLIs OmniCLI can direct and check they are logged in.

This module is also the reference example of the command-module contract
(see omnicli/commands/__init__.py).
"""

from omnicli import agents as ag
from omnicli import paths, settings
from omnicli.ui import BADGE_INFO, c_bold, c_cyan, c_dim, c_green, c_red, print_table

NAMES = ["agents"]


def add_parser(subparsers) -> None:
    p = subparsers.add_parser("agents", help="List AI CLIs OmniCLI can direct and check they are logged in")
    p.add_argument("--check", action="store_true", help="Force a fresh health ping")
    p.set_defaults(func=run_list)


def run_list(args) -> int:
    specs = ag.load_agent_specs()
    print(f"\n{BADGE_INFO} Pinging AI CLIs headlessly (cached successes are reused; --check forces a fresh ping)...")
    health = ag.check_health(specs, force=getattr(args, "check", False))
    rows = []
    ready = [n for n in [x for x in ag.PREFERRED_LEAD_ORDER if x in specs] + [x for x in specs if x not in ag.PREFERRED_LEAD_ORDER]
             if health.get(n, {}).get("ok")]
    saved_lead = settings.get("lead")
    lead = saved_lead if saved_lead in ready else (ready[0] if ready else None)
    for name, spec in specs.items():
        h = health.get(name, {})
        if not h.get("installed"):
            status = c_dim("not installed")
        elif h.get("ok"):
            status = c_green("ready") + (c_dim(" (cached)") if h.get("cached") else "")
        else:
            status = c_red("unavailable")
        role = ("lead" if saved_lead == name else "lead (auto)") if name == lead else ""
        workers = settings.get("workers")
        if h.get("ok") and (not workers or name in workers):
            role = (role + " + worker") if role else "worker"
        model = settings.get(f"models.{name}") or ("default" if spec.get("model_args") else "-")
        effort = settings.get(f"effort.{name}")
        rows.append([c_bold(c_cyan(name)), spec.get("name", name), status, role or c_dim("-"),
                     model + (f" / {effort}" if effort else ""), c_dim(h.get("reason", "")[:40])])
    print_table(["Agent", "CLI", "Status", "Role", "Model / effort", "Detail"], rows,
                title="AI agents OmniCLI can direct")
    print(c_dim("Choose lead, workers, models and effort with: omnicli setup   (list models: omnicli models)"))
    print(c_dim(f"Customise adapters or strengths in {paths.agents_config_path()}"))
    return 0
