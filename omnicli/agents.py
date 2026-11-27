"""
AI agent adapters for OmniCLI team mode.

Every AI coding CLI on the device (claude, codex, agy, muse, ...) is wrapped
in a small adapter that knows how to run it headlessly in two modes:

  * "readonly" - used by the LEAD to plan and review. Must not modify files.
  * "edit"     - used by WORKERS to build. May create/modify files inside the
                 project directory, using each CLI's own *safe* auto-edit mode
                 (accept-edits / workspace-write sandbox). No adapter disables
                 the CLI's permission system by default.

Adapters are plain dicts so users can override or add agents in
~/.omnicli/agents.json without touching code:

    {
      "agents": {
        "claude": {"strengths": "my own description"},
        "mybot":  {"name": "My Bot", "bin": "mybot",
                   "readonly": ["ask", "{prompt}"], "edit": ["do", "{prompt}"]}
      },
      "disabled": ["agy"]
    }

Argument templates understand {prompt}, {prompt_file} and {dir}.

Optional per-agent option templates (only for flags verified in that CLI's --help):

    "model_args":   ["--model", "{model}"]       used when a model is chosen for the agent
    "effort_args":  ["--effort", "{effort}"]     used when a reasoning effort is chosen
    "add_dir_args": ["--add-dir", "{add_dir}"]   repeated per extra directory (workers only)
    "effort_levels": [...]                       accepted effort values (others are ignored)
    "options_before_prompt": true                insert these options before a trailing
                                                 positional {prompt} instead of appending them

The conductor stores the chosen values on its own copy of a spec as "_model",
"_effort" and "_add_dirs"; build_command turns them into arguments.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import re
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from omnicli import ledger, paths

# Kept for backwards compatibility; code below always calls omnicli.paths so that
# OMNICLI_HOME (used by tests) is honoured at call time.
OMNI_HOME = paths.omni_home()
CONFIG_PATH = paths.agents_config_path()
HEALTH_CACHE_PATH = paths.health_cache_path()
HEALTH_TTL_SEC = 6 * 3600

# A nested `claude` refuses to start if these leak in from a parent Claude Code session.
_STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")

# Common build, test, and inspection commands made available to workers (no rm, curl,
# git push, pip install, ...). Adapters may further restrict these; in headless mode,
# actions outside the active CLI's policy can be auto-denied. Override per agent via
# ~/.omnicli/agents.json.
WORKER_DEV_COMMANDS = [
    "npm", "npx", "node", "pnpm", "yarn", "bun", "tsc", "tsx", "vite", "vitest",
    "jest", "mocha", "eslint", "prettier",
    "python3", "python", "pytest", "ruff", "mypy", "black", "flake8", "uv", "poetry",
    "go", "cargo", "rustc", "deno", "swift", "dotnet", "mvn", "gradle", "make",
    "ls", "cat", "mkdir", "touch", "stat", "file", "basename", "dirname",
    # read-only inspection agents use constantly (not find: it has -delete/-exec)
    "grep", "head", "tail", "wc", "pwd", "echo", "which", "sort", "diff", "tree",
    "uniq", "cmp", "cut", "tr", "printf", "test", "true", "false",
]
CLAUDE_WORKER_TOOLS = ",".join(
    [f"Bash({c}:*)" for c in WORKER_DEV_COMMANDS]
    + [
        "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
        "Bash(git show:*)", "Bash(git rev-parse:*)", "Bash(git ls-files:*)",
        "Bash(rg:*)", "Bash(sed -n:*)",
    ]
)
# Antigravity takes session-only settings; command(<cmd>) allow-rules (verified with agy 1.5.18).
# Headless workers also need these common read-only commands to inspect a workspace.
AGY_READONLY_COMMANDS = [
    "git status", "git diff", "git log", "git show", "git rev-parse", "git ls-files", "git branch",
    "rg", "sed -n",
]
AGY_WORKER_SETTINGS = json.dumps({"permissions": {"allow": [
    *[f"command({c})" for c in WORKER_DEV_COMMANDS],
    *[f"command({c})" for c in AGY_READONLY_COMMANDS],
]}})

PREFERRED_LEAD_ORDER = ["claude", "codex", "agy", "muse"]

# model/effort/add-dir/permission flags below were each checked against the CLI's own --help on
# 2026-11-27: claude 2.1.332 (`claude --help`), codex-cli 0.172.0 (`codex --help` / `codex exec --help`),
# agy 1.5.18 (`agy --help`), muse 1.4.24 (`muse exec --help`).
# (Google discontinued Gemini CLI; Gemini models remain available through Antigravity - see `agy models`.)
BUILTIN_AGENTS: Dict[str, Dict] = {
    "claude": {
        "name": "Claude Code",
        "vendor": "anthropic",
        # no image flag: it opens the image file by path (folder granted with --add-dir); verified 2026-11-15
        "sees_images": True,
        "bin": "claude",
        "strengths": "architecture, complex multi-file features, refactoring, debugging, careful reviews",
        "readonly": ["-p", "{prompt}", "--permission-mode", "plan", "--permission-prompts", "none"],
        "edit": [
            "-p", "{prompt}", "--permission-mode", "acceptEdits", "--permission-prompts", "none",
            "--allowedTools", CLAUDE_WORKER_TOOLS,
        ],
        "model_args": ["--model", "{model}"],
        "effort_args": ["--effort", "{effort}"],
        "effort_levels": ["low", "medium", "high", "xhigh", "max"],
        "add_dir_args": ["--add-dir", "{add_dir}"],
    },
    "codex": {
        "name": "Codex CLI",
        "vendor": "openai",
        "bin": "codex",
        "strengths": "backend logic, algorithms, precise code edits, writing and fixing tests",
        "readonly": [
            "-a", "never", "exec", "--skip-git-repo-check", "-s", "read-only",
            "--color", "never", "-C", "{dir}", "{prompt}",
        ],
        "edit": [
            "-a", "never", "exec", "--skip-git-repo-check", "-s", "workspace-write",
            "--color", "never", "-C", "{dir}", "{prompt}",
        ],
        "model_args": ["-m", "{model}"],
        # `-c key=value` overrides ~/.codex/config.toml; model_reasoning_effort is a codex config key
        "effort_args": ["-c", 'model_reasoning_effort="{effort}"'],
        # levels from `codex debug models` -> supported_reasoning_levels (union over listed models, 2026-11-27)
        "effort_levels": ["low", "medium", "high", "xhigh", "max", "ultra"],
        "add_dir_args": ["--add-dir", "{add_dir}"],
        "options_before_prompt": True,
        # `codex exec --help`: -i, --image <FILE>...  (variadic; the = form keeps the prompt from being eaten)
        "image_args": ["--image={image}"],
        "sees_images": True,   # verified 2026-11-15: answered "red" for a red-square PNG
    },
    "agy": {
        "name": "Antigravity CLI",
        "vendor": "google",
        "bin": "agy",
        "strengths": "full-stack and web UI work, frontend pages, end-to-end feature wiring",
        "readonly": ["-p", "{prompt}", "--mode", "plan"],
        # AGY 1.5.18 `--help`: auto approves safe sandboxed actions in headless runs;
        # keep the terminal sandbox and explicit session command allow-list enabled.
        "edit": ["-p", "{prompt}", "--mode", "auto", "--sandbox", "--settings", AGY_WORKER_SETTINGS],
        "model_args": ["--model", "{model}"],
        "effort_args": ["--effort", "{effort}"],
        # AGY 1.5.18 `--help`; model slugs that end in a tier are narrowed by effort_levels().
        "effort_levels": ["low", "medium", "high", "xhigh", "max"],
        "add_dir_args": ["--add-dir", "{add_dir}"],
    },
    "muse": {
        "name": "Muse Code",
        "vendor": "muse",
        "bin": "muse",
        "strengths": "terminal coding, scripts and tooling, docs, tests, project scaffolding",
        "readonly": [
            "exec", "--workspace", "{dir}", "--disable-write", "--disable-shell",
            "--disable-request-user-input", "{prompt}",
        ],
        "edit": [
            "exec", "--workspace", "{dir}", "--approval-mode", "never",
            "--disable-request-user-input", "{prompt}",
        ],
        "model_args": ["--model", "{model}"],
        "effort_args": ["--reasoning-effort", "{effort}"],
        "effort_levels": ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
        # `muse exec` has no --add-dir equivalent (only --workspace), so no add_dir_args
        "options_before_prompt": True,
        # `muse exec --help`: --image <PATH>  Attach a local image file (repeatable)
        "image_args": ["--image", "{image}"],
        "sees_images": True,   # verified 2026-11-15
    },
}

_AUTH_HINTS = (
    "unauthorized", "401", "log in", "login", "logged in", "sign in", "refresh token",
    "api key", "invalid_api_key", "authenticat", "token expired",
)
# Plan/usage caps (e.g. claude: "You've hit your 5-hour limit · resets 2:20am"). Only checked on a FAILED
# run, and only in its last lines, so a worker that merely writes "rate limit" in code isn't affected.
_QUOTA_PATTERNS = [re.compile(p, re.I) for p in (
    r"hit your [\w\- ]*limit", r"usage[_\s]+limit", r"limit (has been )?reached", r"quota (exceeded|exhausted)",
    r"exceeded your (current )?quota", r"rate[_\s.\-]?limit(?:ed|ing)?\b", r"too many requests", r"\b429\b",
    r"\b402\b", r"out of (credits|tokens)", r"insufficient (credits|quota|balance)",
    r"credit balance is too low", r"(?:5-hour|hourly|daily|weekly|monthly) limit",
)]
_RESET_TIME = re.compile(r"\breset(?:s|ting)?(?:\s+(?:at|around))?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_RESET_TIME_24H = re.compile(r"\breset(?:s|ting)?(?:\s+(?:at|around))?\s+([01]?\d|2[0-3]):([0-5]\d)\b(?!\s*(?:am|pm))", re.I)
_RESET_RELATIVE = re.compile(
    r"\b(?:reset(?:s|ting)?|try again|retry(?:\s+after)?)\s+(?:in\s+)?"
    r"(?:(\d+)\s*(?:h|hr|hrs|hour|hours)\s*)?(?:(\d+)\s*(?:m|min|mins|minute|minutes)\s*)?(?:(\d+)\s*(?:s|sec|secs|second|seconds))?\b",
    re.I,
)


def parse_quota_reset(text: str, now: Optional[float] = None) -> Optional[float]:
    """Parse local clock reset notices such as 'resets 2:20am', 'resets at 14:30', or 'try again in 15m'."""
    raw = str(text or "")
    ref_now = now if now is not None else time.time()
    match = _RESET_TIME.search(raw)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        meridiem = match.group(3).lower()
        if 1 <= hour <= 12 and minute <= 59:
            hour = hour % 12 + (12 if meridiem == "pm" else 0)
            current = datetime.datetime.fromtimestamp(ref_now).astimezone()
            target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target.timestamp() <= ref_now:
                target += datetime.timedelta(days=1)
            return target.timestamp()
        return None
    match24 = _RESET_TIME_24H.search(raw)
    if match24:
        hour, minute = int(match24.group(1)), int(match24.group(2))
        current = datetime.datetime.fromtimestamp(ref_now).astimezone()
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= ref_now:
            target += datetime.timedelta(days=1)
        return target.timestamp()
    rel = _RESET_RELATIVE.search(raw)
    if rel and any(rel.groups()):
        delta = (int(rel.group(1) or 0) * 3600) + (int(rel.group(2) or 0) * 60) + int(rel.group(3) or 0)
        if delta > 0:
            return ref_now + delta
    return None


def load_agent_specs(config_path: Optional[Path] = None) -> Dict[str, Dict]:
    """Built-in adapters merged with user overrides from ~/.omnicli/agents.json."""
    specs = {k: dict(v, id=k) for k, v in BUILTIN_AGENTS.items()}
    path = config_path or paths.agents_config_path()
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            for name, override in (cfg.get("agents") or {}).items():
                base = specs.get(name, {"name": name, "bin": name, "strengths": "general coding"})
                specs[name] = dict(base, **override, id=name)
            for name in cfg.get("disabled") or []:
                specs.pop(name, None)
        except Exception as ex:  # a broken config must never take the whole tool down
            print(f"[omnicli] ignoring invalid {path}: {ex}")
    return specs


def resolve_bin(spec: Dict) -> Optional[str]:
    b = spec.get("bin", "")
    if not b:
        return None
    if os.path.sep in b:
        return b if os.access(b, os.X_OK) else None
    found = shutil.which(b)
    if found:
        return found
    local = Path.home() / ".local" / "bin" / b
    return str(local) if os.access(str(local), os.X_OK) else None


def with_options(spec: Dict, model: Optional[str] = None, effort: Optional[str] = None) -> Dict:
    """Copy of `spec` carrying a model / effort, applied only when the adapter has a verified
    flag for it (and, for effort, only a level the CLI accepts). Never mutates `spec`."""
    out = dict(spec)
    model = str(model or "").strip()
    effort = str(effort or "").strip()
    if model and out.get("model_args"):
        out["_model"] = model
    if effort and out.get("effort_args") and effort_supported(out, model, effort):
        out["_effort"] = effort
    return out


def effort_levels(spec: Dict, model: Optional[str] = None) -> List[str]:
    """Effort values safe for this adapter/model pair when the CLI exposes them."""
    if not spec.get("effort_args"):
        return []
    levels = list(spec.get("effort_levels") or [])
    # Antigravity model ids can encode a fixed reasoning tier (for example
    # gemini-3.8-flash-high). Passing a different --effort to that model fails.
    if str(spec.get("id") or "").lower() == "agy" and model:
        match = re.search(r"-(none|minimal|low|medium|high|xhigh|max|ultra)$", str(model).lower())
        if match:
            return [match.group(1)]
    return levels


def effort_supported(spec: Dict, model: Optional[str], effort: str) -> bool:
    """Whether an explicitly configured effort can safely be passed to a CLI."""
    if model:
        from omnicli import model_catalog
        discovered = model_catalog.cached_effort_levels(spec, str(model))
        if discovered is not None:
            return effort in discovered
        # A saved Codex tier should still work tomorrow. Discovery refreshes
        # expired metadata and returns no tiers for an unconfirmed model.
        if str(spec.get("id") or "").lower() == "codex":
            return effort in model_catalog.effort_levels(spec, model)
    levels = effort_levels(spec, model)
    return not levels or effort in levels


def with_saved_options(spec: Dict, name: str) -> Dict:
    """`with_options` using the user's saved `models.<name>` / `effort.<name>` settings."""
    from omnicli import settings
    return with_options(spec, settings.get(f"models.{name}"), settings.get(f"effort.{name}"))


