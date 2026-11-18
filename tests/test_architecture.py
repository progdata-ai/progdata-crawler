"""The core must not know how it is called."""

import ast
from pathlib import Path

import pytest

import progdata_crawler

PACKAGE_DIR = Path(progdata_crawler.__file__).parent
CORE_FILES = sorted((PACKAGE_DIR / "core").rglob("*.py"))

# Entry points and the frameworks that belong to them.
FORBIDDEN_IN_CORE = ("progdata_crawler.api", "typer", "click", "fastapi", "starlette", "mcp")


def _imported_modules(file: Path) -> set[str]:
    tree = ast.parse(file.read_text(), filename=str(file))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_core_has_modules() -> None:
    assert CORE_FILES


@pytest.mark.parametrize("file", CORE_FILES, ids=lambda f: str(f.relative_to(PACKAGE_DIR)))
def test_core_does_not_import_entry_points(file: Path) -> None:
    offending = sorted(
        module
        for module in _imported_modules(file)
        for forbidden in FORBIDDEN_IN_CORE
        if module == forbidden or module.startswith(forbidden + ".")
    )
    assert not offending, f"{file.name} imports {offending}"
