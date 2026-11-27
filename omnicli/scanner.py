"""
Device CLI Scanner and Inventory Engine for OmniCLI.
Discovers, categorizes, validates, and benchmarks all command-line tools
available on the host device.
"""

import concurrent.futures
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from omnicli import paths
from omnicli.models import CliCategory, CliTool

# Known CLI metadata catalog: (name, category, version_arg, description, alternatives)
KNOWN_TOOLS = [
    # SCM
    ("git", CliCategory.SCM, "--version", "Git Version Control System", ["gh"]),
    ("gh", CliCategory.SCM, "--version", "GitHub Official CLI", ["git"]),
    ("svn", CliCategory.SCM, "--version", "Apache Subversion", []),
    ("hg", CliCategory.SCM, "--version", "Mercurial SCM", []),
    # Package Managers
    ("npm", CliCategory.PACKAGE_MANAGER, "-v", "Node Package Manager", ["pnpm", "yarn", "bun"]),
    ("pnpm", CliCategory.PACKAGE_MANAGER, "-v", "Fast, disk space efficient package manager", ["npm", "yarn", "bun"]),
    ("yarn", CliCategory.PACKAGE_MANAGER, "-v", "Yarn Package Manager", ["npm", "pnpm", "bun"]),
    ("bun", CliCategory.PACKAGE_MANAGER, "--version", "All-in-one JavaScript runtime & toolkit", ["npm", "node"]),
    ("pip", CliCategory.PACKAGE_MANAGER, "--version", "Python Package Installer", ["pip3", "uv", "poetry"]),
    ("pip3", CliCategory.PACKAGE_MANAGER, "--version", "Python 3 Package Installer", ["pip", "uv", "poetry"]),
    ("poetry", CliCategory.PACKAGE_MANAGER, "--version", "Python packaging and dependency management", ["pip", "pip3"]),
    ("uv", CliCategory.PACKAGE_MANAGER, "--version", "Extremely fast Python package installer and resolver", ["pip"]),
    ("cargo", CliCategory.PACKAGE_MANAGER, "--version", "Rust Package Manager", ["rustc"]),
    ("brew", CliCategory.PACKAGE_MANAGER, "--version", "Homebrew package manager for macOS/Linux", []),
    ("composer", CliCategory.PACKAGE_MANAGER, "--version", "PHP Dependency Manager", []),
    ("gem", CliCategory.PACKAGE_MANAGER, "--version", "RubyGems package manager", []),
    # Runtimes & Compilers
    ("python3", CliCategory.RUNTIME, "--version", "Python 3 Interpreter", ["python"]),
    ("python", CliCategory.RUNTIME, "--version", "Python Interpreter", ["python3"]),
    ("node", CliCategory.RUNTIME, "-v", "Node.js JavaScript Runtime", ["bun", "deno"]),
    ("deno", CliCategory.RUNTIME, "--version", "Secure JavaScript & TypeScript runtime", ["node", "bun"]),
    ("go", CliCategory.RUNTIME, "version", "Go programming language runtime & tools", []),
    ("rustc", CliCategory.RUNTIME, "--version", "Rust compiler", ["cargo"]),
    ("gcc", CliCategory.RUNTIME, "--version", "GNU Compiler Collection", ["clang"]),
    ("clang", CliCategory.RUNTIME, "--version", "Clang C/C++ compiler", ["gcc"]),
    ("java", CliCategory.RUNTIME, "-version", "Java Runtime Environment", []),
    ("javac", CliCategory.RUNTIME, "-version", "Java Programming Language Compiler", []),
    ("swift", CliCategory.RUNTIME, "--version", "Apple Swift Compiler & REPL", []),
    ("php", CliCategory.RUNTIME, "-v", "PHP Command Line Interface", []),
    ("ruby", CliCategory.RUNTIME, "-v", "Ruby Programming Language", []),
    # Containers & Cloud
    ("docker", CliCategory.CONTAINER, "--version", "Docker Container Platform", ["podman", "colima"]),
    ("docker-compose", CliCategory.CONTAINER, "version", "Docker Compose multi-container tool", ["docker"]),
    ("colima", CliCategory.CONTAINER, "version", "Container runtimes on macOS with minimal setup", ["docker"]),
    ("podman", CliCategory.CONTAINER, "--version", "Daemonless container engine", ["docker"]),
    ("kubectl", CliCategory.CONTAINER, "version --client", "Kubernetes command-line tool", []),
    ("helm", CliCategory.CONTAINER, "version", "The Kubernetes Package Manager", []),
    ("terraform", CliCategory.CONTAINER, "version", "Infrastructure as Code tool", []),
    ("aws", CliCategory.CONTAINER, "--version", "AWS Command Line Interface", []),
    # Build & Task Runners
    ("make", CliCategory.BUILD, "--version", "GNU Make utility", ["cmake"]),
    ("cmake", CliCategory.BUILD, "--version", "Cross-platform Make generator", ["make"]),
    ("gradle", CliCategory.BUILD, "-v", "Gradle build tool", ["mvn"]),
    ("mvn", CliCategory.BUILD, "-v", "Apache Maven build tool", ["gradle"]),
    ("npx", CliCategory.BUILD, "-v", "Execute npm package binaries directly", ["npm", "bun"]),
    # Testing & Linting
    ("pytest", CliCategory.LINTER_TEST, "--version", "Python testing framework", ["python3 -m unittest"]),
    ("eslint", CliCategory.LINTER_TEST, "--version", "JavaScript & TypeScript linter", []),
    ("prettier", CliCategory.LINTER_TEST, "--version", "Code formatter", []),
    ("ruff", CliCategory.LINTER_TEST, "--version", "Extremely fast Python linter and formatter", ["flake8", "black"]),
    ("black", CliCategory.LINTER_TEST, "--version", "The uncompromising Python code formatter", ["ruff"]),
    ("mypy", CliCategory.LINTER_TEST, "--version", "Static type checker for Python", []),
    ("tsc", CliCategory.LINTER_TEST, "-v", "TypeScript Compiler & type checker", []),
    # AI & Agentic CLIs
    ("claude", CliCategory.AI, "--version", "Anthropic Claude Code CLI", []),
    ("codex", CliCategory.AI, "--version", "Codex CLI", []),
    ("agy", CliCategory.AI, "--version", "Google Antigravity CLI", ["AGY"]),
    ("AGY", CliCategory.AI, "--version", "Google Antigravity CLI Binary", ["agy"]),
    ("muse", CliCategory.AI, "--version", "Muse Code terminal coding agent", []),
    ("higgsfield", CliCategory.AI, "--version", "Higgsfield AI Generative Studio CLI", []),
    ("kiro-cli", CliCategory.AI, "--version", "Kiro CLI Agent", []),
    # Network & Utilities
    ("curl", CliCategory.NETWORK_UTILITY, "--version", "Command line tool for HTTP requests", ["wget"]),
    ("wget", CliCategory.NETWORK_UTILITY, "--version", "Network downloader", ["curl"]),
    ("jq", CliCategory.NETWORK_UTILITY, "--version", "Command-line JSON processor", []),
    ("openssl", CliCategory.NETWORK_UTILITY, "version", "OpenSSL cryptography toolkit", []),
    ("ssh", CliCategory.NETWORK_UTILITY, "-V", "OpenSSH remote login client", []),
    ("rsync", CliCategory.NETWORK_UTILITY, "--version", "Fast incremental file transfer tool", []),
    ("tar", CliCategory.SYSTEM, "--version", "Archive utility", []),
    ("zip", CliCategory.SYSTEM, "-v", "Package and compress files", []),
    ("unzip", CliCategory.SYSTEM, "-v", "Extract compressed archives", []),
]