def with_saved_lead_options(spec: Dict, name: str) -> Dict:
    """Apply lead settings without carrying a worker tier onto a different model."""
    from omnicli import settings
    model = settings.get(f"lead_models.{name}")
    worker_model = settings.get(f"models.{name}")
    lead_effort = settings.get(f"lead_effort.{name}")
    if model is None:
        model = worker_model
    elif str(model).lower() == "default":
        out = with_options(spec, None, lead_effort)
        out.pop("_model", None)
        return out
    if lead_effort is None and (settings.get(f"lead_models.{name}") is None or model == worker_model):
        lead_effort = settings.get(f"effort.{name}")
    return with_options(spec, model, lead_effort)


def option_args(spec: Dict, mode: str, model: Optional[str] = None, effort: Optional[str] = None,
                add_dirs: Optional[List[str]] = None, images: Optional[List[str]] = None) -> List[str]:
    """Extra CLI arguments for the chosen model / effort / extra directories.

    Values default to the spec's "_model", "_effort" and "_add_dirs". Each is used only
    when the adapter declares the matching template ("model_args", "effort_args",
    "add_dir_args"); extra directories are only given to workers (mode "edit").
    """
    model = spec.get("_model") if model is None else model
    effort = spec.get("_effort") if effort is None else effort
    add_dirs = spec.get("_add_dirs") if add_dirs is None else add_dirs
    out: List[str] = []
    if model and spec.get("model_args"):
        out += [str(a).replace("{model}", str(model)) for a in spec["model_args"]]
    if effort and spec.get("effort_args"):
        if effort_supported(spec, model, effort):
            out += [str(a).replace("{effort}", str(effort)) for a in spec["effort_args"]]
    if add_dirs and mode == "edit" and spec.get("add_dir_args"):
        for d in add_dirs:
            out += [str(a).replace("{add_dir}", str(d)) for a in spec["add_dir_args"]]
    images = spec.get("_images") if images is None else images
    if images:
        if spec.get("image_args"):          # the CLI attaches images natively (codex, muse)
            for img in images:
                out += [str(a).replace("{image}", str(img)) for a in spec["image_args"]]
        elif spec.get("add_dir_args") and spec.get("sees_images"):      # otherwise let it open the files: grant their folder
            for d in dict.fromkeys(str(Path(i).parent) for i in images):
                out += [str(a).replace("{add_dir}", d) for a in spec["add_dir_args"]]
    return out


