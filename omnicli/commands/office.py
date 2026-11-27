"""Local animated office view for a live OmniCLI team run."""

import argparse
import hmac
import json
import os
import re
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from omnicli import paths
from omnicli.commands import status

NAMES = ["office"]

ASSETS = Path(__file__).with_name("office_assets")
SCENE_FILES = ["scene_office.js", "scene_cricket.js"]  # order = order in the scene picker
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OmniCLI Office</title>
<style>{css}</style>
</head>
<body>
<header>
  <div class="brand"><span class="logo" aria-hidden="true">🏢</span><div><h1>OmniCLI Office</h1><div id="project">Connecting to run…</div></div></div>
  <p id="goal"></p>
</header>
<div class="bar" aria-live="polite">
  <span class="pill live" id="phase">Loading…</span><span class="pill" id="progress"></span><span class="pill" id="refresh">Live</span>
  <label class="pill">Scene <select id="scene" aria-label="Scene"></select></label>
  <div class="legend" aria-hidden="true"><span style="--c:var(--working)">working</span><span style="--c:var(--reviewing)">checking work</span><span style="--c:var(--waiting)">waiting</span><span style="--c:var(--free)">free</span><span style="--c:var(--blocked)">blocked</span></div>
</div>
<div class="stage-wrap"><div class="stage" id="stage"></div></div>
<section class="roster-wrap"><h2>Who's doing what</h2><ul class="roster" id="roster" aria-live="polite"></ul></section>
<footer>Local view · updates every second · add <code>#demo</code> to the address to preview every state · it stops when the omnicli chat or command that started it ends</footer>
{scripts}
<script>OmniOffice.start();</script>
</body>
</html>"""


def build_page() -> str:
    """The whole page as one document: CSS and JS are inlined (the CSP allows no external files)."""
    css = (ASSETS / "office.css").read_text(encoding="utf-8")
    scripts = []
    for name in ["base.js"] + SCENE_FILES:
        js = (ASSETS / name).read_text(encoding="utf-8")
        if "</script" in js.lower() or "</style" in css.lower():
            raise ValueError("office asset {} must not contain a closing script/style tag".format(name))
        scripts.append("<script>\n{}\n</script>".format(js))
    return PAGE_TEMPLATE.replace("{css}", css).replace("{scripts}", "\n".join(scripts))


OFFICE_HTML = build_page()


def add_parser(subparsers):
    parser = subparsers.add_parser("office", help="Open an animated local office for a team run")
    parser.add_argument("ref", nargs="?", help="Run number, project path, name, or goal fragment")
    parser.add_argument("-d", "--dir", help="Project directory of the run")
    parser.add_argument("--port", type=int, default=8765, help="Local web port (default: 8765; 0 chooses one)")
    parser.add_argument("--no-browser", action="store_true", help="Print the URL without opening a browser")
    parser.set_defaults(func=handler)


KEY_HEADER = "X-OmniCLI-Key"


def access_key():
    """The secret the page must send to read run data. Kept in ~/.omnicli/office_key (owner-only), so the
    link stays the same across sessions while other programs and users on this machine can't read the data."""
    path = paths.omni_home() / "office_key"
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 32 and re.fullmatch(r"[A-Za-z0-9_-]+", key):
            if path.stat().st_mode & 0o077:
                os.chmod(str(path), 0o600)
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("office_key.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    os.replace(str(tmp), str(path))
    return key


def page_url(port, key):
    # the key rides in the #fragment: browsers never send it to any server, logs or Referer headers
    return "http://127.0.0.1:{}/#key={}".format(port, key)


_SECRET_RES = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),   # JWT
]
_SECRET_ASSIGN = re.compile(r"(?i)\b([\w.-]*(?:api[_-]?key|token|secret|passw(?:or)?d|credential)[\w.-]*)"
                            r"(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)")


def redact(text):
    """Hide things that look like credentials in agent output shown on the page."""
    text = str(text or "")
    for rx in _SECRET_RES:
        text = rx.sub("<redacted>", text)
    return _SECRET_ASSIGN.sub(lambda m: m.group(1) + m.group(2) + "<redacted>", text)


