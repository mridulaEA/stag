"""`stag-skills` — manage the STAG skill pack across agent runtimes.

The canonical skills live in ``<repo>/skills/<name>/SKILL.md`` (Agent Skills format). This tool
keeps them usable from anywhere:

    stag-skills list                       # catalog as agents see it (name + description)
    stag-skills validate                   # frontmatter / naming checks (CI runs this)
    stag-skills install --target all       # install for Claude Code, Codex and Cursor (user scope)
    stag-skills install --target codex --scope project
    stag-skills uninstall --target all

Targets and where each runtime discovers skills:

    claude   project: .claude/skills/<name>        user: ~/.claude/skills/<name>
    codex    project: .agents/skills/<name>        user: ~/.agents/skills/<name>
    cursor   project: .cursor/skills/<name>        user: ~/.cursor/skills/<name>
             (Cursor also reads .agents/skills and .claude/skills)

Project scope links (symlink, falls back to copy); user scope copies. Stdlib only, so it runs
without installing STAG's heavier dependencies:  ``python -m stag.skills ...``
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
AGENTS_DIR = REPO_ROOT / "agents"

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024

TARGET_DIRS: dict[str, dict[str, str]] = {
    "claude": {"project": ".claude/skills", "user": ".claude/skills"},
    "codex": {"project": ".agents/skills", "user": ".agents/skills"},
    "cursor": {"project": ".cursor/skills", "user": ".cursor/skills"},
}
ALL_TARGETS = tuple(TARGET_DIRS)


@dataclass
class Skill:
    name: str
    path: Path
    description: str = ""
    frontmatter: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def domain(self) -> str:
        meta = self.frontmatter.get("metadata") or {}
        return str(meta.get("domain", "")) if isinstance(meta, dict) else ""

    @property
    def version(self) -> str:
        meta = self.frontmatter.get("metadata") or {}
        return str(meta.get("version", "")) if isinstance(meta, dict) else ""


# --------------------------------------------------------------------------- frontmatter parsing


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML-ish frontmatter parser (stdlib only).

    Supports ``key: value`` scalars, quoted strings, and one level of nested mapping (``metadata:``).
    Falls back to PyYAML when available for anything richer.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip("\n")
    body = text[end + 4 :]

    try:  # prefer a real parser when present
        import yaml  # type: ignore

        data = yaml.safe_load(block) or {}
        return (data if isinstance(data, dict) else {}), body
    except ImportError:
        pass

    data: dict = {}
    current: dict | None = None
    for raw in block.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        key, _, value = raw.strip().partition(":")
        value = value.strip().strip('"').strip("'")
        if indent == 0:
            if value == "":
                current = {}
                data[key] = current
            else:
                current = None
                data[key] = value
        elif current is not None:
            current[key] = value
    return data, body


def load_skill(skill_dir: Path) -> Skill:
    skill_file = skill_dir / "SKILL.md"
    skill = Skill(name=skill_dir.name, path=skill_dir)
    if not skill_file.is_file():
        skill.errors.append("missing SKILL.md")
        return skill
    fm, _ = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
    skill.frontmatter = fm
    skill.description = str(fm.get("description", "")).strip()
    return skill


def discover_skills(skills_dir: Path = SKILLS_DIR) -> list[Skill]:
    if not skills_dir.is_dir():
        return []
    return [
        load_skill(p)
        for p in sorted(skills_dir.iterdir())
        if p.is_dir() and not p.name.startswith((".", "_"))
    ]


# --------------------------------------------------------------------------- validation


def validate_skill(skill: Skill) -> list[str]:
    errors = list(skill.errors)
    fm = skill.frontmatter
    name = str(fm.get("name", "")).strip()
    if not name:
        errors.append("frontmatter 'name' is required")
    else:
        if name != skill.path.name:
            errors.append(f"name '{name}' must equal folder name '{skill.path.name}'")
        if len(name) > MAX_NAME:
            errors.append(f"name longer than {MAX_NAME} chars")
        if not NAME_RE.match(name):
            errors.append("name must be lowercase letters, digits and single hyphens")
        if not name.startswith("stag-"):
            errors.append("STAG skills must be prefixed 'stag-'")
    desc = skill.description
    if not desc:
        errors.append("frontmatter 'description' is required")
    elif len(desc) > MAX_DESCRIPTION:
        errors.append(f"description longer than {MAX_DESCRIPTION} chars ({len(desc)})")
    meta = fm.get("metadata")
    if meta is not None and not isinstance(meta, dict):
        errors.append("metadata must be a mapping")
    return errors


def validate_all(skills_dir: Path = SKILLS_DIR) -> dict[str, list[str]]:
    return {s.name: validate_skill(s) for s in discover_skills(skills_dir)}


# --------------------------------------------------------------------------- install / uninstall


def _dest_root(target: str, scope: str, project_dir: Path, home: Path) -> Path:
    rel = TARGET_DIRS[target][scope]
    base = project_dir if scope == "project" else home
    return base / rel


def _link_or_copy(src: Path, dest: Path, mode: str, force: bool) -> str:
    if dest.exists() or dest.is_symlink():
        if not force:
            return "exists"
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        else:
            shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if mode == "link":
        try:
            dest.symlink_to(os.path.relpath(src, dest.parent), target_is_directory=True)
            return "linked"
        except (OSError, NotImplementedError):
            pass  # e.g. Windows without developer mode → copy instead
    shutil.copytree(src, dest)
    return "copied"


def install(
    targets: tuple[str, ...],
    scope: str,
    skills_dir: Path = SKILLS_DIR,
    project_dir: Path | None = None,
    home: Path | None = None,
    force: bool = False,
    only: set[str] | None = None,
) -> list[tuple[str, str, str, str]]:
    """Install skills. Returns (target, skill, destination, outcome) tuples."""
    project_dir = project_dir or Path.cwd()
    home = home or Path.home()
    mode = "link" if scope == "project" else "copy"
    results = []
    for target in targets:
        root = _dest_root(target, scope, project_dir, home)
        for skill in discover_skills(skills_dir):
            if only and skill.name not in only:
                continue
            outcome = _link_or_copy(skill.path, root / skill.name, mode, force)
            results.append((target, skill.name, str(root / skill.name), outcome))
    return results


def uninstall(
    targets: tuple[str, ...],
    scope: str,
    skills_dir: Path = SKILLS_DIR,
    project_dir: Path | None = None,
    home: Path | None = None,
) -> list[tuple[str, str, str]]:
    project_dir = project_dir or Path.cwd()
    home = home or Path.home()
    results = []
    names = {s.name for s in discover_skills(skills_dir)}
    for target in targets:
        root = _dest_root(target, scope, project_dir, home)
        if not root.exists():
            continue
        for child in root.iterdir():
            if child.name in names:
                if child.is_symlink() or child.is_file():
                    child.unlink()
                else:
                    shutil.rmtree(child)
                results.append((target, child.name, "removed"))
    return results


# --------------------------------------------------------------------------- CLI


def _resolve_targets(value: str) -> tuple[str, ...]:
    if value == "all":
        return ALL_TARGETS
    chosen = tuple(t.strip() for t in value.split(",") if t.strip())
    unknown = [t for t in chosen if t not in TARGET_DIRS]
    if unknown:
        raise SystemExit(f"unknown target(s): {', '.join(unknown)}; choose from {', '.join(ALL_TARGETS)}, all")
    return chosen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stag-skills", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skills-dir", type=Path, default=SKILLS_DIR, help="skill pack source directory")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list skills with descriptions")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--domain", help="filter by metadata.domain (techops, development, community)")

    sub.add_parser("validate", help="validate frontmatter and naming for every skill")

    p_inst = sub.add_parser("install", help="install skills for one or more agent runtimes")
    p_inst.add_argument("--target", default="all", help="claude, codex, cursor, comma list, or all")
    p_inst.add_argument("--scope", choices=("user", "project"), default="user")
    p_inst.add_argument("--project-dir", type=Path, default=Path.cwd())
    p_inst.add_argument("--home", type=Path, default=Path.home())
    p_inst.add_argument("--force", action="store_true", help="replace existing entries")
    p_inst.add_argument("--only", help="comma-separated skill names")

    p_un = sub.add_parser("uninstall", help="remove installed STAG skills")
    p_un.add_argument("--target", default="all")
    p_un.add_argument("--scope", choices=("user", "project"), default="user")
    p_un.add_argument("--project-dir", type=Path, default=Path.cwd())
    p_un.add_argument("--home", type=Path, default=Path.home())

    args = parser.parse_args(argv)

    if args.command == "list":
        skills = discover_skills(args.skills_dir)
        if args.domain:
            skills = [s for s in skills if s.domain == args.domain]
        if args.json:
            print(json.dumps([{"name": s.name, "domain": s.domain, "version": s.version, "description": s.description} for s in skills], indent=2))
        else:
            for s in skills:
                tag = f"[{s.domain}] " if s.domain else ""
                print(f"{tag}{s.name}\n    {s.description}\n")
        return 0

    if args.command == "validate":
        report = validate_all(args.skills_dir)
        bad = {k: v for k, v in report.items() if v}
        for name, errs in report.items():
            status = "FAIL" if errs else "ok"
            print(f"{status:4s} {name}")
            for e in errs:
                print(f"       - {e}")
        print(f"\n{len(report) - len(bad)}/{len(report)} skills valid")
        return 1 if bad else 0

    if args.command == "install":
        targets = _resolve_targets(args.target)
        only = {n.strip() for n in args.only.split(",")} if args.only else None
        for target, name, dest, outcome in install(targets, args.scope, args.skills_dir, args.project_dir, args.home, args.force, only):
            print(f"{outcome:7s} {target:6s} {name} -> {dest}")
        return 0

    if args.command == "uninstall":
        targets = _resolve_targets(args.target)
        for target, name, outcome in uninstall(targets, args.scope, args.skills_dir, args.project_dir, args.home):
            print(f"{outcome:7s} {target:6s} {name}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
