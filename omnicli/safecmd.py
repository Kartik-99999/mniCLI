"""
Validation and sandboxing of `verify` commands before OmniCLI runs them on the user's machine.

Verify commands are written by the lead AI, and an AI can be steered by text it reads (prompt
injection). With settings `verify_policy` = "strict" (default) a verify command must be built
only from ALLOWED FORMS - no denylists:

  * python/python3:  [-B|-u|-q|-I|-s|-E] -m <module> ...   or   <script>.py ...
      the module must be a test module (unittest, pytest, ...) or - when run - exist in the project;
      the script must be a relative .py file in the project. Any other dash-option is refused, so
      -c / -Bc / -c'code' style inline code can't slip through.
  * node: --test [paths]  or  <script>.js|.mjs|.cjs
  * npm/pnpm/yarn/bun: test | run <script>        deno: test       go: test|vet|build (no @version)
  * cargo: test|check|build|clippy|fmt   mvn/gradle/swift/dotnet: test-type goals only
  * npx: a known test tool that is ALREADY installed in the project (nothing is downloaded)
  * read-only checks (test, grep, diff, cmp, ls, wc, head, tail, sort, uniq, cat, true, false),
    test runners on PATH (pytest, jest, vitest, mocha, ruff, eslint, tsc, rspec, phpunit), make.
  * joined only with && / || / pipes into read-only tools; redirects only to /dev/null (and 2>&1);
    a leading `!`; no ; ` $ sub-shells, groups, background &, VAR=x prefixes, URLs/scheme args.
  * every argument - including the part after '=', ':' or ',' and after a short option like -I -
    may not start with / (except /dev/null) or ~, and may not contain '..'.

On macOS the command then runs under sandbox-exec: no network, no writes outside the project /
temp / tool caches, and no reads of common secret locations. Elsewhere it runs without a sandbox.
`verify_policy` = "trust" disables the form check (an explicit, user-chosen trade-off).
"""

import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

TEST_MODULES = {"unittest", "pytest", "py_compile", "compileall", "doctest"}
# modules that install, serve, fetch or execute code from their arguments: refused even at plan time
# (at run time anything that isn't a test module must also exist in the project)
RISKY_MODULES = {"pip", "ensurepip", "venv", "virtualenv", "http", "urllib", "smtplib", "smtpd", "ftplib",
                 "telnetlib", "socketserver", "xmlrpc", "webbrowser", "pydoc", "timeit", "cProfile", "profile",
                 "trace", "pdb", "code", "runpy", "site", "zipapp", "asyncio", "idlelib", "turtledemo",
                 "pipx", "uv", "poetry", "twine", "build", "setuptools", "distutils"}
_PY_FLAGS = {"-B", "-u", "-q", "-I", "-s", "-E"}
_PKG_RUNNERS = {"npm", "pnpm", "yarn", "bun"}
_SUBCOMMANDS = {
    "deno": {"test"}, "go": {"test", "vet", "build"}, "cargo": {"test", "check", "build", "clippy", "fmt"},
    "mvn": {"test", "verify", "compile", "package"}, "gradle": {"test", "check", "build"},
    "swift": {"test", "build"}, "dotnet": {"test", "build"},
}
_FREE_ARGS = {  # programs whose arguments are only path-checked
    "test", "[", "grep", "diff", "cmp", "ls", "wc", "head", "tail", "sort", "uniq", "cat", "true", "false",
    "pytest", "jest", "vitest", "mocha", "ruff", "eslint", "tsc", "rspec", "phpunit", "make",
}
ALLOWED_PROGRAMS = {"python", "python3", "node", "npx"} | _PKG_RUNNERS | set(_SUBCOMMANDS) | _FREE_ARGS
PIPE_PROGRAMS = {"grep", "head", "tail", "wc", "sort", "uniq", "diff", "cmp", "cat", "test"}
NPX_TOOLS = {"jest", "vitest", "mocha", "tsc", "eslint", "prettier", "playwright"}
_JOINERS = {"&&", "||", "|"}
_DEV_NULL = "/dev/null"
_WORD = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]*$")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _path_problem(arg: str) -> Optional[str]:
    if arg == _DEV_NULL:
        return None
    check = arg[:-3] if arg.endswith("...") else arg  # Go's recursive package pattern: ./... or pkg/...
    if ".." in check:
        return f"refers to '{arg}' (no '..' allowed)"
    if "://" in arg or arg.startswith(("git+", "github:", "npm:", "file:", "http:", "https:")):
        return f"refers to a URL or remote package ('{arg}')"
    pieces = [arg]
    if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
        pieces.append(arg[2:])  # -I/etc, -o/tmp/x
    for sep in ("=", ":", ","):
        pieces += [p for piece in list(pieces) for p in piece.split(sep)]
    for piece in pieces:
        if piece.startswith("/") and piece != _DEV_NULL or piece.startswith("~"):
            return f"refers to '{arg}', outside the project"
    return None


def _project_has_module(project: Path, module: str) -> bool:
    top = module.split(".")[0]
    for base in (project, project / "src"):
        if (base / top).is_dir() or (base / f"{top}.py").is_file():
            return True
    return False


def _inside(project: Path, rel: str) -> bool:
    try:
        target = (project / rel).resolve()
        return target.is_file() and (target == project.resolve() or project.resolve() in target.parents)
    except OSError:
        return False


