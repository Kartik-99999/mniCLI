"""
Stand-in AI CLI for testing `omnicli team` without calling real models.

Usage (as an adapter template):  python3 fake_agent.py <mode> <prompt_file>
Modes:
  good      plans, builds, reviews and approves
  picky     like good, but as lead asks for one revision of the first task it reviews
  followup  like good, but the first final check asks for one follow-up task
  broken    always crashes (exit 3)
  authfail  always fails like a logged-out CLI (401)
  silent    exits 0 with no output, like agy when a tool is auto-denied headlessly
  limited   fails like claude after its plan usage cap ("You've hit your 5-hour limit")
  recorder  like good, but first appends its extra argv (after <prompt_file>) and the
            prompt kind to $OMNICLI_FAKE_STATE/argv.jsonl - lets tests see model/effort/add-dir flags
State (counters) lives in $OMNICLI_FAKE_STATE.
"""

import json
import os
import re
import sys
from pathlib import Path

FILES = {"t1": "a/a.txt", "t2": "b/b.txt", "t3": "README.md"}


def bump(name: str) -> int:
    d = Path(os.environ.get("OMNICLI_FAKE_STATE", "."))
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    n = int(f.read_text()) if f.exists() else 0
    f.write_text(str(n + 1))
    return n


def main():
    mode, prompt_file = sys.argv[1], sys.argv[2]
    prompt = Path(prompt_file).read_text(encoding="utf-8")

    if mode == "recorder":
        kind = re.search(r"\[OmniCLI team - ([a-z ]+)\]", prompt)
        d = Path(os.environ.get("OMNICLI_FAKE_STATE", "."))
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "argv.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind.group(1) if kind else "other", "args": sys.argv[3:]}) + "\n")
        mode = "good"

    if mode == "broken":
        print("starting...")
        sys.stderr.write("panic: something exploded\n")
        sys.exit(3)
    if mode == "silent":
        sys.stderr.write('jetski: no output produced - a tool required the "command" permission that headless mode '
                         'cannot prompt for, so it was auto-denied.\n')
        sys.exit(0)
    if mode == "limited":
        print("You've hit your 5-hour limit · resets 2:20am (Asia/Calcutta)")
        sys.exit(1)
    if mode == "authfail":
        sys.stderr.write("ERROR: workspace routing discovery unauthorized (401)\n")
        sys.exit(1)

    if "[OmniCLI team - planning]" in prompt:
        section = prompt.split("WORKERS")[1].split("Tools installed")[0]
        workers = re.findall(r"^- (\w+): ", section, flags=re.M)
        w1, w2 = workers[0], workers[-1]
        plan = {
            "project_name": "fake-project",
            "summary": "Two independent parts built in parallel, then a README.",
            "workflows": [{"kind": "inspection", "scenario": "Read the assembled project documentation.", "check_task": "t3"}],
            "tasks": [
                {"id": "t1", "title": "Part A", "assignee": w1, "instructions": "make a/a.txt",
                 "owns": ["a/"], "verify": "test -f a/a.txt"},
                {"id": "t2", "title": "Part B", "assignee": w2, "instructions": "make b/b.txt",
                 "owns": ["b"], "verify": "test -f b/b.txt"},
                {"id": "t3", "title": "Docs", "assignee": w1, "instructions": "write README.md",
                 "depends_on": ["t1", "t2"], "owns": [], "verify": "test -f README.md"},
            ],
        }
        print("Here is the plan:\n```json\n" + json.dumps(plan) + "\n```")
        return

    if "[OmniCLI team - task assignment]" in prompt:
        tid = re.search(r"YOUR TASK \(([^)]+)\)", prompt).group(1)
        rel = FILES.get(tid)
        if rel is None:
            # like a well-behaved worker: stay inside the first path this task owns, if it owns any
            owned = re.search(r"^Files/dirs you own: (.+)$", prompt, flags=re.M)
            first = owned.group(1).split(",")[0].strip().strip("/") if owned else ""
            if not first or first.startswith("(") or first == ".":
                rel = f"{tid}.txt"
            elif "." in first.rsplit("/", 1)[-1]:
                rel = first                      # owns a single file: write that file
            else:
                rel = f"{first}/{tid}.txt"       # owns a folder: write inside it
        target = Path.cwd() / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        rev = "REVISION #" in prompt
        target.write_text(f"{tid} by {mode}{' (revised)' if rev else ''}\n")
        print(f"working on {tid}\n=== SUMMARY ===\n- created {rel}")
        return

    if "[OmniCLI team - review]" in prompt:
        if mode == "picky" and bump("reviews") == 0:
            print(json.dumps({"verdict": "revise", "feedback": "add the word revised", "summary": "needs work"}))
        else:
            print('Looks good.\n{"verdict": "approve", "feedback": "", "summary": "delivered"}')
        return

    if "[OmniCLI team - final check]" in prompt:
        if mode == "followup" and bump("finals") == 0 and "followup_tasks" in prompt and "must be []" not in prompt:
            worker = re.findall(r"^- (\w+): ", prompt.split("WORKERS:")[1], flags=re.M)[0]
            print(json.dumps({"complete": False, "summary": "almost", "how_to_run": "", "remaining_issues": ["extra missing"],
                              "workflow_results": {"W1": {"status": "met", "evidence": "README.md exists."}},
                              "followup_tasks": [{"title": "Extra", "assignee": worker, "instructions": "make extra",
                                                  "owns": ["extra"], "verify": ""}]}))
        else:
            print(json.dumps({"complete": True, "summary": "fake project built", "how_to_run": "cat README.md",
                              "workflow_results": {"W1": {"status": "met", "evidence": "README.md describes the project."}},
                              "remaining_issues": [], "followup_tasks": []}))
        return

    if "PONG" in prompt:
        print("PONG")
        return
    print("unknown prompt")


if __name__ == "__main__":
    main()
