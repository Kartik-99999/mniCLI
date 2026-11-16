# AGENTS.md — guide for AI coding agents working on OmniCLI

OmniCLI connects the AI coding CLIs on this Mac (claude, codex, agy, muse) into one team:
`omnicli team "<goal>"` → a **lead** AI plans tasks → **worker** AIs build them → OmniCLI runs each
task's verify command → the lead reviews (approve / revise) → git checkpoint per task → final check.
`omnicli` with no arguments opens a chatbot (`omnicli/commands/chat.py`).

## Hard rules
- **Python 3.9 standard library only.** The system `/usr/bin/python3` is 3.9.6: no `X | Y` unions at
  runtime, no `match`, no 3.10+ APIs. No third-party dependencies (that is a feature of the project).
- **Run the full suite after every change** (currently 461 tests, all must pass):
  `PYTHONPATH=. python3 -m unittest discover -s tests -p "test_*.py"`
- **Tests never touch the real `~/.omnicli`**: every test module sets
  `os.environ["OMNICLI_HOME"] = tempfile.mkdtemp(...)` *before* importing omnicli modules.
- **Tests never call a real AI CLI.** Use `tests/fake_agent.py` (modes: good, picky, followup, broken,
  authfail, silent, limited, recorder) or a tiny fake script written to a temp dir.
- **Security invariants (security review 2026-11-16) — keep them:** verify commands pass
  `omnicli/safecmd.py` (allowed FORMS, not denylists) at planning and run time, and run under
  `safecmd.sandbox_argv` on macOS; child processes get `agents.agent_env(spec)`
  (allow-list, never `os.environ` wholesale); git is set up only after plan approval and asks before a baseline
  commit (`GIT_EXCLUDES` keeps secrets out); team memory is per project and sanitized; revisions from state go
  through `gitutil.safe_rev`; chat AI commands go through the strict schema in `commands/chat.py`.
- **Never add permission-bypass flags** to adapters (`--dangerously-skip-permissions`,
  `bypassPermissions`, `--yolo`, `--dangerously-bypass-approvals-and-sandbox`). A test enforces this.
  Workers use each CLI's safe auto-edit mode plus an allow-list of build/test commands.
- **Only use CLI flags you verified** in that CLI's `--help` on this machine; note the verification in a
  comment next to the adapter field.
- **`omnicli/models.py` is the ORIGINAL core dataclasses file** (CliTool, Step, Pipeline…) used by the
  old engine. Model discovery lives in `omnicli/model_catalog.py`. Never overwrite `models.py`.
- Live tests against real AI CLIs cost the user's quota: keep them tiny and only when needed.

## Map
| File | What it is |
|---|---|
| `omnicli/cli.py` | argparse entry point; `team` handler; passthrough mode (`omnicli git status`); typo guard; no-args → chat |
| `omnicli/agents.py` | AI CLI adapters (`BUILTIN_AGENTS`), `build_command`, `option_args` (model/effort/add-dir/images), `run_agent` (headless run, live log, ledger), health checks, `AgentRun` (`silent`, `auth_problem`, `quota_problem`) |
| `omnicli/conductor.py` | Team engine: `TeamConfig`, `Conductor` (plan → execute → review → checkpoint → final), prompts, `normalize_tasks` (sanitises lead-supplied task ids) |
| `omnicli/teamopts.py` | Flags + saved-settings → `TeamConfig`, shared by team / resume / continue |
| `omnicli/paths.py` · `ledger.py` · `runs.py` · `settings.py` | File locations (`OMNICLI_HOME`), usage ledger, run registry, user config |
| `omnicli/model_catalog.py` · `picker.py` | Each CLI's model list + validation; numbered menus |
| `omnicli/commands/*.py` | Pluggable subcommands (contract in `commands/__init__.py`): agents, usage, runs/resume/continue, status, logs, doctor, config/init, undo/rewind, export, review, chat, models/setup |
| `omnicli/engine.py`, `planner.py`, `recipes.py`, `scanner.py`, `context.py`, `models.py` | Original template/pipeline features (`project`, `chain`, `run`, `recipe`, `scan`) |
| `tests/` | One test file per area; `tests/fake_agent.py` stands in for AI CLIs |

## Facts verified on this machine (keep them true)
- Headless invocations: `claude -p`, `codex exec`, `agy -p`, `muse exec` (see adapters in `agents.py`).
- `agy` silently exits 0 with no output when a shell command isn't in its allow-list
  (`--settings` `command(<cmd>)` rules) → treated as failure (`AgentRun.silent`) and the task is reassigned.
- Claude prints "You've hit your 5-hour limit" at its usage cap → `quota_problem` → dropped for the run.
- Codex images must use `--image=<file>` (the variadic `-i` would swallow the prompt). Muse uses `--image <file>`.
  Claude opens images by path (folder granted with `--add-dir`). `agy` cannot view images headlessly.
- Model lists: `agy models`, `codex debug models` (JSON), `claude --help` aliases (fable/opus/sonnet),
  Muse's `~/.config/muse/settings.json` + `muse model-profile show <id>`.
- Tested CLI versions: claude 2.1.325, codex-cli 0.169.0, agy 1.5.9, muse 1.4.20.

## Style
Match the existing UI helpers in `omnicli/ui.py` (`c_*` colours, `BADGE_*`, `print_table`), respect
`NO_COLOR`, give friendly errors that name the next command to run, and keep `--json` output clean.