def _check_segment(prog: str, args: List[str], project: Optional[Path]) -> Optional[str]:
    if prog in ("python", "python3"):
        i = 0
        while i < len(args) and args[i] in _PY_FLAGS:
            i += 1
        if i >= len(args):
            return "python needs -m <module> or a script"
        if args[i] == "-m":
            if i + 1 >= len(args) or not _MODULE.match(args[i + 1]):
                return "python -m needs a module name"
            module = args[i + 1]
            if module.split(".")[0] in RISKY_MODULES:
                return f"runs module '{module}', which can install, serve or execute code"
            if module.split(".")[0] not in TEST_MODULES and project is not None \
                    and not _project_has_module(project, module):
                return f"runs module '{module}', which is neither a test module nor part of the project"
        elif args[i].startswith("-"):
            return f"uses python option '{args[i]}' (only -m <module> or a script file is allowed)"
        elif not args[i].endswith(".py"):
            return f"runs '{args[i]}' with python (only .py files in the project)"
        elif project is not None and not _inside(project, args[i]):
            return f"runs '{args[i]}', which isn't a file in the project"
        return None
    if prog == "node":
        if not args:
            return "node needs --test or a script"
        if args[0] == "--test":
            return None
        if args[0].startswith("-"):
            return f"uses node option '{args[0]}' (only --test or a script file is allowed)"
        if not args[0].endswith((".js", ".mjs", ".cjs")):
            return f"runs '{args[0]}' with node (only .js files in the project)"
        if project is not None and not _inside(project, args[0]):
            return f"runs '{args[0]}', which isn't a file in the project"
        return None
    if prog in _PKG_RUNNERS:
        if not args:
            return f"{prog} needs 'test' or 'run <script>'"
        if args[0] in ("test", "t"):
            return None
        if args[0] in ("run", "run-script") and len(args) > 1 and _WORD.match(args[1]):
            return None
        return f"runs '{prog} {args[0]}' (only '{prog} test' or '{prog} run <script>' is allowed)"
    if prog == "npx":
        if not args or args[0] not in NPX_TOOLS:
            return "npx may only run a known test tool"
        if project is not None and not (project / "node_modules" / ".bin" / args[0]).exists():
            return f"npx {args[0]} isn't installed in the project (it would be downloaded)"
        return None
    if prog in _SUBCOMMANDS:
        if not args or args[0] not in _SUBCOMMANDS[prog]:
            return f"runs '{prog} {args[0] if args else ''}' (allowed: {', '.join(sorted(_SUBCOMMANDS[prog]))})"
        if prog == "go" and any("@" in a for a in args):
            return "fetches a Go module version (@)"
        return None
    return None  # _FREE_ARGS: arguments are only path-checked


def check_verify_command(cmd: str, project_dir=None) -> Tuple[bool, str]:
    """(True, "") if the command may run, else (False, reason). With project_dir, also check that the
    modules/scripts/tools it runs really belong to the project (use this right before running)."""
    project = Path(project_dir) if project_dir else None
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
        if seg and seg[0] == "!":
            seg = seg[1:]
        if not seg:
            return False, "has an empty part"
        prog, args = seg[0], seg[1:]
        if "=" in prog:
            return False, "sets environment variables"
        if "/" in prog:
            return False, f"runs '{prog}' by path (only known test tools may run)"
        if prog not in ALLOWED_PROGRAMS:
            return False, f"runs '{prog}', which isn't a known test tool"
        if n > 0 and joiners[n - 1] == "|" and prog not in PIPE_PROGRAMS:
            return False, f"pipes into '{prog}'"
        for arg in args:
            problem = _path_problem(arg)
            if problem:
                return False, problem
        problem = _check_segment(prog, args, project)
        if problem:
            return False, problem
    return True, ""


def verify_allowed(cmd: str, policy: str = "strict", project_dir=None) -> Tuple[bool, str]:
    if str(policy or "strict").lower() == "trust":
        return True, ""
    return check_verify_command(cmd, project_dir)


_SECRET_READS = [".ssh", ".aws", ".config/gcloud", ".kube", ".docker", ".gnupg", ".netrc", ".npmrc", ".pypirc",
                 "Library/Keychains", ".codex/auth.json", ".config/muse/auth.json", ".claude/.credentials.json",
                 ".omnicli"]


def _sb_quote(path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_argv(cmd: str, project_dir) -> Optional[List[str]]:
    """argv that runs `cmd` under macOS sandbox-exec (no network, writes only to the project, temp
    dirs and tool caches, no reads of secret locations), or None when no sandbox is available."""
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        return None
    home = Path.home()
    project = Path(project_dir).resolve()
    writable = [project, Path("/private/var/folders"), Path("/private/tmp"), Path("/dev"),
                home / "Library" / "Caches", home / ".cache", home / ".npm"]
    write_rule = " ".join(f"(subpath {_sb_quote(p)})" for p in writable)
    read_rule = " ".join(f"(subpath {_sb_quote(home / s)})" for s in _SECRET_READS)
    profile = ("(version 1)(allow default)(deny network*)"
               f"(deny file-write* (require-not (require-any {write_rule})))"
               f"(deny file-read* {read_rule})")
    return ["sandbox-exec", "-p", profile, "/bin/sh", "-c", cmd]
