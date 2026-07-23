"""DEV-001 acceptance: production packages must not inherit the spike.

The spike package is frozen historical evidence. It still carries superseded
Finance names and a default personal scope, so production code must never
import it or reuse its tool vocabulary.
"""

from __future__ import annotations

import ast
from pathlib import Path


SRC = Path(__file__).parents[2] / "src"
PRODUCTION_PACKAGES = ("personal_agent", "personal_data_mcp")
SPIKE_PACKAGE = "personal_agent_spike"
OBSOLETE_TOOL_NAMES = (
    "finance.query_transactions",
    "finance.analyze_period",
    "meta.get_capabilities",
)


def production_modules() -> list[Path]:
    return sorted(
        path
        for package in PRODUCTION_PACKAGES
        for path in (SRC / package).rglob("*.py")
    )


def imported_module_names(tree: ast.AST) -> list[tuple[str, int]]:
    names: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append((node.module or "", node.lineno))
    return names


def test_production_packages_are_importable_and_non_empty() -> None:
    for package in PRODUCTION_PACKAGES:
        assert (SRC / package / "__init__.py").is_file()
    assert production_modules(), "boundary tests would pass vacuously"


def test_production_code_never_imports_the_spike() -> None:
    offenders = []
    for path in production_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name, lineno in imported_module_names(tree):
            if name == SPIKE_PACKAGE or name.startswith(f"{SPIKE_PACKAGE}."):
                offenders.append(f"{path.relative_to(SRC)}:{lineno} imports {name}")
    assert offenders == []


def test_production_code_never_names_obsolete_tools() -> None:
    offenders = []
    for path in production_modules():
        text = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(SRC)} references {tool}"
            for tool in OBSOLETE_TOOL_NAMES
            if tool in text
        )
    assert offenders == []
