"""Compare the configured models' reasoning effort on the same scored examples."""

import json
import sys
import tempfile
from pathlib import Path

from omnicli import agents, model_catalog, settings

NAMES = ["tune"]

# Each prompt has a single checkable JSON answer. Only prompts go to the CLI.
CASES = {
    "closures": {
        "prompt": "In Python, f = [lambda: i for i in range(3)]. What is [g() for g in f]?",
        "expected": [2, 2, 2],
    },
    "schedule": {
        "prompt": "A takes 3 minutes and B takes 5. C takes 4 and depends on A. D takes 2 and "
                  "depends on B and C. With unlimited parallelism, return the earliest finish time of D in minutes.",
        "expected": 9,
    },
    "boundaries": {
        "prompt": "The spec accepts integers 2 through 5 inclusive. The implementation returns 2 < x <= 6. "
                  "List all integers for which the results differ, in ascending order.",
        "expected": [2, 6],
    },
    "left_join": {
        "prompt": "Table a(id) contains 1,2,3. Table b(a_id,v) contains (1,4),(1,NULL),(3,0). "
                  "SELECT a.id, COUNT(b.v), COUNT(*) FROM a LEFT JOIN b ON a.id=b.a_id "
                  "GROUP BY a.id ORDER BY a.id. Return the rows as arrays.",
        "expected": [[1, 1, 2], [2, 0, 1], [3, 1, 1]],
    },
}


def add_parser(subparsers):
    p = subparsers.add_parser("tune", help="Score reasoning efforts for the configured lead and worker models")
    p.add_argument("agent", nargs="?", help="Only this AI CLI (default: all installed adapters)")
    p.add_argument("--efforts", default="medium,high", help="Comma-separated tiers to compare with current effort")
    p.add_argument("--all-models", action="store_true", help="Also score every model in the selected CLIs' catalogs")
    p.add_argument("--cases", type=Path, help="JSON object mapping case ids to prompt and expected JSON answer")
    p.add_argument("--apply", action="store_true", help="Save efforts that score higher than the current setting")
    p.set_defaults(func=handler)


def _load_cases(path):
    cases = json.loads(path.read_text(encoding="utf-8")) if path else CASES
    if not isinstance(cases, dict) or not cases:
        raise ValueError("cases must be a nonempty JSON object")
    for name, case in cases.items():
        if (not name or not isinstance(case, dict) or not isinstance(case.get("prompt"), str)
                or not case["prompt"].strip() or "expected" not in case):
            raise ValueError("each case needs an id, a prompt string and an expected JSON value")
    return cases


def _score(output, cases):
    answers = (agents.extract_json(output, "answers") or {}).get("answers")
    if not isinstance(answers, dict):
        return 0
    # JSON booleans and numbers differ even though Python compares True equal to 1.
    correct = 0
    for name, case in cases.items():
        if name in answers and json.dumps(answers[name], sort_keys=True) == json.dumps(case["expected"], sort_keys=True):
            correct += 1
    return correct


def _targets(name, spec):
    worker = agents.with_saved_options(spec, name)
    lead = agents.with_saved_lead_options(spec, name)
    targets = {}
    for key, configured in (("effort", worker), ("lead_effort", lead)):
        pair = (configured.get("_model"), configured.get("_effort"))
        targets.setdefault(pair, []).append(key)
    return targets


def _compare(spec, model, current, default_model, requested, cases):
    levels = model_catalog.effort_levels(spec, model or default_model)
    tiers = [tier for tier in requested
             if tier != current and tier in levels and agents.effort_supported(spec, model, tier)]
    if not tiers:
        print("  No alternative requested tier is available.")
        return {}, False

    questions = {key: case["prompt"] for key, case in cases.items()}
    prompt = ('Solve each case independently. Return one JSON object with an "answers" field mapping each '
              'case id to its JSON answer. Use only the supplied questions, without tools or files.\n'
              + json.dumps(questions, ensure_ascii=False))
    scores = {}
    for tier in [current] + tiers:
        trial = agents.with_options(spec, model, tier)
        with tempfile.TemporaryDirectory(prefix="omnicli-tune-") as folder:
            run = agents.run_agent(trial, prompt, "readonly", folder, timeout=120, meta={"kind": "tune"})
        if not run.ok:
            print("  {}: {}".format(tier or "CLI default", run.failure_reason()))
            if run.auth_problem or run.quota_problem:
                return scores, True
            if tier == current:
                break
            continue
        scores[tier] = _score(run.output, cases)
        print("  {}: {}/{} correct ({:.1f}s)".format(tier or "CLI default", scores[tier], len(cases),
                                                     run.duration), flush=True)
    return scores, False


def _recommend(name, keys, best, apply):
    if not keys:
        print("  Best effort on these cases: {}".format(best))
        return
    for key in keys:
        # When both roles share a setting, preserve the lead's inheritance.
        if (key == "lead_effort" and "effort" in keys and settings.get("lead_effort." + name) is None
                and str(settings.get("lead_models." + name)).lower() != "default"):
            continue
        setting = "{}.{}".format(key, name)
        if apply:
            settings.set_value(setting, best)
            print("  Saved {} = {}".format(setting, best))
        else:
            print("  Recommended {} = {}".format(setting, best))


def _tune_agent(name, spec, args, requested, cases):
    catalog = model_catalog.discover(name, spec)
    default_model = catalog.get("cli_default")
    targets = _targets(name, spec)
    if args.all_models:
        selected = {model or default_model for model, _ in targets}
        for row in catalog["models"]:
            if row["id"] not in selected:
                targets[(row["id"], None)] = []

    compared = False
    for (model, current), keys in targets.items():
        print("{} / {} ({})".format(name, model or default_model or "CLI default model",
                                    ", ".join(keys) or "catalog model"), flush=True)
        scores, unavailable = _compare(spec, model, current, default_model, requested, cases)
        if len(scores) >= 2:
            compared = True
            # The current tier is measured first and wins ties.
            best = max(scores, key=scores.get)
            if best == current:
                print("  Kept current effort (best score or tie).")
            elif args.apply and keys and _targets(name, spec).get((model, current)) != keys:
                print("  Settings changed during the comparison. Run tune again to apply this result.")
            else:
                _recommend(name, keys, best, args.apply)
        if unavailable:
            break
    return compared


def handler(args):
    specs = agents.load_agent_specs()
    if args.agent and args.agent not in specs:
        print("Unknown AI CLI {!r}. Available adapters: {}".format(args.agent, ", ".join(specs)), file=sys.stderr)
        return 1
    try:
        cases = _load_cases(args.cases)
    except (OSError, ValueError) as ex:
        print("Could not load tuning cases: {}".format(ex), file=sys.stderr)
        return 1
    requested = list(dict.fromkeys(t.strip() for t in args.efforts.split(",") if t.strip()))
    if not requested:
        print("Provide at least one tier with --efforts.", file=sys.stderr)
        return 1
    compared = False
    for name in ([args.agent] if args.agent else specs):
        spec = specs[name]
        if not agents.resolve_bin(spec) or not spec.get("effort_args") or not spec.get("readonly"):
            print("{}: skipped (requires an installed CLI with read-only and effort support)".format(name))
            continue
        if _tune_agent(name, spec, args, requested, cases):
            compared = True
    return 0 if compared else 1