def build_command(spec: Dict, mode: str, prompt: str, cwd: str, prompt_file: Optional[str] = None,
                  model: Optional[str] = None, effort: Optional[str] = None,
                  add_dirs: Optional[List[str]] = None) -> List[str]:
    binary = resolve_bin(spec) or spec.get("bin", "")
    template = spec.get(mode) or spec.get("edit") or ["{prompt}"]
    args = []
    for a in template:
        a = a.replace("{dir}", cwd)
        if "{prompt_file}" in a:
            a = a.replace("{prompt_file}", prompt_file or "")
        if "{prompt}" in a:
            a = a.replace("{prompt}", prompt)
        args.append(a)
    extra = option_args(spec, mode, model, effort, add_dirs)
    if extra:
        if spec.get("options_before_prompt") and template and template[-1] == "{prompt}":
            args[-1:-1] = extra  # keep the positional prompt last (codex exec / muse exec)
        else:
            args += extra
    return [binary] + args


# Environment allow-list. AI CLIs - and the project code they run - must not see unrelated secrets
# (cloud keys, deploy tokens, SSH agent, a parent Claude Code session's messaging token...), so only
# these pass through. Anything else: `omnicli config set env_passthrough NAME,...` or "env_passthrough"
# on an adapter in ~/.omnicli/agents.json.
_BASE_ENV = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TERM", "TERM_PROGRAM", "COLORTERM",
    "TMPDIR", "TZ", "NO_COLOR", "FORCE_COLOR", "__CF_USER_TEXT_ENCODING",
    # network plumbing (not credentials)
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    # toolchain locations (not credentials)
    "VIRTUAL_ENV", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "PYENV_ROOT", "PYENV_VERSION", "NVM_DIR", "NODE_PATH",
    "GOPATH", "GOROOT", "GOBIN", "CARGO_HOME", "RUSTUP_HOME", "JAVA_HOME", "ANDROID_HOME", "HOMEBREW_PREFIX",
    "HOMEBREW_CELLAR", "HOMEBREW_REPOSITORY", "SDKROOT", "DEVELOPER_DIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
}
_BASE_PREFIXES = ("LC_", "OMNICLI_")
# each AI CLI's own configuration/credentials: (exact names, prefixes)
_AGENT_ENV = {
    "claude": ({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"}, ("ANTHROPIC_",)),
    "codex": ({"CODEX_HOME"}, ("OPENAI_",)),
    "agy": ({"GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"}, ("GEMINI_", "ANTIGRAVITY_")),
    "muse": (set(), ("MUSE_",)),
}


def agent_env(spec: Optional[Dict] = None) -> Dict[str, str]:
    """Environment for a child process. With `spec`, that AI CLI's own variables are added;
    without it (verify commands, version checks) no AI credentials are included."""
    names = set(_BASE_ENV)
    prefixes = list(_BASE_PREFIXES)
    if spec:
        extra_names, extra_prefixes = _AGENT_ENV.get(str(spec.get("id", "")), (set(), ()))
        names |= set(extra_names)
        prefixes += list(extra_prefixes)
        names |= {str(n) for n in (spec.get("env_passthrough") or [])}
    try:
        from omnicli import settings
        names |= {str(n) for n in (settings.get("env_passthrough") or [])}
    except Exception:
        pass
    pre = tuple(prefixes)
    env = {k: v for k, v in os.environ.items()
           if (k in names or k.startswith(pre)) and k not in _STRIP_ENV}
    env.setdefault("NO_COLOR", "1")
    return env


# --------------------------------------------------------------------------- running

_live_lock = threading.Lock()
_live_procs: Dict[int, subprocess.Popen] = {}


def _kill_tree(proc: subprocess.Popen, grace: float = 4.0):
    """Agents spawn their own children; kill the whole process group."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        proc.terminate()
    try:
        proc.wait(timeout=grace)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()


def kill_all():
    with _live_lock:
        procs = list(_live_procs.values())
    for p in procs:
        _kill_tree(p, grace=2.0)


@dataclass
class AgentRun:
    agent: str
    exit_code: int
    output: str
    stderr: str
    duration: float
    timed_out: bool = False
    error: str = ""

    @property
    def silent(self) -> bool:
        """Exit 0 but printed nothing - e.g. agy aborts when a tool is auto-denied in headless mode."""
        return self.exit_code == 0 and not self.timed_out and not self.error and not self.output.strip()

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.error and not self.silent

    def _tail_text(self) -> str:
        lines = [l for l in (self.stderr + "\n" + self.output).strip().splitlines() if l.strip()]
        return "\n".join(lines[-8:])

    @property
    def quota_problem(self) -> bool:
        """The CLI ran out of plan/usage allowance - retrying it this run is pointless."""
        if self.ok or self.timed_out or self.error:
            return False
        tail = self._tail_text()
        return any(p.search(tail) for p in _QUOTA_PATTERNS)

    @property
    def quota_reset_at(self) -> Optional[float]:
        if not self.quota_problem:
            return None
        return parse_quota_reset(self._tail_text())

    @property
    def auth_problem(self) -> bool:
        blob = (self.stderr[-3000:] + self.output[-1000:]).lower()
        return not self.ok and any(h in blob for h in _AUTH_HINTS)

    def failure_reason(self) -> str:
        if self.error:
            return self.error
        if self.timed_out:
            return f"timed out after {self.duration:.0f}s"
        if self.quota_problem:
            hit = next((l for l in reversed(self._tail_text().splitlines())
                        if any(p.search(l) for p in _QUOTA_PATTERNS)), "")
            return "usage limit reached" + (f": {hit.strip()[:140]}" if hit else "")
        if self.auth_problem:
            return "not logged in / auth expired"
        if self.silent:
            note = [l for l in self.stderr.strip().splitlines() if l.strip()]
            denial = next((line for line in reversed(note) if "auto-denied" in line.lower()), "")
            if denial:
                reason = "produced no output: a tool it needed was auto-denied in headless mode"
                return reason + ": " + denial.strip()[:180]
            return "produced no output" + (f": {note[-1][:160]}" if note else "")
        tail = [l for l in (self.stderr or self.output).strip().splitlines() if l.strip()]
        last = tail[-1][:160] if tail else ""
        return f"exit code {self.exit_code}" + (f": {last}" if last else "")


def _silent_unlink(path) -> None:
    try:
        os.unlink(path)
    except Exception:
        pass


def run_agent(
    spec: Dict,
    prompt: str,
    mode: str,
    cwd: str,
    timeout: float = 1200,
    log_path: Optional[Path] = None,
    on_line: Optional[Callable[[str], None]] = None,
    meta: Optional[Dict] = None,
) -> AgentRun:
    """Run one agent headlessly; stream stdout lines to on_line; never raises.

    meta ({"kind", "run_dir", "task_id"}) is stored with the call in the usage ledger.
    """
    name = spec.get("id", spec.get("bin", "agent"))
    if not resolve_bin(spec):
        return AgentRun(name, 127, "", "", 0.0, error=f"'{spec.get('bin')}' is not installed")

    prompt_file = None
    temp_prompt = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_file = str(log_path.with_suffix(".prompt.md"))
        Path(prompt_file).write_text(prompt, encoding="utf-8")
    else:
        # adapters may read the prompt from {prompt_file}; health pings have no log dir
        fd, temp_prompt = tempfile.mkstemp(prefix="omnicli-prompt-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt)
        prompt_file = temp_prompt

    cmd = build_command(spec, mode, prompt, cwd, prompt_file)
    start = time.time()
    out_lines: List[str] = []
    temp_err = log_path is None
    if temp_err:
        # stderr is how we tell "logged out" from other failures, so always keep it
        fd, tmp_name = tempfile.mkstemp(prefix="omnicli-agent-", suffix=".stderr")
        os.close(fd)
        err_path = Path(tmp_name)
    else:
        err_path = log_path.with_suffix(".stderr.log")
    timed_out = {"v": False}

    try:
        err_fh = open(err_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=agent_env(spec),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=err_fh,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except Exception as ex:
        err_fh.close()
        if temp_prompt:
            _silent_unlink(temp_prompt)
        return AgentRun(name, 126, "", "", time.time() - start, error=f"failed to start: {ex}")

    # the output log is written as lines arrive, so `omnicli logs --follow` / `status` see live work
    log_fh = None
    if log_path is not None:
        try:
            log_fh = open(log_path, "w", encoding="utf-8")
        except Exception:
            log_fh = None

    with _live_lock:
        _live_procs[proc.pid] = proc

    def _on_timeout():
        timed_out["v"] = True
        _kill_tree(proc)

    timer = threading.Timer(timeout, _on_timeout)
    timer.daemon = True
    timer.start()
    try:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            out_lines.append(line)
            if log_fh is not None:
                try:
                    log_fh.write(line)
                    log_fh.flush()
                except Exception:
                    pass
            if on_line:
                try:
                    on_line(line.rstrip("\n"))
                except Exception:
                    pass
        proc.wait()
    finally:
        timer.cancel()
        with _live_lock:
            _live_procs.pop(proc.pid, None)
        err_fh.close()
        if log_fh is not None:
            log_fh.close()
        if temp_prompt:
            _silent_unlink(temp_prompt)
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass

    output = "".join(out_lines)
    stderr = err_path.read_text(encoding="utf-8", errors="replace") if err_path.exists() else ""
    if temp_err:
        try:
            err_path.unlink()
        except Exception:
            pass
    if log_path is not None and log_fh is None:
        try:
            log_path.write_text(output, encoding="utf-8")
        except Exception:
            pass
    run = AgentRun(name, proc.returncode, output, stderr, time.time() - start, timed_out=timed_out["v"])
    entry = ledger.make_entry(name, mode, meta, run.duration, run.exit_code, run.ok, run.timed_out,
                              prompt, output)
    if run.quota_problem:
        entry["quota_problem"] = True
        if run.quota_reset_at:
            entry["quota_reset_at"] = run.quota_reset_at
            ledger.record_quota_reset(name, run.quota_reset_at, run.failure_reason())
    ledger.record(entry)
    return run


# --------------------------------------------------------------------------- health

PING_PROMPT = "Reply with exactly the single word PONG and nothing else."


def _ping(spec: Dict, workdir: str, timeout: float) -> Tuple[bool, str]:
    ping_spec = spec
    model = spec.get("_model")
    if (spec.get("effort_args") and not spec.get("_effort")
            and (str(spec.get("id") or "").lower() != "agy" or model)
            and effort_supported(spec, model, "low")):
        ping_spec = with_options(spec, model=model, effort="low")
    run = run_agent(ping_spec, PING_PROMPT, "readonly", workdir, timeout=timeout, meta={"kind": "ping"})
    if run.ok and "pong" in run.output.lower():
        return True, f"ok ({run.duration:.0f}s)"
    if run.ok:
        return False, "answered but did not follow the ping instruction"
    return False, run.failure_reason()


def _load_health_cache() -> Dict:
    try:
        return json.loads(paths.health_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def check_health(
    specs: Dict[str, Dict],
    names: Optional[List[str]] = None,
    force: bool = False,
    timeout: float = 120,
    on_result: Optional[Callable[[str, bool, str], None]] = None,
) -> Dict[str, Dict]:
    """Ping each installed agent (in parallel) and cache the result for a few hours.

    Returns {name: {"installed": bool, "ok": bool, "reason": str, "checked_at": float}}.
    """
    names = names or list(specs)
    cache = _load_health_cache()
    now = time.time()
    result: Dict[str, Dict] = {}
    to_ping: List[str] = []

    for n in names:
        spec = specs[n]
        if not resolve_bin(spec):
            result[n] = {"installed": False, "ok": False, "reason": "not installed", "checked_at": now}
            continue
        cached = cache.get(n)
        # Only successes are trusted from cache; a failed agent (e.g. logged out) is
        # re-pinged every run so `codex login` etc. takes effect immediately.
        if not force and cached and cached.get("ok") and now - cached.get("checked_at", 0) < HEALTH_TTL_SEC:
            result[n] = dict(cached, installed=True, cached=True)
        else:
            to_ping.append(n)

    if to_ping:
        workdir = paths.omni_home() / "ping"
        workdir.mkdir(parents=True, exist_ok=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(to_ping)) as ex:
            futs = {ex.submit(_ping, specs[n], str(workdir), timeout): n for n in to_ping}
            for f in concurrent.futures.as_completed(futs):
                n = futs[f]
                ok, reason = f.result()
                result[n] = {"installed": True, "ok": ok, "reason": reason, "checked_at": time.time()}
                cache[n] = {"ok": ok, "reason": reason, "checked_at": result[n]["checked_at"]}
                if on_result:
                    on_result(n, ok, reason)
        try:
            paths.omni_home().mkdir(parents=True, exist_ok=True)
            paths.health_cache_path().write_text(json.dumps(cache, indent=2), encoding="utf-8")
        except Exception:
            pass
    return result


def mark_unhealthy(name: str, reason: str):
    """Remember a runtime failure (e.g. auth expired) so the next run skips that agent."""
    cache = _load_health_cache()
    cache[name] = {"ok": False, "reason": reason, "checked_at": time.time()}
    try:
        paths.health_cache_path().parent.mkdir(parents=True, exist_ok=True)
        paths.health_cache_path().write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


def extract_json(text: str, required_key: Optional[str] = None) -> Optional[Dict]:
    """Pull the JSON object out of an agent reply (fenced or bare, possibly with prose around it)."""
    if not text:
        return None
    candidates: List[str] = []
    marker = "```"
    parts = text.split(marker)
    # odd-indexed parts are inside fences; prefer the last fenced block
    for i in range(len(parts) - 2, 0, -2):
        block = parts[i]
        if block.lstrip().lower().startswith("json"):
            block = block.lstrip()[4:]
        candidates.append(block.strip())
    candidates.append(text)

    decoder = json.JSONDecoder()
    for cand in candidates:
        last_match = None
        idx = cand.find("{")
        while idx != -1:
            try:
                obj, end = decoder.raw_decode(cand[idx:])
                if isinstance(obj, dict) and (required_key is None or required_key in obj):
                    last_match = obj
                    idx = cand.find("{", idx + end)
                    continue
            except ValueError:
                pass
            idx = cand.find("{", idx + 1)
        if last_match is not None:
            return last_match
    return None
