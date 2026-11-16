"""Best-effort desktop notifications using tools already present on the system."""

import shutil
import subprocess


def _applescript_text(value):
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def notify(title, message):
    """Send a non-blocking OS notification when a supported notifier is installed."""
    osascript = shutil.which("osascript")
    if osascript:
        script = 'display notification "{}" with title "{}"'.format(
            _applescript_text(message), _applescript_text(title))
        command = [osascript, "-e", script]
    else:
        notify_send = shutil.which("notify-send")
        if not notify_send:
            return False
        command = [notify_send, str(title), str(message)]
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=3).returncode == 0
    except Exception:
        return False
