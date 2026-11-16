"""
Safe handling of git revisions that come from saved run state.

state.json is a plain file inside the project: a value stored there must never be able to
act as a git *option* (e.g. a "commit" of "--output=/some/file" given to `git show`).
Every revision read from state goes through safe_rev(), which only accepts hex object ids
(which can never start with "-"); callers also put "--end-of-options" before it where git
supports that.
"""

import re
from typing import Optional

_HEX_SHA = re.compile(r"^[0-9a-fA-F]{4,64}$")


def safe_rev(value) -> Optional[str]:
    """The value if it is a plain hex commit id, else None."""
    text = str(value or "").strip()
    return text if _HEX_SHA.match(text) else None