def _handler_for(project_dir, fallback=None, key=None):
    """project_dir may be a path or a zero-argument callable returning one (or None), so a long-lived
    server (the one chat starts) can follow the chat's current project. fallback(project) returns a
    snapshot to show when there is no team run yet (or None for the classic 503)."""
    def current_project():
        return project_dir() if callable(project_dir) else project_dir

    class OfficeHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Only answer requests addressed to this machine by name/IP: a web page that rebinds its
            # own domain to 127.0.0.1 (DNS rebinding) sends a different Host and is refused.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            if host not in ALLOWED_HOSTS:
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Forbidden")
                return
            path = urlsplit(self.path).path
            if path == "/favicon.ico":  # browsers ask for it; answer quietly instead of a console 404
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path in ("/", "/index.html"):
                payload = OFFICE_HTML.encode("utf-8")
                content_type = "text/html; charset=utf-8"
                code = 200
            elif path == "/snapshot" and key is not None and not hmac.compare_digest(
                    (self.headers.get(KEY_HEADER) or "").encode("utf-8"), key.encode("utf-8")):
                payload = json.dumps({"error": "locked: open the link printed in your terminal"}).encode("utf-8")
                content_type = "application/json; charset=utf-8"
                code = 403
            elif path == "/snapshot":
                project = current_project()
                snapshot = read_snapshot(project) if project is not None else None
                if snapshot is None and fallback is not None:
                    try:
                        snapshot = fallback(project)
                    except Exception:
                        snapshot = None
                if snapshot is None:
                    payload = json.dumps({"error": "Run state is unavailable"}).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                    code = 503
                else:
                    payload = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
                    content_type = "application/json; charset=utf-8"
                    code = 200
            else:
                payload = b"Not found"
                content_type = "text/plain; charset=utf-8"
                code = 404
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return

    return OfficeHandler


def read_snapshot(project_dir):
    state = status.runs.load_state(project_dir)
    if state is None:
        return None
    snap = status.to_json(status.build_snapshot(project_dir, state))
    for agent in snap.get("agents") or []:
        if isinstance(agent, dict) and agent.get("latest_output"):
            agent["latest_output"] = redact(agent["latest_output"])
    return snap


def idle_snapshot(project_dir, lead, workers):
    """What the office shows before any team run: the team at rest, waiting for a task."""
    agents = []
    for role, name in [("lead", lead)] + [("worker", w) for w in workers]:
        if name and name not in [a["id"] for a in agents]:
            agents.append({"id": name, "role": role, "status": "idle", "progress": {"done": 0, "total": 0},
                           "current_tasks": [], "latest_output": "", "reviewing_task": None,
                           "blockers": [], "completed_work": []})
    name = Path(project_dir).name if project_dir else "No project yet"
    return {"project_name": name, "dir": str(project_dir or ""), "phase": "idle", "alive": False,
            "goal": "No team run yet. Ask in the chat (for example \"build a todo app with tests\") "
                    "and the team gets to work here.",
            "counts": {"done": 0, "failed": 0, "running": 0, "pending": 0, "total": 0}, "agents": agents}


def start_background(project_dir, port=0, fallback=None, key=None):
    """Start a daemon-thread office server and return (server, local URL with the access key)."""
    key = key or access_key()
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler_for(project_dir, fallback, key))
    server.office_key = key
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
    thread.start()
    return server, page_url(server.server_address[1], key)


DEFAULT_PORT = 8765


def start_preferred(project_dir, fallback=None, port=DEFAULT_PORT):
    """Like start_background, but on the usual port when it's free (a stable, bookmarkable link);
    another free port when it's taken (e.g. a second chat is open)."""
    try:
        return start_background(project_dir, port=port, fallback=fallback)
    except OSError:
        return start_background(project_dir, port=0, fallback=fallback)


def handler(args):
    if args.port < 0 or args.port > 65535:
        print("Office error: --port must be between 0 and 65535", file=sys.stderr)
        return 2
    project_dir, problem = status.resolve_run(getattr(args, "ref", None), getattr(args, "dir", None))
    if project_dir is None:
        print(problem, file=sys.stderr)
        return 1
    try:
        key = access_key()
        server = ThreadingHTTPServer(("127.0.0.1", args.port), _handler_for(project_dir, key=key))
    except OSError as ex:
        print("Office error: could not start local server: {}".format(ex), file=sys.stderr)
        return 1
    server.daemon_threads = True
    url = page_url(server.server_address[1], key)
    print("OmniCLI Office: {}".format(url), flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nOffice closed.")
    finally:
        server.server_close()
    return 0
