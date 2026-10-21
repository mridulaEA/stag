"""Locate STAG's catalog assets (skills, crews, agents) in a checkout *or* an installed package.

The canonical copies live at the repository root so coding agents discover them directly
(``.claude/skills`` and ``.agents/skills`` link to ``skills/``). Wheels bundle a copy under
``stag/_assets/`` (see ``setup.py``). Resolution order:

1. ``STAG_ASSETS_DIR`` environment variable (explicit override)
2. the repository root next to this package, when it has the requested catalog
3. the packaged copy ``stag/_assets/<name>``

A missing catalog is an error for validators, never a silent "0 items valid".
"""

from __future__ import annotations

import os
from pathlib import Path

CATALOGS = ("skills", "crews", "agents")

_PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = _PACKAGE_DIR.parent
PACKAGED_ASSETS = _PACKAGE_DIR / "_assets"


class MissingCatalog(FileNotFoundError):
    pass


def _has_content(path: Path, name: str) -> bool:
    if not path.is_dir():
        return False
    marker = {"skills": "SKILL.md", "crews": "crew.yaml", "agents": None}[name]
    if marker is None:
        return any(p.suffix == ".md" for p in path.iterdir())
    return any((p / marker).is_file() for p in path.iterdir() if p.is_dir())


def asset_dir(name: str, required: bool = False) -> Path:
    """Return the directory for one catalog (``skills`` / ``crews`` / ``agents``)."""
    if name not in CATALOGS:
        raise ValueError(f"unknown catalog '{name}'; choose from {CATALOGS}")
    override = os.environ.get("STAG_ASSETS_DIR")
    candidates = []
    if override:
        candidates.append(Path(override) / name)
    candidates.append(REPO_ROOT / name)
    candidates.append(PACKAGED_ASSETS / name)
    for candidate in candidates:
        if _has_content(candidate, name):
            return candidate
    if required:
        raise MissingCatalog(
            f"STAG catalog '{name}' not found. Looked in: {', '.join(str(c) for c in candidates)}. "
            "Reinstall the package or set STAG_ASSETS_DIR to a checkout."
        )
    return candidates[-2] if not override else candidates[0]


def asset_source() -> str:
    """Where the catalogs come from: 'env', 'checkout' or 'package'."""
    if os.environ.get("STAG_ASSETS_DIR"):
        return "env"
    if _has_content(REPO_ROOT / "skills", "skills"):
        return "checkout"
    return "package"
