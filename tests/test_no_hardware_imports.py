"""Guard: the test suite must run with no BioCAM and no 3Brain DLLs.

This is stronger than "no instrument attached". Importing the interop layer
requires the 3Brain assemblies to be present on disk, so no test and no Layer
2/3 module may import `clr` or `pythonnet` at all.

If this test fails, the layer boundary described in biocam/__init__.py has been
broken, and the suite is about to become unrunnable on a development machine.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directories that must stay free of interop dependencies.
# biocam/interop is deliberately absent: it is the one place `clr` is allowed.
GUARDED_DIRS = [
    REPO_ROOT / "biocam" / "data",
    REPO_ROOT / "biocam" / "analysis",
    REPO_ROOT / "tests",
    REPO_ROOT / "tools",
]

FORBIDDEN_ROOTS = {"clr", "pythonnet", "clr_loader"}


def _guarded_python_files():
    for directory in GUARDED_DIRS:
        if directory.exists():
            yield from sorted(directory.rglob("*.py"))


def _imported_roots(path):
    """Return the top-level module names imported by a Python file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import; it has no top-level root to check.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize(
    "path", list(_guarded_python_files()), ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_module_does_not_import_interop_dependencies(path):
    offending = _imported_roots(path) & FORBIDDEN_ROOTS
    assert not offending, (
        f"{path.relative_to(REPO_ROOT)} imports {sorted(offending)}. "
        "Layers 2 and 3 and the test suite must run with no 3Brain DLLs "
        "installed. Move hardware access into biocam/interop/."
    )


def test_interop_dependencies_are_not_loaded_at_runtime():
    """Nothing collected so far may have pulled in the interop stack."""
    loaded = FORBIDDEN_ROOTS & set(sys.modules)
    assert not loaded, (
        f"{sorted(loaded)} was imported while running the test suite. "
        "Some first-party module reaches the interop layer transitively."
    )
