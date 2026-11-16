"""
`omnicli doctor` - check that OmniCLI is healthy, like Claude Code's /doctor.

    omnicli doctor            offline checks + the cached AI-agent health
    omnicli doctor --check    also pings every installed AI CLI (one tiny prompt each)
    omnicli doctor --fix      apply safe, local fixes (never edits agents.json/config.json,
                              never installs anything)
    omnicli doctor --json     machine-readable output

Every check is a small function returning Check(status, name, detail, hint) objects,
grouped into sections: Install, Environment, Config, AI agents, Runs. A check that can
be repaired safely carries a `fix` callable that --fix runs. The exit code is 1 if any
check FAILs, else 0.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import omnicli
from omnicli import agents as ag
from omnicli import paths, runs, settings
from omnicli.ui import c_bold, c_cyan, c_dim, c_green, c_red, c_yellow

NAMES = ["doctor"]

PASS, WARN, FAIL, INFO, FIXED = "PASS", "WARN", "FAIL", "INFO", "FIXED"
SECTIONS = ["Install", "Environment", "Config", "AI agents", "Runs"]
MIN_PYTHON = (3, 9)
MIN_FREE_BYTES = 2 * 1024 ** 3
VERSION_TIMEOUT = 15.0
GIT_TIMEOUT = 10.0
NAME_WIDTH = 24

CHECK_NOTICE = ("Pinging each installed AI CLI with one tiny prompt (\"reply PONG\") for a fresh login "
                "check - this uses a little of each CLI's quota...")

# How to log in, per built-in agent (anything else: "run `<bin>` once and log in").
LOGIN_HINTS = {
    "claude": "run `claude` and log in with /login",
    "codex": "run `codex login`",
}
_AUTH_WORDS = ("not logged in", "auth", "login", "log in", "logged in", "sign in", "unauthorized", "401",
               "api key", "token")

# A fix returns (resolved, message): resolved=True turns the check into FIXED.
FixFn = Callable[[], Tuple[bool, str]]


class Check:
    """One line of the doctor report."""

    def __init__(self, status: str, name: str, detail: str = "", hint: str = "",
                 fix: Optional[FixFn] = None):
        self.status = status
        self.name = name
        self.detail = detail
        self.hint = hint
        self.fix = fix
        self.section = ""
        self.fix_message = ""   # set by --fix
        self.fix_ok = True

    def to_dict(self) -> Dict:
        return {
            "section": self.section,
            "status": self.status,
            "name": self.name,
            "detail": self.detail,
            "fix_hint": self.hint,
            "fixable": self.fix is not None,
        }

    def __repr__(self) -> str:  # handy in test failures
        return f"Check({self.section}/{self.status} {self.name}: {self.detail})"


# --------------------------------------------------------------------------- small helpers

def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _capture(cmd: List[str], timeout: float, env: Optional[Dict[str, str]] = None) -> Tuple[Optional[int], str, str]:
    """Run a command with no stdin; return (exit_code, stdout, stderr). exit_code is None on
    timeout (the whole process group is killed, so CLIs that spawn helpers can't hang us)."""
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, start_new_session=True)
    except Exception as ex:
        return 126, "", str(ex)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            proc.kill()
        try:
            proc.communicate(timeout=2)
        except Exception:
            pass
        return None, "", f"timed out after {timeout:.0f}s"


def _first_line(*texts: str, limit: int = 70) -> str:
    for t in texts:
        for line in (t or "").splitlines():
            if line.strip():
                line = line.strip()
                return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def _tilde(p) -> str:
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if s == home or s.startswith(home + os.sep) else s


