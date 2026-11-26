"""User workflows in a team plan and their evidence on the integrated project."""

import re
from typing import Dict, List

from omnicli.safecmd import parse_command


def editor_reference(goal: str) -> str:
    """A product reference for a suite request. The user's scope still determines the plan."""
    named_editors = all(re.search(pattern, goal, re.I) for pattern in
                        (r"\b(word|word processor)\b", r"\b(powerpoint|ppt|slides)\b",
                         r"\b(excel|spreadsheets?)\b"))
    if not named_editors and not re.search(r"\b(office|productivity) suite\b", goal, re.I):
        return ""
    return (
        "EDITOR SUITE REFERENCE — adapt to the requested scope. Give each requested editor its own workflow.\n"
        "Document: type 'First draft', bold just 'draft', save, reopen, and continue editing. "
        "Check that the first word remains plain and that the toolbar reflects the selection.\n"
        "Slides: create Opening and Results, move Results first, present, and advance to Opening. Check that "
        "editing or deleting one slide preserves the others. Connect thumbnails and selection to the canvas.\n"
        "Spreadsheet: enter 4 and 7 in A1 and B1, enter =A1+B1 in C1, then change A1 to 8. "
        "C1 should display 11 then 15, while selecting it reveals the formula. Also test other inputs and "
        "references so a hardcoded answer fails.\n"
        "For an editor build, make the primary editing actions discoverable. Follow the workspace's existing "
        "design system and establish a visual hierarchy for the document page, slide canvas, and selected cell. "
        "Use browser interaction for behavior and screenshots for layout when those tools are available."
    )


def _static_check(argv: List[str]) -> bool:
    tool, args = argv[0], argv[1:]
    if tool in {"test", "ls", "true", "cat", "grep", "head", "tail", "wc", "tsc"}:
        return True
    if tool in {"npm", "pnpm", "bun", "yarn", "make"}:
        name = args[1:2] if args[:1] == ["run"] else args[:1]
        return bool(name and name[0] in {"build", "lint", "typecheck", "type-check"})
    if tool in {"go", "cargo"}:
        return args[:1] in (["build"], ["vet"], ["fmt"], ["check"], ["clippy"])
    if tool in {"python", "python3"} and "-m" in args:
        return args[args.index("-m") + 1:][:1] in (["py_compile"], ["compileall"])
    return False


def only_static_checks(command: str) -> bool:
    chain, _ = parse_command(command)
    return bool(chain) and all(_static_check(seg.argv) for _, pipeline in chain for seg in pipeline.segs)


def empty_test_run(command: str, output: str) -> bool:
    chain, _ = parse_command(command)
    if not chain or len(chain) != 1 or len(chain[0][1].segs) != 1:
        return False
    argv = chain[0][1].segs[0].argv
    if argv[0] in {"python", "python3"} and "unittest" in argv:
        return bool(re.search(r"^Ran 0 tests in [\d.]+s\s*$", output, re.M))
    return argv[:2] == ["node", "--test"] and bool(re.search(r"^# tests 0\s*$", output, re.M))


def from_plan(data: Dict, tasks: List) -> List[Dict]:
    rows = data.get("workflows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Include a workflows list covering each main outcome in the user's request.")
    by_id = {t.id: t for t in tasks}
    workflows = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("scenario"), str) or not row["scenario"].strip():
            raise ValueError("Each workflow needs a scenario describing an action and its observable result.")
        check_task = row.get("check_task", "")
        if not isinstance(check_task, str) or (check_task and
                (check_task not in by_id or not by_id[check_task].verify)):
            raise ValueError("Each check_task must name a planned task with an allowed verify command, "
                             "or be empty for a workflow that needs inspection.")
        kind = row.get("kind")
        if kind not in ("interaction", "inspection"):
            raise ValueError("Give each workflow a kind of interaction or inspection.")
        if kind == "interaction" and check_task and only_static_checks(by_id[check_task].verify):
            raise ValueError("Plan a test that exercises the interaction '{}' through the product. "
                             "Its proposed check only inspects or builds files.".format(row["scenario"]))
        workflows.append({"scenario": row["scenario"].strip(), "check_task": check_task, "kind": kind})
    return workflows


def describe(workflows: List[Dict]) -> str:
    lines = []
    for i, row in enumerate(workflows, 1):
        if row["check_task"]:
            proof = "check from " + row["check_task"]
        elif row["kind"] == "interaction":
            proof = "interaction check unavailable"
        else:
            proof = "inspect the artifact"
        lines.append("W{}: {} ({})".format(i, row["scenario"], proof))
    return "\n".join(lines)


def final_reply_error(review, workflows):
    if not isinstance(review.get("complete"), bool):
        return "complete must be a JSON boolean."
    results = review.get("workflow_results")
    for i in range(1, len(workflows) + 1):
        key = "W{}".format(i)
        row = results.get(key) if isinstance(results, dict) else None
        if (not isinstance(row, dict) or row.get("status") not in ("met", "unmet", "unverified")
                or not isinstance(row.get("evidence"), str) or not row["evidence"].strip()):
            return "Supply a status and specific evidence for {}. Use unverified if evidence is unavailable.".format(key)
    return ""


def assess(workflows, tasks, checks, review):
    """Join the final review to real check results. The reviewer cannot override a failed check."""
    evidence = review.get("workflow_results")
    evidence = evidence if isinstance(evidence, dict) else {}
    commands = {task.id: task.verify for task in tasks}
    finished = {task.id for task in tasks if task.status == "done"}
    results = dict(checks)
    rows = []
    for i, workflow in enumerate(workflows, 1):
        key = "W{}".format(i)
        finding = evidence.get(key)
        finding = finding if isinstance(finding, dict) else {}
        status, detail = finding.get("status"), finding.get("evidence")
        if status not in ("met", "unmet", "unverified") or not isinstance(detail, str) or not detail.strip():
            status, detail = "unverified", "The final review supplied no usable evidence for this workflow."
        check_task = workflow["check_task"]
        if not check_task:
            candidate = finding.get("check_task")
            if isinstance(candidate, str) and candidate in finished and commands[candidate] in results:
                if workflow["kind"] != "interaction" or not only_static_checks(commands[candidate]):
                    check_task = candidate
        command = commands.get(check_task, "")
        if workflow.get("kind") == "interaction" and not command and status == "met":
            status, detail = "unverified", "No executable interaction check was linked to this workflow."
        if check_task:
            check = results.get(command)
            if check is None or check[2]:
                status = "unverified"
                detail = check[2] if check and check[2] else "The planned check did not run."
            elif check[0]:
                status = "unmet"
                detail = "Check exited {}. {}".format(check[0], check[1].strip()[-1500:])
            elif workflow["kind"] == "interaction" and empty_test_run(command, check[1]):
                status, detail = "unverified", "The runner reported zero tests for the planned interaction."
        rows.append(dict(workflow, id=key, status=status, evidence=detail.strip(), check_task=check_task, verify=command))
    return rows
