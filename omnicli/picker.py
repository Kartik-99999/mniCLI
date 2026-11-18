"""
Tiny numbered-menu pickers for interactive choices (setup wizard, chat /lead /model ...).
Pure input()/print() - works in any terminal, and tests inject input_fn/out.
"""

from typing import Callable, List, Optional, Sequence, Tuple

from omnicli.ui import c_bold, c_cyan, c_dim, c_green, c_yellow

Option = Tuple[str, str]  # (value, label)
CANCEL = object()


def _ask(input_fn: Callable[[str], str], prompt: str) -> Optional[str]:
    try:
        return input_fn(c_bold(c_yellow(prompt))).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def choose_one(title: str, options: Sequence[Option], current: Optional[str] = None,
               input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print,
               allow_custom: bool = False, custom_hint: str = "or type a name",
               validate: Optional[Callable[[str], Optional[str]]] = None):
    """Returns the chosen value, None to keep the current one (Enter), or CANCEL (q / Ctrl-D).
    `validate(value)` may return an error message to reject a typed custom value."""
    out(c_bold(title))
    for i, (value, label) in enumerate(options, 1):
        mark = c_green("  ✓ current") if current is not None and value == current else ""
        out(f"  {c_cyan(str(i).rjust(2))}) {c_bold(value)}  {c_dim(label)}{mark}")
    keep = f"Enter = keep {current}" if current else "Enter = keep default"
    hint = f"Type a number{', ' + custom_hint if allow_custom else ''} ({keep}; q = cancel): "
    for _ in range(3):
        ans = _ask(input_fn, hint)
        if ans is None or ans.lower() in ("q", "quit", "cancel"):
            return CANCEL
        if ans == "":
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1][0]
        if allow_custom and not ans.isdigit():
            err = validate(ans) if validate else None
            if not err:
                return ans
            out(f"  {err}")
            continue
        out(f"  Please enter a number from 1 to {len(options)}" + (" or a name" if allow_custom else ""))
    return CANCEL


def choose_many(title: str, options: Sequence[Option], current: Sequence[str],
                input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print):
    """Returns a list of chosen values, None to keep the current selection, or CANCEL."""
    out(c_bold(title))
    for i, (value, label) in enumerate(options, 1):
        mark = c_green("  ✓") if value in current else ""
        out(f"  {c_cyan(str(i).rjust(2))}) {c_bold(value)}  {c_dim(label)}{mark}")
    for _ in range(3):
        ans = _ask(input_fn, "Numbers separated by commas, e.g. 1,3 (a = all, Enter = keep, q = cancel): ")
        if ans is None or ans.lower() in ("q", "quit", "cancel"):
            return CANCEL
        if ans == "":
            return None
        if ans.lower() in ("a", "all"):
            return [v for v, _ in options]
        picks: List[str] = []
        ok = True
        for part in ans.replace(" ", ",").split(","):
            if not part:
                continue
            if part.isdigit() and 1 <= int(part) <= len(options):
                v = options[int(part) - 1][0]
                if v not in picks:
                    picks.append(v)
            else:
                ok = False
        if ok and picks:
            return picks
        out(f"  Please use numbers from 1 to {len(options)}, separated by commas")
    return CANCEL


def ask_number(label: str, current, cast=int, minimum=0, input_fn: Callable[[str], str] = input,
               out: Callable[[str], None] = print):
    """Returns a number, None to keep the current value, or CANCEL."""
    for _ in range(3):
        ans = _ask(input_fn, f"{label} (Enter = keep {current}): ")
        if ans is None:
            return CANCEL
        if ans == "":
            return None
        try:
            v = cast(ans)
            if v < minimum:
                raise ValueError
            return v
        except ValueError:
            out(f"  Please enter a number ≥ {minimum}")
    return CANCEL
