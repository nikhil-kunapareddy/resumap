"""The layering rules, enforced: each package may import only the layers below it.

    api  ->  services  ->  llm, storage, rendering, parsing  ->  domain

``main`` is the composition root and may import anything. Only ``api`` and
``main`` know about HTTP. A new import that crosses a line fails here, so the
structure cannot erode one convenient shortcut at a time.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "backend" / "resumap"

# package -> the app packages its modules may import (itself always allowed)
ALLOWED = {
    "domain": set(),
    "config": set(),
    "obs": set(),
    "storage": {"domain"},
    "parsing": {"obs"},
    "rendering": {"obs", "parsing"},
    "llm": {"domain", "obs", "parsing"},
    "services": {"domain", "storage", "llm", "rendering", "parsing", "obs", "config"},
    "api": {"services", "domain", "llm", "obs"},
}
HTTP = ("fastapi", "starlette")


def _package(path: Path) -> str:
    rel = path.relative_to(APP)
    return rel.parts[0].removesuffix(".py")


def _imports(path: Path) -> tuple[set[str], set[str]]:
    """(app packages imported, third-party top-level modules imported) by one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    here = path.relative_to(APP).parent.parts   # e.g. ("llm", "backends")
    app, external = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:   # relative: resolve against this file's package
                base = here[:len(here) - (node.level - 1)] if node.level > 1 else here
                target = list(base) + (node.module.split(".") if node.module else [])
                if target:
                    app.add(target[0])
                else:   # "from .. import obs": the names are the packages
                    app.update(a.name for a in node.names)
            elif node.module:
                external.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            external.update(a.name.split(".")[0] for a in node.names)
    return app, external


MODULES = sorted(p for p in APP.rglob("*.py") if _package(p) in ALLOWED)


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(APP)))
def test_imports_respect_the_layers(path):
    pkg = _package(path)
    app, external = _imports(path)
    crossing = app - ALLOWED[pkg] - {pkg}
    assert not crossing, f"{pkg} may not import {sorted(crossing)}"
    if pkg != "api":
        assert not external & set(HTTP), f"{pkg} must not know about HTTP"


def test_every_package_is_covered():
    """A new top-level package needs a line in ALLOWED before it can land."""
    packages = {_package(p) for p in APP.rglob("*.py")} - {"__init__", "main"}
    assert packages <= set(ALLOWED), sorted(packages - set(ALLOWED))
