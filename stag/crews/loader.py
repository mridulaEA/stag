"""Discover crews on disk and cross-check them against skills and agents."""

from __future__ import annotations

from pathlib import Path

from stag._resources import REPO_ROOT, MissingCatalog, asset_dir
from stag.crews.schema import Crew, Scenario

CREWS_DIR = asset_dir("crews")
SKILLS_DIR = asset_dir("skills")
AGENTS_DIR = asset_dir("agents")

__all__ = ["CREWS_DIR", "SKILLS_DIR", "AGENTS_DIR", "REPO_ROOT", "MissingCatalog", "discover_crews",
           "load_crew", "load_scenario", "validate_crew", "validate_all"]


def discover_crews(crews_dir: Path = CREWS_DIR) -> list[Crew]:
    if not crews_dir.is_dir():
        return []
    crews = []
    for p in sorted(crews_dir.iterdir()):
        if p.is_dir() and (p / "crew.yaml").is_file() and not p.name.startswith((".", "_")):
            crews.append(Crew.from_yaml(p / "crew.yaml"))
    return crews


def load_crew(name: str, crews_dir: Path = CREWS_DIR) -> Crew:
    path = crews_dir / name / "crew.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"crew '{name}' not found under {crews_dir}")
    return Crew.from_yaml(path)


def load_scenario(crew: Crew, scenario: str | Path | None = None) -> Scenario:
    if scenario is None:
        if crew.path is None:
            raise FileNotFoundError("crew has no path; pass a scenario explicitly")
        scenario = crew.path / "scenario.yaml"
    return Scenario.from_yaml(Path(scenario))


def validate_crew(
    crew: Crew, skills_dir: Path = SKILLS_DIR, agents_dir: Path = AGENTS_DIR
) -> list[str]:
    """Return problems: unknown skills/agents, folder/name mismatch, missing scenario."""
    problems: list[str] = []
    if crew.path is not None and crew.path.name != crew.name:
        problems.append(f"folder '{crew.path.name}' must equal crew name '{crew.name}'")
    for skill in crew.skills:
        if not (skills_dir / skill / "SKILL.md").is_file():
            problems.append(f"unknown skill '{skill}'")
    for agent in crew.agents:
        if not (agents_dir / f"{agent}.md").is_file():
            problems.append(f"unknown agent '{agent}'")
    try:
        from stag.triggers import RuleRegistry

        if crew.triggers:
            RuleRegistry.from_specs([t.model_dump() for t in crew.triggers])
    except Exception as e:  # unknown rule type / bad params must fail validation, not the first run
        problems.append(f"invalid trigger spec: {e}")
    if crew.path is not None and not (crew.path / "scenario.yaml").is_file():
        problems.append("missing scenario.yaml (needed for end-to-end checks)")
    elif crew.path is not None:
        try:
            Scenario.from_yaml(crew.path / "scenario.yaml")
        except Exception as e:
            problems.append(f"invalid scenario.yaml: {e}")
    if crew.path is not None and not (crew.path / "README.md").is_file():
        problems.append("missing README.md")
    return problems


def validate_all(crews_dir: Path = CREWS_DIR) -> dict[str, list[str]]:
    report: dict[str, list[str]] = {}
    if not crews_dir.is_dir():
        return report
    for p in sorted(crews_dir.iterdir()):
        if not p.is_dir() or p.name.startswith((".", "_")):
            continue
        try:
            crew = Crew.from_yaml(p / "crew.yaml")
        except Exception as e:  # pydantic / yaml errors are the finding
            report[p.name] = [f"invalid crew.yaml: {e}"]
            continue
        report[crew.name] = validate_crew(crew)
    return report
