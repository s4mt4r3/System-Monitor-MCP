"""Static guardrail for the read-only rule in CLAUDE.md. Do not weaken."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

# Allowlist, not denylist: a denylist missed psutil.Popen, os.posix_spawn,
# open(mode="w"), pathlib writes, shutil and getattr tricks. Any new import must
# be added here on purpose, which forces a review of what it can mutate.
# The package itself (and relative imports) are fine: those files are scanned too.
ALLOWED_MODULES = {"asyncio", "socket", "typing", "psutil", "mcp", "__future__", "sysmon_mcp"}
# Names that allow file writes or dynamic dispatch around the checks below.
FORBIDDEN_NAMES = {
    "open", "eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr",
    "globals", "vars", "__builtins__", "breakpoint",
}
# Mutating psutil.Process methods, psutil's own Popen, and asyncio's process spawners.
FORBIDDEN_ATTRS = {
    "kill", "terminate", "suspend", "resume", "send_signal", "nice", "ionice",
    "cpu_affinity", "rlimit", "Popen", "create_subprocess_exec", "create_subprocess_shell",
}


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] not in ALLOWED_MODULES:
                    found.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if not node.level and (node.module or "").split(".")[0] not in ALLOWED_MODULES:
                found.append(f"from {'.' * node.level}{node.module or ''} import ...")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            found.append(f".{node.attr}")
    return [f"{path.name}:{v}" for v in found]


def test_source_has_no_mutating_or_exec_calls():
    files = list(SRC.rglob("*.py"))
    assert files, "no source files found"
    violations = [v for f in files for v in _violations(f)]
    assert not violations, f"read-only rule violated: {violations}"


BANNED_STRINGS = ("subprocess", "os.system", "kill", "terminate")


def _banned_string_hits(root: Path) -> list[str]:
    # Deliberately blunt: a plain substring match, so comments and docstrings
    # count too. Reword them rather than loosening this.
    hits = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for word in BANNED_STRINGS:
                if word in line:
                    hits.append(f"{path.relative_to(root)}:{lineno}: {word!r}")
    return hits


def test_source_tree_has_no_banned_strings():
    hits = _banned_string_hits(SRC)
    assert not hits, "read-only rule violated:\n" + "\n".join(hits)


def test_banned_string_check_catches_violations(tmp_path):
    (tmp_path / "bad.py").write_text("import psutil\npsutil.Process(1).terminate()\n")
    (tmp_path / "ok.py").write_text("x = 1\n")
    assert _banned_string_hits(tmp_path) == ["bad.py:2: 'terminate'"]


@pytest.mark.parametrize("snippet", [
    'import subprocess',
    'import os\nos.system("x")',
    'import posix',
    'from pathlib import Path',
    'import shutil',
    'import psutil\npsutil.Popen(["rm", "x"])',
    'import psutil\npsutil.Process(1).send_signal(9)',
    'import asyncio\nasyncio.create_subprocess_exec("sh")',
    'open("/tmp/x", mode="w")',
    'getattr(object(), "ki" + "ll")()',
    '__import__("o" + "s")',
    'eval("1")',
])
def test_ast_check_catches_bypasses(tmp_path, snippet):
    path = tmp_path / "bad.py"
    path.write_text(snippet + "\n")
    assert _violations(path), f"not flagged: {snippet!r}"
