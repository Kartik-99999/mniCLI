"""Capture a local app screenshot and send it to an image-capable reviewer."""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from omnicli.commands import review

NAMES = ["visual"]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def add_parser(subparsers):
    parser = subparsers.add_parser("visual", help="Capture a local app and ask an AI reviewer to inspect the UI")
    parser.add_argument("-d", "--dir", default=".", help="Project directory to review")
    parser.add_argument("--url", help="Local app URL to open (localhost or loopback only)")
    parser.add_argument("--start-command", help="Optional command to start the local app in the project directory")
    parser.add_argument("--output", help="Screenshot path (default: .omnicli/review/screenshots/latest.png)")
    parser.add_argument("--wait", type=float, default=3.0, help="Seconds to wait before capturing (default: 3)")
    parser.add_argument("--lead", help="Image-capable AI reviewer")
    parser.add_argument("--focus", nargs="*", default=["visual layout", "responsive behavior", "visible UI defects"])
    parser.set_defaults(func=handler)


def _local_url(url):
    parsed = urlsplit(url or "")
    if parsed.scheme not in ("http", "https") or parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("--url must use http(s)://localhost, 127.0.0.1, or [::1]")
    return url


def capture_screen(output):
    tool = shutil.which("screencapture")
    if not tool:
        raise RuntimeError("macOS `screencapture` is unavailable; capture a screenshot manually and use `omnicli review --image FILE`")
    result = subprocess.run([tool, "-x", str(output)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    if result.returncode or not Path(output).is_file():
        raise RuntimeError((result.stderr or "screenshot capture failed").strip())
    return Path(output)


def _stop_process(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=2)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass


def handler(args):
    target = Path(args.dir).expanduser().resolve()
    if not target.is_dir():
        print("Visual review error: project directory does not exist: {}".format(target), file=sys.stderr)
        return 1
    if args.wait < 0 or args.wait > 60:
        print("Visual review error: --wait must be between 0 and 60 seconds", file=sys.stderr)
        return 2
    try:
        url = _local_url(args.url) if args.url else None
        if args.start_command and not url:
            raise ValueError("--start-command requires a local --url")
    except ValueError as ex:
        print("Visual review error: {}".format(ex), file=sys.stderr)
        return 2

    if args.output:
        output = Path(args.output).expanduser()
        if not output.is_absolute():
            output = target / output
    else:
        output = target / ".omnicli" / "review" / "screenshots" / "latest.png"
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    app = None
    try:
        if args.start_command:
            app = subprocess.Popen(args.start_command, shell=True, cwd=str(target), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(min(args.wait, 10))
        if url:
            opener = shutil.which("open")
            if not opener:
                raise RuntimeError("macOS `open` is unavailable; open the local URL manually, then rerun without --url")
            subprocess.Popen([opener, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(args.wait)
        capture_screen(output)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as ex:
        print("Visual review error: {}".format(ex), file=sys.stderr)
        return 1
    finally:
        _stop_process(app)

    print("Screenshot saved: {}".format(output))
    review_args = argparse.Namespace(
        focus=list(args.focus or ["visual layout"]), dir=str(target), lead=args.lead, json=False,
        fix=False, yes=False, max_tasks=None, timeout=None, verbose=False, image=[str(output)])
    return review.handler(review_args)
