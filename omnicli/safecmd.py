"""
Validation of `verify` commands before OmniCLI runs them on the user's machine.

Verify commands are written by the lead AI, and an AI can be steered by text it reads
(prompt injection). So by default (settings `verify_policy` = "strict") a verify command
must be a plain test invocation:

  * every program is a known test/build tool or a read-only utility (ALLOWED_PROGRAMS);
  * no inline code (python3 -c, node -e, ...), no `npx <anything>` except known test tools;
  * only `&&`, `||` and pipes into read-only tools (PIPE_PROGRAMS) join commands;
  * redirects only to /dev/null (and 2>&1); a leading `!` is allowed;
  * no `;`, backticks, `$` expansions, sub-shells/groups, `VAR=x` prefixes, background `&`;
  * no paths outside the project: arguments may not start with `/` (except /dev/null) or `~`,
    or contain `..`.

The tests a verify command runs are still code written by AI agents; this check stops a
planted *command* (curl | sh, rm -rf ~, reading ~/.ssh ...) - it is not a sandbox.
`verify_policy` = "trust" disables the check (an explicit, user-chosen trade-off).
"""

import shlex
from typing import List, Tuple

ALLOWED_PROGRAMS = {
    # test runners / build tools (they run the project's own tests)
    "python3", "python", "pytest", "node", "npm", "pnpm", "yarn", "bun", "deno", "npx", "tsc",
    "go", "cargo", "make", "mvn", "gradle", "java", "swift", "ruby", "rspec", "php", "phpunit",
    "dotnet", "jest", "vitest", "mocha", "ruff", "eslint",
    # read-only checks
    "test", "[", "grep", "diff", "cmp", "ls", "wc", "head", "tail", "sort", "uniq", "cat", "true", "false",
}
PIPE_PROGRAMS = {"grep", "head", "tail", "wc", "sort", "uniq", "diff", "cmp", "cat", "test"}
INLINE_CODE_FLAGS = {
    "python": {"-c"}, "python3": {"-c"}, "node": {"-e", "--eval", "-p", "--print"},
    "bun": {"-e", "--eval", "-p", "--print"}, "deno": {"eval"}, "ruby": {"-e"}, "php": {"-r"},
}
NPX_TOOLS = {"jest", "vitest", "mocha", "tsc", "eslint", "prettier", "playwright"}
_JOINERS = {"&&", "||", "|"}
_DEV_NULL = "/dev/null"


def check_verify_command(cmd: str) -> Tuple[bool, str]:
    """(True, "") if the command may run, else (False, reason)."""
    text = (cmd or "").strip()
    if not text:
        return False, "empty command"
    for bad, why in (("\n", "multiple lines"), ("\r", "multiple lines"), ("`", "backticks"),
                     ("$", "shell variables or $(...)"), (";", "';'")):
        if bad in text:
            return False, f"uses {why}"
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens: List[str] = list(lex)
    except ValueError as ex:
        return False, f"can't be parsed ({ex})"
    if not tokens:
        return False, "empty command"

    segments: List[List[str]] = [[]]
    joiners: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in _JOINERS:
            joiners.append(tok)
            segments.append([])
        elif tok in (">", ">>", "&>"):
            seg = segments[-1]
            if seg and seg[-1].isdigit():
                seg.pop()  # "2>/dev/null": the 2 is a file descriptor, not an argument
            if i + 1 >= len(tokens) or tokens[i + 1] != _DEV_NULL:
                return False, "writes output to a file (only >/dev/null is allowed)"
            i += 1
        elif tok == ">&":
            seg = segments[-1]
            if seg and seg[-1].isdigit():
                seg.pop()
            if i + 1 >= len(tokens) or tokens[i + 1] not in ("1", "2"):
                return False, "unsupported redirect"
            i += 1
        elif tok and all(ch in "();<>|&" for ch in tok):
            return False, f"uses '{tok}'"
        else:
            segments[-1].append(tok)
        i += 1

    for n, seg in enumerate(segments):
        if not seg:
            return False, "has an empty part"
        if seg[0] == "!":
            seg = seg[1:]
            if not seg:
                return False, "has an empty part"
        prog = seg[0]
        if "=" in prog:
            return False, "sets environment variables"
        if "/" in prog:
            return False, f"runs '{prog}' by path (only known test tools may run)"
        if prog not in ALLOWED_PROGRAMS:
            return False, f"runs '{prog}', which isn't a known test tool"
        if n > 0 and joiners[n - 1] == "|" and prog not in PIPE_PROGRAMS:
            return False, f"pipes into '{prog}'"
        if any(arg in INLINE_CODE_FLAGS.get(prog, set()) for arg in seg[1:]):
            return False, f"runs inline code ({prog} {sorted(INLINE_CODE_FLAGS[prog])[0]} ...); put checks in a test file"
        if prog == "npx" and (len(seg) < 2 or seg[1] not in NPX_TOOLS):
            return False, "npx may only run a known test tool"
        for arg in seg[1:]:
            if arg == _DEV_NULL:
                continue
            if arg.startswith("/") or arg.startswith("~") or ".." in arg.split("/"):
                return False, f"refers to '{arg}', outside the project"
    return True, ""


def verify_allowed(cmd: str, policy: str = "strict") -> Tuple[bool, str]:
    if str(policy or "strict").lower() == "trust":
        return True, ""
    return check_verify_command(cmd)
