"""
ANSI Terminal UI and formatting engine for OmniCLI.
Zero external dependencies, fast and rich output formatting.
"""

import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple


class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"

    BG_BLUE = "\033[44m"
    BG_GREEN = "\033[42m"
    BG_RED = "\033[41m"
    BG_DARK = "\033[100m"


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty"):
        return False
    return sys.stdout.isatty()


USE_COLOR = _supports_color()


def colorize(text: str, *styles: str) -> str:
    if not USE_COLOR:
        return text
    prefix = "".join(styles)
    return f"{prefix}{text}{Colors.RESET}"


def c_green(text: str) -> str:
    return colorize(text, Colors.BRIGHT_GREEN)


def c_red(text: str) -> str:
    return colorize(text, Colors.BRIGHT_RED)


def c_yellow(text: str) -> str:
    return colorize(text, Colors.BRIGHT_YELLOW)


def c_blue(text: str) -> str:
    return colorize(text, Colors.BRIGHT_BLUE)


def c_cyan(text: str) -> str:
    return colorize(text, Colors.BRIGHT_CYAN)


def c_magenta(text: str) -> str:
    return colorize(text, Colors.BRIGHT_MAGENTA)


def c_bold(text: str) -> str:
    return colorize(text, Colors.BOLD)


def c_dim(text: str) -> str:
    return colorize(text, Colors.DIM)


# Badges
BADGE_SUCCESS = colorize("[✓ DONE]   ", Colors.BOLD, Colors.BRIGHT_GREEN)
BADGE_FAILED = colorize("[✗ FAILED] ", Colors.BOLD, Colors.BRIGHT_RED)
BADGE_RUNNING = colorize("[⚡ RUNNING]", Colors.BOLD, Colors.BRIGHT_YELLOW)
BADGE_SKIP = colorize("[↷ SKIP]   ", Colors.DIM, Colors.CYAN)
BADGE_INFO = colorize("[ℹ INFO]   ", Colors.BRIGHT_BLUE)
BADGE_WARN = colorize("[⚠ WARN]   ", Colors.BRIGHT_YELLOW)


def print_banner():
    banner = f"""
{Colors.BRIGHT_CYAN}  ██████╗ ███╗   ███╗███╗   ██╗██╗ ██████╗██╗     ██╗
 ██╔═══██╗████╗ ████║████╗  ██║██║██╔════╝██║     ██║
 ██║   ██║██╔████╔██║██╔██╗ ██║██║██║     ██║     ██║
 ██║   ██║██║╚██╔╝██║██║╚██╗██║██║██║     ██║     ██║
 ╚██████╔╝██║ ╚═╝ ██║██║ ╚████║██║╚██████╗███████╗██║
  ╚═════╝ ╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝ ╚═════╝╚══════╝╚═╝{Colors.RESET}
 {Colors.DIM}Universal CLI Orchestrator & Multi-Tool Project Conductor{Colors.RESET}
"""
    if not USE_COLOR:
        banner = "\n=== OmniCLI: Universal Multi-Tool Conductor ===\n"
    print(banner)


def print_panel(content: str, title: Optional[str] = None, style: str = Colors.CYAN):
    lines = content.strip().split("\n")
    max_len = max((len(l) for l in lines), default=40)
    if title:
        max_len = max(max_len, len(title) + 4)
    width = min(max(max_len + 4, 60), 100)

    horiz = "─" * (width - 2)
    t_str = f" {title} " if title else ""
    t_len = len(t_str)
    left_bar = "─" * 2
    right_bar = "─" * max(0, (width - 2 - 2 - t_len))

    top = f"{colorize('┌' + left_bar + t_str + right_bar + '┐', style, Colors.BOLD)}"
    bot = f"{colorize('└' + horiz + '┘', style)}"

    print(top)
    for line in lines:
        pad = width - 4 - len(line)
        if pad < 0:
            line = line[: width - 7] + "..."
            pad = 0
        border_l = colorize("│ ", style)
        border_r = colorize(" │", style)
        print(f"{border_l}{line}{' ' * pad}{border_r}")
    print(bot)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def visible_len(text: str) -> int:
    """Printed width of text, ignoring ANSI colour codes."""
    return len(_ANSI_RE.sub("", str(text)))


def pad_visible(text: str, width: int) -> str:
    text = str(text)
    return text + " " * max(0, width - visible_len(text))


def print_table(headers: List[str], rows: List[List[str]], title: Optional[str] = None):
    if not rows:
        print(c_dim("(No data)"))
        return

    col_widths = [visible_len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            if idx < len(col_widths):
                col_widths[idx] = max(col_widths[idx], visible_len(cell))

    # Print title if provided
    if title:
        print(f"\n{c_bold(c_cyan(title))}")

    # Separator
    sep_line = "  " + "  ".join("─" * w for w in col_widths)
    header_line = "  " + "  ".join(
        colorize(f"{h:<{col_widths[i]}}", Colors.BOLD, Colors.BRIGHT_WHITE)
        for i, h in enumerate(headers)
    )

    print(header_line)
    print(colorize(sep_line, Colors.DIM))

    for row in rows:
        cells = []
        for i, h in enumerate(headers):
            val = str(row[i]) if i < len(row) else ""
            cells.append(pad_visible(val, col_widths[i]))
        print("  " + "  ".join(cells))
    print()


def print_step_header(index: int, total: int, step_id: str, cli: str, desc: str):
    prefix = colorize(f"[{index}/{total}]", Colors.BOLD, Colors.BRIGHT_BLUE)
    cli_tag = colorize(f"<{cli}>", Colors.BOLD, Colors.BRIGHT_MAGENTA)
    desc_str = colorize(desc or step_id, Colors.BOLD, Colors.WHITE)
    print(f"\n{prefix} {cli_tag} {desc_str}")


def print_step_command(cmd: str):
    print(f"  {colorize('$', Colors.DIM, Colors.CYAN)} {colorize(cmd, Colors.DIM)}")


def print_step_output_line(cli: str, line: str):
    tag = colorize(f"[{cli}]", Colors.DIM, Colors.CYAN)
    print(f"  {tag} {line.rstrip()}")
