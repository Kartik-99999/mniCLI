"""
Validation, sandboxing and shell-free execution of `verify` commands.

Verify commands are written by the lead AI, and an AI can be steered by text it reads (prompt
injection). With settings `verify_policy` = "strict" (default):

1. ONE parser (parse_command) turns the command into pipelines of argv lists. The validator and the
   executor both use it, so what is checked is exactly what runs.
2. Each argv must match an ALLOWED FORM (no denylists):
     python/python3  [-B|-u|-q|-I|-s|-E] -m <test module | project module> ...  |  <project .py> ...
     node            --test [paths]  |  <project .js|.mjs|.cjs> ...
     npm/pnpm/yarn/bun  test | run <script>      deno test      go test|vet|build (no @version)
     cargo test|check|build|clippy|fmt     mvn/gradle/swift/dotnet: test-type goals
     npx             a known test tool ALREADY installed in node_modules/.bin (nothing downloaded)
     read-only checks (test, grep, diff, cmp, ls, wc, head, tail, sort, uniq, cat, true, false),
     test runners on PATH (pytest, jest, vitest, mocha, ruff, eslint, tsc, rspec, phpunit), make
   joined only by && / || / pipes into read-only tools; redirects only to /dev/null and 2>&1;
   a leading ! per pipeline; no ; ` $, sub-shells, groups, background &, VAR=x prefixes.
   Every argument piece (also after = : , and short options like -I) may not start with / (except
   /dev/null) or ~, and may not contain '..' (Go's ./... excepted); launcher arguments may not be
   URLs or scheme-prefixed (data:, npm:, ...).
3. run_verify_command() executes the argvs itself - NO SHELL - so nothing is expanded after
   validation (no globs, braces, variables). On macOS every process runs under sandbox-exec:
   no network; reading files inside $HOME is denied except the project and toolchain folders;
   programs inside $HOME (other than the project and toolchains) can't be started, nor
   security/osascript/curl/ssh/...; writes only to the project, temp folders and tool caches.

`verify_policy` = "trust" skips steps 1-2 and runs the text with /bin/sh (still sandboxed unless
`verify_sandbox` = false) - an explicit, user-chosen trade-off.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import time
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
_LAUNCHERS = {"python", "python3", "node", "npx", "deno", "go", "cargo"} | _PKG_RUNNERS
ALLOWED_PROGRAMS = {"python", "python3", "node", "npx"} | _PKG_RUNNERS | set(_SUBCOMMANDS) | _FREE_ARGS
PIPE_PROGRAMS = {"grep", "head", "tail", "wc", "sort", "uniq", "diff", "cmp", "cat", "test"}
NPX_TOOLS = {"jest", "vitest", "mocha", "tsc", "eslint", "prettier", "playwright"}
_DEV_NULL = "/dev/null"
_WORD = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]*$")
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


class Seg:
    """One command in a pipeline."""
    def __init__(self):
        self.argv: List[str] = []
        self.out_null = False      # >/dev/null
        self.err_null = False      # 2>/dev/null
        self.err_to_out = False    # 2>&1


class Pipeline:
    def __init__(self):
        self.negate = False
        self.segs: List[Seg] = [Seg()]


def parse_command(cmd: str) -> Tuple[Optional[List[Tuple[str, Pipeline]]], str]:
    """[(joiner, pipeline), ...] with joiner '' for the first, '&&' or '||' after; or (None, reason)."""
    text = (cmd or "").strip()
    if not text:
        return None, "empty command"
    for bad, why in (("\n", "multiple lines"), ("\r", "multiple lines"), ("`", "backticks"),
                     ("$", "shell variables or $(...)"), (";", "';'")):
        if bad in text:
            return None, f"uses {why}"
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens: List[str] = list(lex)
    except ValueError as ex:
        return None, f"can't be parsed ({ex})"
    chain: List[Tuple[str, Pipeline]] = [("", Pipeline())]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        pipe = chain[-1][1]
        seg = pipe.segs[-1]
        if tok in ("&&", "||"):
            chain.append((tok, Pipeline()))
        elif tok == "|":
            pipe.segs.append(Seg())
        elif tok in (">", ">>", "&>"):
            fd = seg.argv.pop() if seg.argv and seg.argv[-1] in ("1", "2") and tok != "&>" else "1"
            if i + 1 >= len(tokens) or tokens[i + 1] != _DEV_NULL:
                return None, "writes output to a file (only >/dev/null is allowed)"
            if tok == "&>":
                seg.out_null = seg.err_null = True
            elif fd == "2":
                seg.err_null = True
            else:
                seg.out_null = True
            i += 1
        elif tok == ">&":
            fd = seg.argv.pop() if seg.argv and seg.argv[-1] in ("1", "2") else "1"
            if fd != "2" or i + 1 >= len(tokens) or tokens[i + 1] != "1":
                return None, "unsupported redirect (only 2>&1)"
            seg.err_to_out = True
            i += 1
        elif tok and all(ch in "();<>|&" for ch in tok):
            return None, f"uses '{tok}'"
        elif tok == "!" and not seg.argv and len(pipe.segs) == 1 and not pipe.negate:
            pipe.negate = True
        else:
            seg.argv.append(tok)
        i += 1
    for _j, pipe in chain:
        if any(not s.argv for s in pipe.segs):
            return None, "has an empty part"
    return chain, ""


def _path_problem(arg: str, launcher: bool) -> Optional[str]:
    if arg == _DEV_NULL:
        return None
    check = arg[:-3] if arg.endswith("...") else arg  # Go's recursive package pattern: ./... or pkg/...
    if ".." in check:
        return f"refers to '{arg}' (no '..' allowed)"
    if "://" in arg or (launcher and _SCHEME.match(arg)):
        return f"refers to a URL or scheme ('{arg}')"
    pieces = [arg]
    if arg.startswith("-") and not arg.startswith("--") and len(arg) > 2:
        pieces.append(arg[2:])  # -I/etc, -o/tmp/x
    for sep in ("=", ":", ","):
        pieces += [p for piece in list(pieces) for p in piece.split(sep)]
    for piece in pieces:
        if (piece.startswith("/") and piece != _DEV_NULL) or piece.startswith("~"):
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
        root = project.resolve()
        return target.is_file() and root in target.parents
    except OSError:
        return False


def _check_argv(prog: str, args: List[str], project: Optional[Path]) -> Optional[str]:
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
            extra = [a for a in args[1:] if a.startswith("-")]
            return f"uses node option '{extra[0]}' after --test (only test file paths)" if extra else None
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
    modules/scripts/tools it runs really belong to the project (done right before running)."""
    project = Path(project_dir) if project_dir else None
    chain, why = parse_command(cmd)
    if chain is None:
        return False, why
    for _joiner, pipe in chain:
        for n, seg in enumerate(pipe.segs):
            prog, args = seg.argv[0], seg.argv[1:]
            if "=" in prog:
                return False, "sets environment variables"
            if "/" in prog:
                return False, f"runs '{prog}' by path (only known test tools may run)"
            if prog not in ALLOWED_PROGRAMS:
                return False, f"runs '{prog}', which isn't a known test tool"
            if n > 0 and prog not in PIPE_PROGRAMS:
                return False, f"pipes into '{prog}'"
            for arg in args:
                problem = _path_problem(arg, prog in _LAUNCHERS)
                if problem:
                    return False, problem
            problem = _check_argv(prog, args, project)
            if problem:
                return False, problem
    return True, ""


