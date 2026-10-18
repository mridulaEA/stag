"""Build hook: bundle the root catalogs (skills/, crews/, agents/) into the wheel as stag/_assets.

The canonical copies stay at the repository root so coding agents discover them in a checkout;
``stag._resources`` resolves either location at runtime. Metadata lives in pyproject.toml.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent
CATALOGS = ("skills", "crews", "agents")


class BuildPyWithAssets(build_py):
    def run(self) -> None:  # noqa: D401
        super().run()
        target = Path(self.build_lib) / "stag" / "_assets"
        for name in CATALOGS:
            source = ROOT / name
            if not source.is_dir():
                raise SystemExit(f"catalog '{name}' missing at {source}; refusing to build an incomplete package")
            shutil.copytree(
                source,
                target / name,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
            )


setup(cmdclass={"build_py": BuildPyWithAssets})
