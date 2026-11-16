"""
`omnicli models` - which models each AI CLI can use, and which one OmniCLI will pick.
`omnicli setup` - interactive wizard: choose the lead, the workers, each AI's model and
                  effort, and team defaults. Everything is saved to ~/.omnicli/config.json.

The pick_* helpers are shared with chat (/lead /workers /model /effort /team /setup).
"""

import json
from typing import Callable, Dict, List, Optional

from omnicli import model_catalog, settings
from omnicli.picker import CANCEL, ask_number, choose_many, choose_one
from omnicli.ui import BADGE_INFO, BADGE_SUCCESS, BADGE_WARN, c_bold, c_cyan, c_dim, c_green, c_magenta

NAMES = ["models", "setup"]

Out = Callable[[str], None]
In = Callable[[str], str]


# --------------------------------------------------------------------------- shared helpers

def load_team(specs: Optional[Dict] = None, health: Optional[Dict] = None, check: bool = True):
    from omnicli import agents as ag
    specs = specs if specs is not None else ag.load_agent_specs()
    if health is None:
        health = ag.check_health(specs) if check else {}
    order = [n for n in ag.PREFERRED_LEAD_ORDER if n in specs] + [n for n in specs if n not in ag.PREFERRED_LEAD_ORDER]
    ready = [n for n in order if health.get(n, {}).get("ok")]
    return specs, health, order, ready


def describe_team(specs: Dict, ready: List[str], health: Dict) -> List[str]:
    """Human summary of what a team run would use right now."""
    lead = settings.get("lead")
    workers = settings.get("workers")
    eff_lead = lead if lead in ready else (ready[0] if ready else None)
    eff_workers = [w for w in (workers or ready) if w in ready] or ready
    lines = [f"{c_bold('Lead')}     {c_magenta(eff_lead or '(no AI ready)')}"
             + (c_dim("  (auto: best ready)") if not lead else "")
             + (c_dim(f"  (saved '{lead}' is not ready)") if lead and lead not in ready else ""),
             f"{c_bold('Workers')}  {', '.join(c_cyan(w) for w in eff_workers) or '(none)'}"
             + (c_dim("  (auto: all ready)") if not workers else "")]
    for n in sorted(set([eff_lead] + eff_workers) - {None}, key=lambda x: (x != eff_lead, x)):
        model = settings.get(f"models.{n}") or c_dim("CLI default")
        effort = settings.get(f"effort.{n}") or c_dim("default")
        lines.append(f"  {c_cyan(n.ljust(7))} model {model}   effort {effort}")
    not_ready = [f"{n} ({health.get(n, {}).get('reason', 'not installed')})" for n in specs if n not in ready]
    if not_ready:
        lines.append(c_dim("Not available: " + ", ".join(not_ready)))
    lines.append(c_dim(f"Team defaults: jobs {settings.get('jobs')} · max tasks {settings.get('max_tasks')} · "
                       f"retries {settings.get('retries')} · rounds {settings.get('rounds')} · "
                       f"budget {settings.get('max_calls') or '∞'} calls / {settings.get('max_minutes') or '∞'} min"))
    return lines


def _save(key: str, value, out: Out) -> None:
    if value is None:
        settings.unset(key)
        out(f"  {BADGE_SUCCESS} {key} = (auto)")
    else:
        settings.set_value(key, ",".join(value) if isinstance(value, list) else str(value))
        out(f"  {BADGE_SUCCESS} {key} = {', '.join(value) if isinstance(value, list) else value}")


def pick_lead(specs: Dict, ready: List[str], input_fn: In, out: Out, save: bool = True):
    if not ready:
        out(f"{BADGE_WARN} No AI is ready to lead. Check with: omnicli agents")
        return CANCEL
    opts = [(n, f"{specs[n].get('name', n)} - {specs[n].get('strengths', '')}"[:70]) for n in ready]
    choice = choose_one("Which AI should LEAD (plan the work and review every task)?", opts,
                        current=settings.get("lead"), input_fn=input_fn, out=out)
    if choice is not None and choice is not CANCEL and save:
        _save("lead", choice, out)
    return choice


def pick_workers(specs: Dict, ready: List[str], input_fn: In, out: Out, save: bool = True):
    if not ready:
        out(f"{BADGE_WARN} No AI is ready. Check with: omnicli agents")
        return CANCEL
    opts = [(n, f"{specs[n].get('name', n)} - {specs[n].get('strengths', '')}"[:70]) for n in ready]
    current = settings.get("workers") or ready
    choice = choose_many("Which AIs should WORK on tasks?", opts, current=current, input_fn=input_fn, out=out)
    if choice is not None and choice is not CANCEL and save:
        _save("workers", None if set(choice) == set(ready) else choice, out)
    return choice


def pick_model(name: str, spec: Dict, input_fn: In, out: Out, save: bool = True, refresh: bool = False):
    if not spec.get("model_args"):
        out(f"{BADGE_WARN} {name} has no supported way to choose a model.")
        return CANCEL
    info = model_catalog.discover(name, spec, refresh=refresh)
    opts = [(m["id"], m["name"]) for m in info["models"]]
    opts.append(("default", "let the CLI use its own default" + (f" ({info['cli_default']})" if info.get("cli_default") else "")))
    current = settings.get(f"models.{name}") or "default"

    def check(value: str) -> Optional[str]:
        ok = model_catalog.validate(name, spec, value)
        if ok is False:
            return f"{name} doesn't know a model called '{value}'."
        return None

    title = f"Which model should {c_cyan(name)} use?" + (c_dim(f"  (from {info['source']})") if info.get("source") else "")
    if info.get("note"):
        title += c_dim(f"\n  {info['note']}")
    choice = choose_one(title, opts, current=current, input_fn=input_fn, out=out,
                        allow_custom=info.get("free_text", True), custom_hint="or type a model name",
                        validate=check)
    if choice is None or choice is CANCEL:
        return choice
    if save:
        _save(f"models.{name}", None if choice == "default" else choice, out)
    return choice