INVENTORY_CACHE_PATH = paths.omni_home() / "inventory.json"


class CliScanner:
    """Discovers and caches available CLI tools on the host."""

    def __init__(self, cache_path: Optional[Path] = None):
        self.cache_path = cache_path or (paths.omni_home() / "inventory.json")
        self.tools: Dict[str, CliTool] = {}
        self._load_cache()

    def _load_cache(self) -> bool:
        if self.cache_path.exists():
            try:
                data = json.loads(self.cache_path.read_text(encoding="utf-8"))
                self.tools = {k: CliTool.from_dict(v) for k, v in data.get("tools", {}).items()}
                return True
            except Exception:
                pass
        return False

    def save_cache(self):
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "tools": {k: v.to_dict() for k, v in self.tools.items()}
            }
            self.cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def check_tool(self, name: str, category: CliCategory, version_arg: str, desc: str, alts: List[str]) -> CliTool:
        """Probes a single CLI tool on the system."""
        path = shutil.which(name)
        if not path:
            return CliTool(
                name=name,
                path="",
                version=None,
                category=category.value,
                available=False,
                description=desc,
                alternatives=alts,
            )

        version_str: Optional[str] = None
        try:
            argv = [path] + (shlex.split(version_arg) if version_arg else [])
            res = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3.0,
            )
            raw = (res.stdout or res.stderr or "").strip()
            if raw:
                # Take first line and sanitize
                first_line = raw.split("\n")[0].strip()
                if len(first_line) > 60:
                    first_line = first_line[:57] + "..."
                version_str = first_line
        except Exception:
            version_str = "installed (version unknown)"

        return CliTool(
            name=name,
            path=path,
            version=version_str,
            category=category.value,
            available=True,
            description=desc,
            alternatives=alts,
        )

    def scan(self, force_refresh: bool = False, max_workers: int = 16) -> Dict[str, CliTool]:
        """Scans the system for all known CLIs and caches the result."""
        if not force_refresh and self.tools:
            return self.tools

        discovered: Dict[str, CliTool] = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_name = {
                executor.submit(
                    self.check_tool, name, cat, v_arg, desc, alts
                ): name
                for name, cat, v_arg, desc, alts in KNOWN_TOOLS
            }
            for future in concurrent.futures.as_completed(future_to_name):
                name = future_to_name[future]
                try:
                    tool = future.result()
                    discovered[name] = tool
                except Exception:
                    pass

        self.tools = discovered
        self.save_cache()
        return self.tools

    def is_available(self, cli_name: str) -> bool:
        """Returns True if the CLI is installed and available in PATH."""
        if cli_name in self.tools:
            return self.tools[cli_name].available
        # Fallback check
        return shutil.which(cli_name) is not None

    def get_tool(self, cli_name: str) -> Optional[CliTool]:
        return self.tools.get(cli_name)

    def find_alternative(self, cli_name: str) -> Optional[str]:
        """If cli_name is missing, checks known alternatives to see if any are available."""
        tool = self.tools.get(cli_name)
        if tool and tool.alternatives:
            for alt in tool.alternatives:
                if self.is_available(alt):
                    return alt
        return None

    def get_installed(self) -> List[CliTool]:
        return [t for t in self.tools.values() if t.available]

    def get_missing(self) -> List[CliTool]:
        return [t for t in self.tools.values() if not t.available]

    def get_by_category(self, category: str) -> List[CliTool]:
        return [t for t in self.tools.values() if t.category == category and t.available]