def verify_allowed(cmd: str, policy: str = "strict", project_dir=None) -> Tuple[bool, str]:
    if str(policy or "strict").lower() == "trust":
        return True, ""
    return check_verify_command(cmd, project_dir)


# --------------------------------------------------------------------------- sandbox

_HOME_TOOLCHAIN_READ = ["Library/Python", "Library/Caches", ".cache", ".npm", ".pyenv", ".nvm", ".cargo",
                        ".rustup", "go", ".local/lib", ".deno", ".bun", ".gradle", ".m2", "Library/pnpm",
                        ".volta", ".asdf"]
_HOME_TOOLCHAIN_EXEC = [".pyenv", ".nvm", ".cargo/bin", ".rustup", "go/bin", ".deno/bin", ".bun/bin", ".volta",
                        ".asdf", "Library/pnpm"]
_DENIED_TOOLS = ["/usr/bin/security", "/usr/bin/osascript", "/usr/bin/open", "/usr/bin/curl", "/usr/bin/ssh",
                 "/usr/bin/scp", "/usr/bin/sftp", "/usr/bin/nc", "/usr/bin/ftp", "/usr/bin/git"]


def _sb_quote(path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_prefix(project_dir) -> Optional[List[str]]:
    """["sandbox-exec", "-p", profile] for macOS, or None when no sandbox is available."""
    if sys.platform != "darwin" or not shutil.which("sandbox-exec"):
        return None
    home = Path.home()
    project = Path(project_dir).resolve()
    sub = lambda paths: " ".join(f"(subpath {_sb_quote(p)})" for p in paths)
    readable = [project] + [home / p for p in _HOME_TOOLCHAIN_READ]
    runnable = [project] + [home / p for p in _HOME_TOOLCHAIN_EXEC]
    writable = [project, Path("/private/var/folders"), Path("/private/tmp"), Path("/dev"),
                home / "Library" / "Caches", home / ".cache", home / ".npm"]
    denied_tools = " ".join(f"(literal {_sb_quote(t)})" for t in _DENIED_TOOLS)
    profile = ("(version 1)(allow default)(deny network*)"
               f"(deny file-read-data (subpath {_sb_quote(home)}))"
               f"(allow file-read-data {sub(readable)})"
               f"(deny process-exec (subpath {_sb_quote(home)}) {denied_tools})"
               f"(allow process-exec {sub(runnable)})"
               f"(deny file-write* (require-not (require-any {sub(writable)})))")
    return ["sandbox-exec", "-p", profile]


def sandbox_argv(cmd: str, project_dir) -> Optional[List[str]]:
    """Shell form for verify_policy=trust: ["sandbox-exec", ..., "/bin/sh", "-c", cmd] or None."""
    prefix = sandbox_prefix(project_dir)
    return prefix + ["/bin/sh", "-c", cmd] if prefix else None


# --------------------------------------------------------------------------- execution (no shell)

def _run_pipeline(pipe: Pipeline, cwd: Path, env, prefix: Optional[List[str]], deadline: float) -> Tuple[int, str]:
    """Run one pipeline (a | b | c) without a shell. stderr goes to a temp file so no pipe can deadlock."""
    import tempfile
    procs = []
    errfile = tempfile.TemporaryFile(mode="w+")
    prev_stdout = subprocess.DEVNULL
    try:
        for n, seg in enumerate(pipe.segs):
            exe = shutil.which(seg.argv[0], path=(env or {}).get("PATH")) or shutil.which(seg.argv[0])
            if not exe:
                return 127, f"{seg.argv[0]}: command not found"
            last = n == len(pipe.segs) - 1
            stdout = subprocess.DEVNULL if seg.out_null else subprocess.PIPE
            stderr = subprocess.DEVNULL if seg.err_null else (subprocess.STDOUT if seg.err_to_out else errfile)
            p = subprocess.Popen((prefix or []) + [exe] + seg.argv[1:], cwd=str(cwd), env=env, stdin=prev_stdout,
                                 stdout=stdout, stderr=stderr, text=True)
            if hasattr(prev_stdout, "close"):
                prev_stdout.close()  # the previous process gets SIGPIPE if this one stops reading
            procs.append(p)
            prev_stdout = p.stdout if (not last and p.stdout is not None) else subprocess.DEVNULL
        out, _ = procs[-1].communicate(timeout=max(0.1, deadline - time.time()))
        for p in procs[:-1]:
            p.wait(timeout=max(0.1, deadline - time.time()))
        errfile.seek(0)
        return procs[-1].returncode, (out or "") + errfile.read()
    except subprocess.TimeoutExpired:
        for p in procs:
            p.kill()
        return 124, "verify command timed out"
    finally:
        errfile.close()


def run_verify_command(cmd: str, cwd, env=None, timeout: float = 600, policy: str = "strict",
                       sandbox: bool = True) -> Tuple[int, str]:
    """Run a verify command. strict: validated argvs executed WITHOUT a shell (each sandboxed on macOS).
    trust: the raw text via /bin/sh (sandboxed unless sandbox=False). Returns (exit code, output)."""
    cwd = Path(cwd)
    deadline = time.time() + timeout
    prefix = sandbox_prefix(cwd) if sandbox else None
    if str(policy or "strict").lower() == "trust":
        argv = (prefix or []) + ["/bin/sh", "-c", cmd]
        try:
            r = subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return 124, "verify command timed out"
    ok, why = check_verify_command(cmd, cwd)
    if not ok:
        return 126, f"not run: the command {why}"
    chain, _ = parse_command(cmd)
    status, output = 0, []
    for joiner, pipe in chain:
        if (joiner == "&&" and status != 0) or (joiner == "||" and status == 0):
            continue
        status, out = _run_pipeline(pipe, cwd, env, prefix, deadline)
        if pipe.negate:
            status = 0 if status != 0 else 1
        output.append(out)
        if status == 124 and "timed out" in out:
            break
    return status, "".join(output)
