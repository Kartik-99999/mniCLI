"""
Model discovery: which models each AI CLI can use, from the CLI itself where possible.

How each source was verified on this machine (2026-11-15):
  agy    `agy models`          prints "<id>\\t<display name>" per line (live list)
  codex  `codex debug models`  prints the model catalog as JSON ({"models": [{slug, display_name,
                               visibility, default_reasoning_level}, ...]}); works logged out
  claude `claude --help`       documents --model aliases 'fable', 'opus', 'sonnet' or a full model name
  muse   ~/.config/muse/settings.json "model" is the current default; any id can be checked with
                               `muse model-profile show <id>` (exit 0 = known catalog id)
Other CLIs: free text only.

Results are cached for a day in ~/.omnicli/models_cache.json (agy fetches its list online).
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from omnicli import paths

CACHE_TTL_SEC = 24 * 3600
CLAUDE_ALIASES = [
    ("fable", "latest Fable model (alias from `claude --help`)"),
    ("opus", "latest Opus model (alias from `claude --help`)"),
    ("sonnet", "latest Sonnet model (alias from `claude --help`)"),
]


def _cache_path() -> Path:
    return paths.omni_home() / "models_cache.json"


def _load_cache() -> Dict:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: Dict) -> None:
    try:
        _cache_path().parent.mkdir(parents=True, exist_ok=True)
        _cache_path().write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


def _run(cmd: List[str], timeout: float = 30) -> Optional[str]:
    from omnicli.agents import agent_env
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           env=agent_env())
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _bin_is(spec: Dict, name: str) -> bool:
    """Only trust a CLI-specific command when the adapter really runs that CLI."""
    from omnicli.agents import resolve_bin
    b = resolve_bin(spec)
    return bool(b) and os.path.basename(b).lower() == name


def _codex_current() -> Optional[str]:
    try:
        txt = (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
        m = re.search(r'^\s*model\s*=\s*"([^"]+)"', txt, re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def _muse_current() -> Optional[str]:
    try:
        data = json.loads((Path.home() / ".config" / "muse" / "settings.json").read_text(encoding="utf-8"))
        return str(data.get("model") or "") or None
    except Exception:
        return None


def discover(name: str, spec: Dict, refresh: bool = False, runner: Optional[Callable] = None) -> Dict:
    """{"models": [{"id", "name"}], "cli_default": str|None, "source": str, "free_text": bool, "note": str}"""
    run = runner or _run
    cache = _load_cache()
    hit = cache.get(name)
    if not refresh and hit and time.time() - hit.get("at", 0) < CACHE_TTL_SEC and runner is None:
        return hit["data"]

    from omnicli.agents import resolve_bin
    data = {"models": [], "cli_default": None, "source": "", "free_text": True, "note": ""}
    binary = resolve_bin(spec)
    if not spec.get("model_args"):
        data.update(source="-", free_text=False, note="this CLI has no supported model flag")
    elif not binary:
        data.update(source="-", note="not installed")
    elif name == "agy" and _bin_is(spec, "agy"):
        out = run([binary, "models"])
        for line in (out or "").splitlines():
            if "\t" in line:
                mid, label = line.split("\t", 1)
                data["models"].append({"id": mid.strip(), "name": label.strip()})
        data.update(source="agy models", note="" if data["models"] else "`agy models` returned nothing (offline?)")
    elif name == "codex" and _bin_is(spec, "codex"):
        out = run([binary, "debug", "models"])
        try:
            for m in json.loads(out or "{}").get("models", []):
                if m.get("visibility", "list") == "list" and m.get("slug"):
                    eff = m.get("default_reasoning_level")
                    data["models"].append({"id": m["slug"], "name": (m.get("display_name") or m["slug"])
                                           + (f" (default effort {eff})" if eff else "")})
        except ValueError:
            pass
        data.update(source="codex debug models", cli_default=_codex_current())
    elif name == "claude" and _bin_is(spec, "claude"):
        data["models"] = [{"id": a, "name": d} for a, d in CLAUDE_ALIASES]
        data.update(source="claude --help", note="or type a full model name")
    elif name == "muse" and _bin_is(spec, "muse"):
        cur = _muse_current()
        if cur:
            data["models"].append({"id": cur, "name": "Muse's current default"})
        data.update(source="~/.config/muse/settings.json", cli_default=cur,
                    note="type any Muse catalog id; it is checked with `muse model-profile show`")
    else:
        data.update(source="-", note="no model list for this CLI; type a model name")

    if runner is None:
        cache[name] = {"at": time.time(), "data": data}
        _save_cache(cache)
    return data


def validate(name: str, spec: Dict, model: str, runner: Optional[Callable] = None) -> Optional[bool]:
    """True/False when the CLI can tell us, None when it can't be checked without a model call."""
    model = (model or "").strip()
    if not model:
        return False
    run = runner or _run
    from omnicli.agents import resolve_bin
    binary = resolve_bin(spec)
    if name == "muse" and binary and _bin_is(spec, "muse"):
        return run([binary, "model-profile", "show", model], 20) is not None
    if name in ("agy", "codex"):
        listed = {m["id"] for m in discover(name, spec, runner=runner)["models"]}
        return (model in listed) if listed else None
    return None


def effort_levels(spec: Dict) -> List[str]:
    return list(spec.get("effort_levels") or []) if spec.get("effort_args") else []
