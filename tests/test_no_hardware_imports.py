"""Guard: the test suite must run with no BioCAM and no 3Brain DLLs.

This is stronger than "no instrument attached". Importing the interop layer
requires the 3Brain assemblies to be present on disk, so no test and no Layer
2/3 module may import `clr` or `pythonnet` at all.

If this test fails, the layer boundary described in biocam/__init__.py has been
broken, and the suite is about to become unrunnable on a development machine.

This scan is fail-SAFE by construction: every `.py` file under the repository
root is checked by default, and only the two paths in EXEMPT_DIRS are excused.
A new file or a new top-level module is guarded automatically, with no need
for anyone to remember to add it to an allowlist.
"""

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Directory names to skip while walking. Pure noise (VCS metadata, caches,
# build output) — not an architectural exemption from the import rule.
SKIP_DIR_NAMES = {
    ".git",
    ".superpowers",
    "__pycache__",
    ".venv",
    "venv",
    "build",
    "dist",
    ".vs",
    "obj",
    "bin",
}

# Paths permitted to import the interop stack. Keep this list to exactly
# these two entries, each justified. Everything else under the repo root is
# guarded by default (see module docstring).
EXEMPT_DIRS = [
    # The one package permitted to import clr — the architectural exemption.
    REPO_ROOT / "biocam" / "interop",
    # Legacy scripts predating the layer split. Verified: connector.py
    # imports pythonnet and clr; Hello_BioCam.py imports pythonnet, clr_loader
    # and clr. Phase 1 restructures these into biocam/interop/; until then
    # they must not fail the suite.
    REPO_ROOT / "BioCam_DupleX_API",
]

FORBIDDEN_ROOTS = {"clr", "pythonnet", "clr_loader"}


def _under_skipped_dir(path):
    return any(parent.name in SKIP_DIR_NAMES for parent in path.parents)


def _is_exempt(path):
    return any(exempt == path or exempt in path.parents for exempt in EXEMPT_DIRS)


def _guarded_python_files():
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if _under_skipped_dir(path):
            continue
        if _is_exempt(path):
            continue
        yield path


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


@pytest.mark.parametrize(
    "exempt_dir", EXEMPT_DIRS, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_exempt_dirs_exist(exempt_dir):
    """A typo in EXEMPT_DIRS would silently widen the hole and go unnoticed."""
    assert exempt_dir.exists(), (
        f"{exempt_dir.relative_to(REPO_ROOT)} does not exist. "
        "A stale or misspelled entry in EXEMPT_DIRS exempts nothing and "
        "should be removed; a missing real exemption must be fixed."
    )