def pick_effort(name: str, spec: Dict, input_fn: In, out: Out, save: bool = True):
    levels = model_catalog.effort_levels(spec)
    if not levels:
        out(c_dim(f"  {name} has no reasoning-effort setting."))
        return CANCEL
    opts = [(lv, "") for lv in levels] + [("default", "let the CLI decide")]
    choice = choose_one(f"Reasoning effort for {c_cyan(name)}?", opts,
                        current=settings.get(f"effort.{name}") or "default", input_fn=input_fn, out=out)
    if choice is None or choice is CANCEL:
        return choice
    if save:
        _save(f"effort.{name}", None if choice == "default" else choice, out)
    return choice


def run_setup(specs: Dict, ready: List[str], health: Dict, input_fn: In = input, out: Out = print) -> int:
    out(c_bold("\nOmniCLI setup") + c_dim("  - Enter keeps the current value at every step, q cancels a step"))
    if not ready:
        out(f"{BADGE_WARN} No AI CLI is ready (logged in). Fix that first: omnicli doctor")
        return 1
    out(c_dim("Ready: " + ", ".join(ready)) + "\n")
    lead = pick_lead(specs, ready, input_fn, out)
    out("")
    workers = pick_workers(specs, ready, input_fn, out)
    eff_lead = lead if isinstance(lead, str) else (settings.get("lead") if settings.get("lead") in ready else ready[0])
    eff_workers = workers if isinstance(workers, list) else [w for w in (settings.get("workers") or ready) if w in ready]
    for n in [eff_lead] + [w for w in eff_workers if w != eff_lead]:
        out("")
        if pick_model(n, specs[n], input_fn, out) is CANCEL:
            continue
        if model_catalog.effort_levels(specs[n]):
            out("")
            pick_effort(n, specs[n], input_fn, out)
    out("\n" + c_bold("Team defaults"))
    for key, label, cast in (("jobs", "Tasks that may run at the same time", int),
                             ("max_tasks", "Most tasks the lead may plan per round", int),
                             ("max_calls", "Budget: stop after this many AI calls (0 = no limit)", int),
                             ("max_minutes", "Budget: stop after this many minutes (0 = no limit)", float)):
        v = ask_number(label, settings.get(key), cast=cast, minimum=1 if key in ("jobs", "max_tasks") else 0,
                       input_fn=input_fn, out=out)
        if v is CANCEL:
            break
        if v is not None:
            _save(key, v, out)
    out("\n" + c_bold("Your team now:"))
    for line in describe_team(specs, ready, health):
        out("  " + line)
    out(c_dim(f"\nSaved in {settings.settings_path()} · change any time with `omnicli setup` or /setup in chat"))
    return 0


# --------------------------------------------------------------------------- CLI

def add_parser(subparsers) -> None:
    p = subparsers.add_parser("models", help="Show which models each AI CLI can use and which one OmniCLI picks")
    p.add_argument("agent", nargs="?", help="Only this AI CLI (claude, codex, agy, muse, ...)")
    p.add_argument("--refresh", action="store_true", help="Re-read the model lists instead of the 1-day cache")
    p.add_argument("--json", action="store_true", help="Print as JSON")
    p.set_defaults(func=handle_models)

    s = subparsers.add_parser("setup", help="Interactive setup: choose the lead, workers, each AI's model and effort")
    s.add_argument("--show", action="store_true", help="Only show the current team setup")
    s.set_defaults(func=handle_setup)


def handle_models(args) -> int:
    from omnicli import agents as ag
    specs = ag.load_agent_specs()
    names = [args.agent] if args.agent else list(specs)
    if args.agent and args.agent not in specs:
        print(f"{BADGE_WARN} Unknown AI CLI '{args.agent}'. Known: {', '.join(specs)}")
        return 1
    result = {}
    for n in names:
        info = model_catalog.discover(n, specs[n], refresh=args.refresh)
        result[n] = dict(info, chosen=settings.get(f"models.{n}"), effort=settings.get(f"effort.{n}"),
                         effort_levels=model_catalog.effort_levels(specs[n]))
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    for n, info in result.items():
        chosen = info["chosen"] or "CLI default" + (f" ({info['cli_default']})" if info.get("cli_default") else "")
        print(f"\n{c_bold(c_cyan(n))}  {c_dim(specs[n].get('name', n))}   using: {c_green(chosen)}"
              + (f"   effort: {info['effort']}" if info.get("effort") else ""))
        if info.get("source") and info["source"] != "-":
            print(c_dim(f"  models from: {info['source']}"))
        for m in info["models"][:40]:
            mark = c_green(" ✓") if m["id"] == info["chosen"] else ""
            print(f"    {m['id']:<28} {c_dim(m['name'])}{mark}")
        if info.get("effort_levels"):
            print(c_dim(f"  effort levels: {', '.join(info['effort_levels'])}"))
        if info.get("note"):
            print(c_dim(f"  {info['note']}"))
    print(c_dim("\nChoose with: omnicli setup   (or: omnicli config set models.<agent> <model>, or /model in chat)"))
    return 0


def handle_setup(args) -> int:
    specs, health, _order, ready = load_team()
    if args.show:
        print()
        for line in describe_team(specs, ready, health):
            print(line)
        return 0
    return run_setup(specs, ready, health)