def _age(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def _nearest_existing(p: Path) -> Path:
    p = Path(p)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _writable(p: Path) -> bool:
    return os.access(str(p), os.W_OK | os.X_OK)


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _json_error(ex: ValueError) -> str:
    if isinstance(ex, json.JSONDecodeError):
        return f"{ex.msg} (line {ex.lineno}, column {ex.colno})"
    return str(ex)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _checkout_bin() -> Path:
    """This checkout's launcher: <repo>/bin/omnicli."""
    return Path(omnicli.__file__).resolve().parent.parent / "bin" / "omnicli"


def _load_specs() -> Dict[str, Dict]:
    """Agent specs, without load_agent_specs' own "ignoring invalid" print (Config reports that)."""
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return ag.load_agent_specs()
    except Exception:
        return {k: dict(v, id=k) for k, v in ag.BUILTIN_AGENTS.items()}


def _read_health_cache() -> Dict:
    try:
        data = json.loads(paths.health_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _tool_report() -> Tuple[List[str], List[str]]:
    from omnicli import conductor  # lazy: conductor is heavy and only this check needs it
    return conductor.tool_report()


# --------------------------------------------------------------------------- Install

def check_python(version_info=None, executable: Optional[str] = None) -> Check:
    v = tuple(version_info or sys.version_info)
    shown = ".".join(str(x) for x in v[:3])
    where = _tilde(executable or sys.executable)
    if v[:2] < MIN_PYTHON:
        return Check(FAIL, "Python", f"{shown} ({where}) - OmniCLI needs Python 3.9 or newer",
                     "install Python 3.9+ and run OmniCLI with it")
    return Check(PASS, "Python", f"{shown} ({where})")


def check_package() -> Check:
    try:
        import importlib
        pkg = importlib.import_module("omnicli")
    except Exception as ex:
        return Check(FAIL, "omnicli package", f"cannot be imported: {ex}", "reinstall OmniCLI")
    version = getattr(pkg, "__version__", "")
    where = _tilde(Path(pkg.__file__).resolve().parent)
    if not version:
        return Check(WARN, "omnicli package", f"imported from {where} but has no __version__",
                     "your checkout may be incomplete; update it")
    return Check(PASS, "omnicli package", f"v{version} ({where})")


def check_command_on_path() -> Check:
    expected = _checkout_bin()
    found = _which("omnicli")
    link_hint = (f"ln -sf {expected} ~/.local/bin/omnicli  (and make sure ~/.local/bin is on your PATH)"
                 if expected.exists() else "add the folder that holds the `omnicli` script to your PATH")
    if not found:
        return Check(WARN, "omnicli command", "not on your PATH - you have to type the full path", link_hint)
    if not expected.exists():
        return Check(PASS, "omnicli command", _tilde(found))
    try:
        same = Path(found).resolve() == expected.resolve()
    except OSError:
        same = False
    if same:
        return Check(PASS, "omnicli command", f"{_tilde(found)} -> this checkout")
    return Check(WARN, "omnicli command",
                 f"{_tilde(found)} is not this checkout ({_tilde(expected)}) - `omnicli` may run an older copy",
                 link_hint)


# --------------------------------------------------------------------------- Environment

def check_git() -> Check:
    git = _which("git")
    if not git:
        return Check(WARN, "git", "not installed - no git checkpoint after each task, nothing to undo",
                     "install git (macOS: `xcode-select --install`)")
    code, out, err = _capture([git, "--version"], GIT_TIMEOUT)
    version = _first_line(out) if code == 0 else ""
    return Check(PASS, "git", f"{version or 'installed'} ({_tilde(git)})")


def _git_config(git: str, key: str) -> str:
    code, out, _ = _capture([git, "config", "--global", key], GIT_TIMEOUT)
    return out.strip() if code == 0 else ""


def check_git_identity() -> Check:
    git = _which("git")
    if not git:
        return Check(INFO, "git identity", "skipped - git is not installed")
    name, email = _git_config(git, "user.name"), _git_config(git, "user.email")
    if name and email:
        return Check(PASS, "git identity", f"{name} <{email}>")
    missing = [k for k, v in (("user.name", name), ("user.email", email)) if not v]
    cmds = []
    if not name:
        cmds.append('`git config --global user.name "Your Name"`')
    if not email:
        cmds.append("`git config --global user.email you@example.com`")
    hint = " and ".join(cmds)
    if not email:
        return Check(WARN, "git identity",
                     f"{' and '.join(missing)} not set globally - OmniCLI will commit as \"OmniCLI\"", hint)
    return Check(WARN, "git identity", "user.name not set globally - git will guess your name for checkpoints", hint)


def _fix_create_home(home: Path) -> FixFn:
    def fix() -> Tuple[bool, str]:
        home.mkdir(parents=True, exist_ok=True)
        return True, f"created {_tilde(home)}"
    return fix


def check_home() -> Check:
    home = paths.omni_home()
    if home.exists():
        if not home.is_dir():
            return Check(FAIL, "OmniCLI home", f"{_tilde(home)} exists but is not a folder",
                         "move that file aside, or set OMNICLI_HOME to another folder")
        if not _writable(home):
            return Check(FAIL, "OmniCLI home", f"{_tilde(home)} is not writable - runs, logs and settings can't be saved",
                         f"chmod u+rwx {home}  (or set OMNICLI_HOME to a writable folder)")
        return Check(PASS, "OmniCLI home", f"{_tilde(home)} (writable)")
    parent = _nearest_existing(home)
    if parent.is_dir() and _writable(parent):
        return Check(WARN, "OmniCLI home", f"{_tilde(home)} does not exist yet (it is created on first use)",
                     "`omnicli doctor --fix` creates it", fix=_fix_create_home(home))
    return Check(FAIL, "OmniCLI home", f"{_tilde(home)} does not exist and {_tilde(parent)} is not writable",
                 "set OMNICLI_HOME to a writable folder")


def check_disk_space() -> Check:
    target = _nearest_existing(paths.omni_home())
    try:
        free = shutil.disk_usage(str(target)).free
    except OSError as ex:
        return Check(WARN, "disk space", f"could not measure free space at {_tilde(target)}: {ex}")
    gb = free / 1024 ** 3
    if free < MIN_FREE_BYTES:
        return Check(WARN, "disk space", f"only {gb:.1f} GB free on the disk holding {_tilde(target)}",
                     "free up space - projects, node_modules and logs need room (2 GB+ recommended)")
    return Check(PASS, "disk space", f"{gb:.1f} GB free")


def check_dev_tools() -> Check:
    try:
        have, missing = _tool_report()
    except Exception as ex:
        return Check(WARN, "developer tools", f"could not probe them: {ex}")
    if missing:
        return Check(WARN, "developer tools", f"{len(have)} found; missing: {', '.join(missing)}",
                     "install the ones your projects need - workers can only use tools on this machine")
    return Check(PASS, "developer tools", f"all {len(have)} probed tools found")


# --------------------------------------------------------------------------- Config

def check_agents_config() -> List[Check]:
    path = paths.agents_config_path()
    if not path.exists():
        return [Check(PASS, "agents.json", "not present - using the built-in adapters")]
    shown = _tilde(path)
    ignore_hint = f"fix {shown} (or move it aside to use the built-in adapters)"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as ex:
        return [Check(FAIL, "agents.json", f"invalid JSON: {_json_error(ex)} - OmniCLI ignores the whole file",
                      ignore_hint)]
    except OSError as ex:
        return [Check(FAIL, "agents.json", f"cannot be read: {ex}", ignore_hint)]
    if not isinstance(cfg, dict):
        return [Check(FAIL, "agents.json", "must be a JSON object like {\"agents\": {...}, \"disabled\": [...]} "
                      "- OmniCLI ignores the whole file", ignore_hint)]
    overrides = cfg.get("agents") or {}
    if not isinstance(overrides, dict) or any(not isinstance(v, dict) for v in overrides.values()):
        return [Check(FAIL, "agents.json", "\"agents\" must map each agent name to an object "
                      "- OmniCLI ignores the whole file", ignore_hint)]

    out = []
    disabled = cfg.get("disabled") or []
    if not isinstance(disabled, list):
        out.append(Check(WARN, "agents.json", f"\"disabled\" must be a list of agent names, not {type(disabled).__name__}",
                         f"e.g. \"disabled\": [\"agy\"] in {shown}"))
        disabled = []
    out.insert(0, Check(PASS, "agents.json", f"valid ({_plural(len(overrides), 'override')}, {len(disabled)} disabled)"))

    for name, override in overrides.items():
        if name in disabled:
            continue
        custom = name not in ag.BUILTIN_AGENTS
        if not custom and "bin" not in override:
            continue
        base = ag.BUILTIN_AGENTS.get(name, {"name": name, "bin": name})
        spec = dict(base, **override)
        if not ag.resolve_bin(spec):
            kind = "custom agent" if custom else "\"bin\" override"
            out.append(Check(WARN, f"agent {name}", f"{kind}: '{spec.get('bin')}' was not found",
                             f"install it, or set \"bin\" for \"{name}\" in {shown} to a command on PATH "
                             "or an absolute path"))
    return out


_EXPECTED = {bool: "true or false", int: "a whole number", float: "a number",
             list: "a list of agent names", str: "text"}


def _setting_problem(key: str, value, specs: Dict[str, Dict]) -> Tuple[str, str]:
    """('', '') if the saved value is fine, else (problem, hint)."""
    if key not in settings.SCHEMA:
        return ("unknown setting - ignored",
                f"remove \"{key}\" from config.json (known: {', '.join(sorted(settings.SCHEMA))})")
    if value is None:
        return "", ""
    typ = settings.SCHEMA[key][0]
    if typ is dict:
        if not isinstance(value, dict):
            return (f"must be per-agent, e.g. {{\"claude\": \"...\"}}, not {json.dumps(value)}",
                    f"`omnicli config set {key}.<agent> <value>`")
        bad = [k for k, v in value.items() if not isinstance(v, str)]
        if bad:
            return (f"value for {', '.join(bad)} must be text",
                    f"`omnicli config set {key}.{bad[0]} <value>`")
        unknown = [k for k in value if k not in specs]
        if unknown:
            return f"set for unknown agent(s): {', '.join(unknown)}", "`omnicli agents` lists the agent names"
        return "", ""
    if typ is bool:
        ok = isinstance(value, bool)
    elif typ is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif typ is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif typ is list:
        ok = isinstance(value, list) and all(isinstance(x, str) for x in value)
    else:
        ok = isinstance(value, str)
    if not ok:
        hint = f"`omnicli config set {key} <{_EXPECTED.get(typ, 'value')}>`"
        if isinstance(value, str):
            try:
                settings.coerce(key, value)
                hint = f"`omnicli config set {key} {value}` saves it with the right type"
            except settings.SettingsError:
                pass
        return f"is {json.dumps(value)}, expected {_EXPECTED.get(typ, typ.__name__)}", hint
    if key == "lead" and value not in specs:
        return f"'{value}' is not a known AI agent", "`omnicli agents` lists them; `omnicli config set lead <agent>`"
    if key == "workers":
        unknown = [w for w in value if w not in specs]
        if unknown:
            return (f"unknown AI agent(s): {', '.join(unknown)}",
                    "`omnicli agents` lists them; `omnicli config set workers a,b`")
    return "", ""


def check_settings(specs: Dict[str, Dict]) -> List[Check]:
    path = paths.settings_path()
    if not path.exists():
        return [Check(PASS, "config.json", "not present - using the defaults")]
    shown = _tilde(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as ex:
        return [Check(FAIL, "config.json", f"invalid JSON: {_json_error(ex)} - every saved default is ignored",
                      f"fix {shown} or move it aside")]
    except OSError as ex:
        return [Check(FAIL, "config.json", f"cannot be read: {ex}", f"check the permissions of {shown}")]
    if not isinstance(data, dict):
        return [Check(FAIL, "config.json", "must be a JSON object of settings - every saved default is ignored",
                      f"fix {shown} or move it aside")]
    out = []
    for key, value in data.items():
        problem, hint = _setting_problem(key, value, specs)
        if problem:
            out.append(Check(WARN, f"setting {key}", problem, hint))
    if not out:
        out.append(Check(PASS, "config.json", f"{_plural(len(data), 'setting')}, all valid"))
    return out


# --------------------------------------------------------------------------- AI agents

def _agent_version(binary: str) -> str:
    code, out, err = _capture([binary, "--version"], VERSION_TIMEOUT, env=ag.agent_env())
    if code is None:
        return "--version timed out"
    return _first_line(out, err, limit=40) if code == 0 else ""


def _versions(binaries: Dict[str, str]) -> Dict[str, str]:
    """`<bin> --version` for every installed agent, in parallel."""
    if not binaries:
        return {}
    out: Dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(binaries)) as ex:
        futs = {ex.submit(_agent_version, b): n for n, b in binaries.items()}
        for f in concurrent.futures.as_completed(futs):
            try:
                out[futs[f]] = f.result()
            except Exception:
                out[futs[f]] = ""
    return out


def _agent_hint(name: str, spec: Dict, reason: str) -> str:
    binary = spec.get("bin", name)
    low = (reason or "").lower()
    if any(w in low for w in _AUTH_WORDS):
        return LOGIN_HINTS.get(name, f"run `{binary}` once and log in") + ", then `omnicli doctor --check`"
    if "timed out" in low:
        return f"run `{binary}` yourself - it may be slow or waiting for input; then `omnicli doctor --check`"
    return f"run `{binary}` yourself to see what is wrong, then `omnicli doctor --check`"


def _fix_clear_cached_failure(names: List[str]) -> FixFn:
    def fix() -> Tuple[bool, str]:
        cache = _read_health_cache()
        removed = [n for n in names if isinstance(cache.get(n), dict) and not cache[n].get("ok")]
        if not removed:
            return False, f"no cached failure to clear for {', '.join(names)}"
        for n in removed:
            del cache[n]
        _atomic_write_json(paths.health_cache_path(), cache)
        return False, f"cleared the cached failure for {', '.join(removed)} - re-checked on the next run"
    return fix


def check_agents(specs: Dict[str, Dict], fresh: Optional[Dict[str, Dict]] = None) -> List[Check]:
    """One row per agent + an overall verdict. `fresh` is check_health() output from --check;
    without it the cached health (agent_health.json) is reported."""
    now = time.time()
    cache = _read_health_cache()
    installed = {n: ag.resolve_bin(s) for n, s in specs.items()}
    versions = _versions({n: b for n, b in installed.items() if b})
    rows: List[Check] = []
    ready: List[str] = []
    unchecked: List[str] = []
    failed: List[str] = []

    for name, spec in specs.items():
        if not installed[name]:
            rows.append(Check(INFO, name, f"not installed ('{spec.get('bin', name)}' not found) - optional",
                              f"install {spec.get('name', name)} to add it to the team"))
            continue
        version = versions.get(name) or "version unknown"
        if fresh is not None:
            h = fresh.get(name) if isinstance(fresh.get(name), dict) else None
            when = "checked just now"
        else:
            h = cache.get(name) if isinstance(cache.get(name), dict) else None
            when = f"checked {_age(now - float(h.get('checked_at') or 0))}" if h else ""
            if h and h.get("ok") and now - float(h.get("checked_at") or 0) > ag.HEALTH_TTL_SEC:
                when += ", re-checked on the next run"
        if h is None:
            unchecked.append(name)
            rows.append(Check(INFO, name, f"{version} · installed, login not checked yet"))
        elif h.get("ok"):
            ready.append(name)
            rows.append(Check(PASS, name, f"{version} · ready ({when})"))
        else:
            failed.append(name)
            reason = h.get("reason") or "failed its health check"
            rows.append(Check(WARN, name, f"{version} · unavailable: {reason} ({when})",
                              _agent_hint(name, spec, reason), fix=_fix_clear_cached_failure([name])))

    orphans = [n for n, h in cache.items() if n not in specs and isinstance(h, dict) and not h.get("ok")]
    if orphans:
        rows.append(Check(INFO, "health cache", f"cached failures for agents no longer configured: {', '.join(orphans)}",
                          "`omnicli doctor --fix` clears them", fix=_fix_clear_cached_failure(orphans)))

    install_hint = (f"install at least one AI coding CLI ({', '.join(ag.BUILTIN_AGENTS)}) and log in, "
                    "then `omnicli doctor --check`")
    if ready:
        rows.append(Check(PASS, "ready agents", f"{len(ready)} ready: {', '.join(ready)}"))
    elif not any(installed.values()):
        rows.append(Check(FAIL, "ready agents", "no AI CLI is installed - `omnicli team` has nobody to direct",
                          install_hint))
    elif unchecked:
        rows.append(Check(WARN, "ready agents",
                          f"none confirmed ready yet ({len(unchecked)} installed but never checked)",
                          "`omnicli doctor --check` sends one tiny prompt to each installed AI CLI"))
    else:
        rows.append(Check(FAIL, "ready agents", "no AI agent is ready - every installed CLI failed its check",
                          "log in to at least one (see the hints above), then `omnicli doctor --check`"))
    return rows


# --------------------------------------------------------------------------- Runs

def _canonical(project_dir: str) -> str:
    try:
        return str(Path(project_dir).expanduser().resolve())
    except Exception:
        return project_dir


def _drop_raw_registry_key(key: str) -> bool:
    """runs.forget_run() looks a run up by its resolved path; this also drops an entry whose
    key isn't canonical (hand-edited registry, symlinked folder that has since moved)."""
    load, save, lock = (getattr(runs, n, None) for n in ("_load", "_save", "_lock"))
    if not (load and save and lock):
        return False
    with lock:
        data = load()
        if key not in data:
            return False
        del data[key]
        save(data)
        return True


def _fix_forget(project_dir: str) -> FixFn:
    def fix() -> Tuple[bool, str]:
        if runs.forget_run(project_dir) or _drop_raw_registry_key(project_dir):
            return True, f"removed {_tilde(project_dir)} from the run registry"
        return True, f"{_tilde(project_dir)} was already gone from the run registry"
    return fix


def _fix_mark_stopped(rec: Dict) -> FixFn:
    project_dir = rec["dir"]

    def fix() -> Tuple[bool, str]:
        state_path = paths.project_state_dir(project_dir) / "state.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if data.get("phase") != "running" or runs.pid_alive(data.get("pid", 0)):
            return False, f"left {_tilde(project_dir)} alone - it is no longer an interrupted run"
        data["phase"] = "stopped"
        data["pid"] = 0
        data["updated_at"] = time.time()
        _atomic_write_json(state_path, data)
        record = {k: v for k, v in rec.items() if k not in ("exists", "alive")}
        record.update(dir=project_dir, phase="stopped", pid=0)
        runs.register_run(record)
        if _canonical(project_dir) != project_dir:  # register_run re-keyed it; drop the old copy
            _drop_raw_registry_key(project_dir)
        return True, f"marked it stopped - resume with `omnicli resume {project_dir}`"
    return fix


def check_runs() -> List[Check]:
    reg = paths.runs_registry_path()
    if reg.exists():
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except ValueError as ex:
            return [Check(WARN, "run registry", f"{_tilde(reg)} is unreadable ({_json_error(ex)}) - "
                          "`omnicli runs` can't list anything", "move it aside; it is rebuilt as runs start")]
    records = runs.list_runs(include_missing=True)
    out: List[Check] = []
    for rec in records:
        d = rec["dir"]
        label = str(rec.get("project_name") or Path(d).name or d)
        if not rec.get("exists"):
            why = "its folder was deleted" if not Path(d).exists() else "its .omnicli/team/state.json is gone"
            out.append(Check(WARN, label, f"stale registry entry for {_tilde(d)}: {why}",
                             "`omnicli doctor --fix` removes it from the registry", fix=_fix_forget(d)))
            continue
        state = runs.load_state(d)
        if not isinstance(state, dict):
            out.append(Check(WARN, label, f"{_tilde(paths.project_state_dir(d) / 'state.json')} is unreadable",
                             "open it and repair the JSON; doctor never rewrites a run it can't read"))
            continue
        if state.get("phase") == "running":
            pid = state.get("pid") or 0
            if runs.pid_alive(pid):
                out.append(Check(INFO, label, f"running now (pid {pid}) in {_tilde(d)}"))
            else:
                out.append(Check(WARN, label, f"interrupted: marked running but pid {pid or '?'} is gone ({_tilde(d)})",
                                 "`omnicli doctor --fix` marks it stopped; then "
                                 f"`omnicli resume {d}`", fix=_fix_mark_stopped(rec)))
    if not any(c.status == WARN for c in out):
        msg = f"{_plural(len(records), 'run')} registered, all consistent" if records else "no runs yet"
        out.insert(0, Check(PASS, "run registry", msg))
    return out


# --------------------------------------------------------------------------- orchestration

def run_checks(specs: Optional[Dict[str, Dict]] = None, fresh: Optional[Dict[str, Dict]] = None) -> List[Check]:
    specs = _load_specs() if specs is None else specs
    plan = [
        ("Install", [check_python, check_package, check_command_on_path]),
        ("Environment", [check_git, check_git_identity, check_home, check_disk_space, check_dev_tools]),
        ("Config", [check_agents_config, lambda: check_settings(specs)]),
        ("AI agents", [lambda: check_agents(specs, fresh)]),
        ("Runs", [check_runs]),
    ]
    results: List[Check] = []
    for section, fns in plan:
        for fn in fns:
            try:
                res = fn()
            except Exception as ex:  # a doctor bug must never hide the other checks
                res = Check(WARN, getattr(fn, "__name__", "check").replace("check_", ""),
                            f"this check crashed: {type(ex).__name__}: {ex}")
            for c in (res if isinstance(res, list) else [res]):
                c.section = section
                results.append(c)
    return results


def apply_fixes(results: List[Check]) -> List[Dict]:
    applied = []
    for c in results:
        if c.fix is None:
            continue
        try:
            resolved, msg = c.fix()
            ok = True
        except Exception as ex:
            resolved, ok, msg = False, False, f"could not fix {c.name}: {ex}"
        if resolved:
            c.status = FIXED
        c.fix_message = msg
        c.fix_ok = ok
        applied.append({"section": c.section, "name": c.name, "ok": ok, "resolved": resolved, "message": msg})
    return applied


def summarize(results: List[Check]) -> Dict:
    count = lambda s: sum(1 for c in results if c.status == s)  # noqa: E731
    return {"passed": count(PASS), "warnings": count(WARN), "failures": count(FAIL),
            "fixed": count(FIXED), "info": count(INFO)}


def _badge(status: str) -> str:
    color = {PASS: c_green, WARN: c_yellow, FAIL: c_red, FIXED: c_cyan}.get(status, c_dim)
    return color(f"{status:<5}")


def render(results: List[Check], fixes: List[Dict], summary: Dict, did_fix: bool, did_check: bool) -> None:
    width = min(max((len(c.name) for c in results), default=10), NAME_WIDTH)
    indent = " " * (2 + 5 + 2 + width + 2)
    print(f"\n{c_bold(c_cyan('OmniCLI doctor'))}  {c_dim('install · environment · config · AI agents · runs')}")
    for section in SECTIONS:
        rows = [c for c in results if c.section == section]
        if not rows:
            continue
        print(f"\n{c_bold(section)}")
        for c in rows:
            name = c.name if len(c.name) <= width else c.name[: width - 1] + "…"
            print(f"  {_badge(c.status)}  {name:<{width}}  {c.detail}")
            if c.fix_message:
                mark = c_green("✓") if c.fix_ok else c_red("✗")
                print(f"{indent}{mark} {c.fix_message}")
            if c.hint and c.status in (WARN, FAIL, INFO):
                print(indent + c_dim("↳ " + c.hint))

    parts = [
        c_green(f"{summary['passed']} passed"),
        (c_yellow if summary["warnings"] else c_dim)(_plural(summary["warnings"], "warning")),
        (c_red if summary["failures"] else c_dim)(_plural(summary["failures"], "failure")),
    ]
    if summary["fixed"]:
        parts.append(c_cyan(f"{summary['fixed']} fixed"))
    print("\n" + " · ".join(parts))
    if did_fix:
        ok = sum(1 for f in fixes if f["ok"])
        print(c_dim(f"--fix applied {ok} of {len(fixes)} safe local fix{'' if len(fixes) == 1 else 'es'}."
                    if fixes else "--fix: nothing could be fixed automatically."))

    pending = sum(1 for c in results if c.fix is not None and c.status != FIXED)
    if not did_fix and pending:
        print(c_dim(f"Run `omnicli doctor --fix` to apply {pending} safe local fix{'' if pending == 1 else 'es'}."))
    agent_rows = [c for c in results if c.section == "AI agents" and c.name not in ("ready agents", "health cache")]
    if not did_check and any(c.status == WARN or "not checked" in c.detail for c in agent_rows):
        print(c_dim("Run `omnicli doctor --check` to re-test AI CLI logins "
                    "(sends one tiny prompt to each installed AI CLI)."))
    print()


def handler(args) -> int:
    as_json = bool(getattr(args, "json", False))
    do_check = bool(getattr(args, "check", False))
    do_fix = bool(getattr(args, "fix", False))

    specs = _load_specs()
    fresh = None
    if do_check:
        print(CHECK_NOTICE, file=sys.stderr if as_json else sys.stdout, flush=True)
        try:
            fresh = ag.check_health(specs, force=True)
        except Exception as ex:
            print(f"[omnicli] the fresh health check failed ({ex}); showing cached health instead",
                  file=sys.stderr)
            fresh = None

    results = run_checks(specs, fresh)
    fixes = apply_fixes(results) if do_fix else []
    summary = summarize(results)
    code = 1 if summary["failures"] else 0

    if as_json:
        print(json.dumps({
            "checks": [c.to_dict() for c in results],
            "fixes": fixes,
            "summary": dict(summary, fresh_check=do_check, exit_code=code),
        }, indent=2))
    else:
        render(results, fixes, summary, do_fix, do_check)
    return code


def add_parser(subparsers) -> None:
    p = subparsers.add_parser(
        "doctor",
        help="Check OmniCLI's install, environment, config, AI agents and runs (like Claude Code's /doctor)",
        description="Check that OmniCLI is healthy: install, environment, config files, AI agents and saved runs. "
                    "Offline by default - AI agent health comes from the cache unless you pass --check.",
    )
    p.add_argument("--check", action="store_true",
                   help="Also ping every installed AI CLI for a fresh login check. This sends one tiny prompt "
                        "(\"reply PONG\") to each installed AI CLI and uses a little of its quota")
    p.add_argument("--fix", action="store_true",
                   help="Apply safe, local fixes: create the OmniCLI home folder, drop registry entries for "
                        "deleted runs, mark interrupted runs as stopped, clear cached agent failures. Never "
                        "edits agents.json/config.json and never installs anything")
    p.add_argument("--json", action="store_true", help="Print the checks and a summary as JSON")
    p.set_defaults(func=handler)
