"""
Model discovery: which models each AI CLI can use, from the CLI itself where possible.

How each source was verified on this machine (2026-11-15):
  agy    `agy models`          prints "<id>\\t<display name>" per line (live list)
  codex  `codex debug models`  prints the model catalog as JSON ({"models": [{slug, display_name,
                               visibility, default_reasoning_level}, ...]}); works logged out
  claude `claude --help`       documents --model aliases 'fable', 'opus', 'sonnet' or a full model name
  muse   `muse serve --no-session-log` + the read-only MSP `model/list` request; a typed id can be
                               checked with `muse model-profile show <id>` (exit 0 = known catalog id)
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


def _muse_catalog(binary: str, spec: Dict, runner: Optional[Callable] = None) -> Optional[Dict]:
    """Read Muse's live model catalog over its supported stdio session protocol."""
    command = [binary, "serve", "--no-session-log"]  # `muse serve --help` verifies this flag.
    if runner is not None:
        output = runner(command, 20)
    else:
        from omnicli.agents import agent_env
        from omnicli import __version__
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"clientInfo": {"name": "omnicli", "title": "OmniCLI", "version": __version__}}},
            {"jsonrpc": "2.0", "method": "initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "model/list", "params": {}},
        ]
        try:
            result = subprocess.run(command, input="\n".join(json.dumps(frame) for frame in frames) + "\n",
                                    capture_output=True, text=True, timeout=20, env=agent_env(spec))
            output = result.stdout if result.returncode == 0 else None
        except Exception:
            output = None
    for line in (output or "").splitlines():
        try:
            message = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(message, dict) and message.get("id") == 2:
            result = message.get("result")
            if isinstance(result, dict) and isinstance(result.get("models"), list):
                return result
            return None
    return None


def _muse_model_rows(catalog: Optional[Dict]) -> List[Dict]:
    """Normalize Muse model/list rows for the shared picker and effort selector."""
    rows = []
    for row in (catalog or {}).get("models", []):
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("modelId") or "").strip()
        if not model_id:
            continue
        label = str(row.get("displayLabel") or model_id).strip()
        provider = str(row.get("providerId") or "").strip()
        if provider:
            label += " ({})".format(provider)
        item = {"id": model_id, "name": label}
        variants = row.get("variants")
        if isinstance(variants, list):
            item["effort_levels"] = [str(level) for level in variants if isinstance(level, str)]
        if row.get("isDefault"):
            item["default"] = True
        rows.append(item)
    return rows


def discover(name: str, spec: Dict, refresh: bool = False, runner: Optional[Callable] = None) -> Dict:
    """{"models": [{"id", "name"}], "cli_default": str|None, "source": str, "free_text": bool, "note": str}"""
    run = runner or _run
    cache = _load_cache()
    hit = cache.get(name)
    if not refresh and hit and time.time() - hit.get("at", 0) < CACHE_TTL_SEC and runner is None:
        stale_muse_cache = name == "muse" and hit.get("data", {}).get("source") != "muse serve model/list"
        if not stale_muse_cache:
            return hit["data"]

    from omnicli.agents import resolve_bin
    data = {"models": [], "cli_default": None, "source": "", "free_text": True, "note": ""}
    cacheable = True
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
                    row = {"id": m["slug"], "name": (m.get("display_name") or m["slug"])
                           + (f" (default effort {eff})" if eff else "")}
                    levels = m.get("supported_reasoning_levels")
                    if isinstance(levels, list):
                        row["effort_levels"] = [str(level) for level in levels if isinstance(level, str)]
                    data["models"].append(row)
        except ValueError:
            pass
        data.update(source="codex debug models", cli_default=_codex_current())
    elif name == "claude" and _bin_is(spec, "claude"):
        data["models"] = [{"id": a, "name": d} for a, d in CLAUDE_ALIASES]
        data.update(source="claude --help", note="or type a full model name")
    elif name == "muse" and _bin_is(spec, "muse"):
        cur = _muse_current()
        catalog = _muse_catalog(binary, spec, runner=runner)
        data["models"] = _muse_model_rows(catalog)
        cacheable = catalog is not None
        catalog_default = next((row["id"] for row in data["models"] if row.get("default")), None)
        data.update(source="muse serve model/list" if catalog is not None else "Muse settings",
                    cli_default=cur or catalog_default,
                    note="" if data["models"] else
                    "Muse did not return a model list; type a catalog id to check with `muse model-profile show`")
        if cur and all(row["id"] != cur for row in data["models"]):
            data["models"].insert(0, {"id": cur, "name": "Muse's current model (not in the live catalog)"})
    else:
        data.update(source="-", note="no model list for this CLI; type a model name")

    if runner is None and cacheable:
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


def cached_effort_levels(spec: Dict, model: str) -> Optional[List[str]]:
    """Model-specific effort tiers from a recent discovery, without launching a CLI."""
    name = str(spec.get("id") or "")
    if not name or not model:
        return None
    hit = _load_cache().get(name)
    if not hit or time.time() - float(hit.get("at", 0) or 0) >= CACHE_TTL_SEC:
        return None
    for row in (hit.get("data") or {}).get("models", []):
        if not isinstance(row, dict) or row.get("id") != model:
            continue
        levels = row.get("effort_levels")
        if isinstance(levels, list):
            return [str(level) for level in levels if isinstance(level, str)]
        # The Codex catalog can list a model but omit its supported tiers. Treat
        # that as unknown so a union of other models' tiers is never sent to it.
        if name == "codex":
            return []
    return None


def effort_levels(spec: Dict, model: Optional[str] = None, runner: Optional[Callable] = None) -> List[str]:
    if not spec.get("effort_args"):
        return []
    if model:
        info = discover(str(spec.get("id") or ""), spec, runner=runner)
        for row in info.get("models", []):
            if row.get("id") == model:
                if isinstance(row.get("effort_levels"), list):
                    return list(row["effort_levels"])
                if str(spec.get("id") or "") == "codex":
                    return []
                break
    from omnicli.agents import effort_levels as adapter_effort_levels
    return adapter_effort_levels(spec, model)
