"""Tests for the skill pack and the stag-skills tooling."""

from pathlib import Path

from stag.skills import SKILLS_DIR, discover_skills, validate_skill
from stag.skills.cli import install, main, uninstall, validate_all

EXPECTED_TECHOPS = {
    "stag-techops-incident-triage",
    "stag-techops-dedup-correlation",
    "stag-techops-bounded-investigation",
    "stag-techops-remediation-ledger",
    "stag-techops-recovery-verification",
    "stag-techops-postmortem",
    "stag-techops-resilience-tests",
    "stag-techops-enterprise-onboarding",
    "stag-techops-signal-tuning",
}


def test_skill_pack_is_valid():
    report = validate_all(SKILLS_DIR)
    assert report, "no skills discovered"
    assert {k: v for k, v in report.items() if v} == {}


def test_techops_and_multi_agent_skills_present():
    names = {s.name for s in discover_skills(SKILLS_DIR)}
    assert EXPECTED_TECHOPS <= names
    assert "stag-multi-agent-orchestration" in names


def test_descriptions_say_when_to_use():
    for skill in discover_skills(SKILLS_DIR):
        assert "Use when" in skill.description or "Use after" in skill.description, skill.name


def test_agents_match_orchestration_roles():
    agents = {p.stem for p in (SKILLS_DIR.parent / "agents").glob("*.md")}
    assert agents == {
        "stag-incident-coordinator",
        "stag-investigator",
        "stag-remediation-reviewer",
        "stag-recovery-verifier",
    }
    for p in (SKILLS_DIR.parent / "agents").glob("*.md"):
        text = p.read_text()
        assert text.startswith("---") and "name: " in text and "description: " in text


def test_plugin_manifest_points_at_real_paths():
    import json

    root = SKILLS_DIR.parent
    manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "stag-techops"
    for rel in manifest["skills"] + manifest["agents"]:
        assert (root / rel).exists(), rel
    marketplace = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
    assert marketplace["plugins"][0]["name"] == manifest["name"]


def test_project_discovery_links_resolve():
    root = SKILLS_DIR.parent
    for rel in (".claude/skills", ".agents/skills"):
        p = root / rel
        assert p.exists(), rel
        assert (p / "stag-techops-incident-triage" / "SKILL.md").is_file(), rel


def test_validate_rejects_bad_name(tmp_path: Path):
    bad = tmp_path / "Bad_Name"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: other\ndescription: x\n---\nbody\n")
    errors = validate_skill(discover_skills(tmp_path)[0])
    assert any("folder name" in e for e in errors)
    assert any("prefixed" in e for e in errors)


def test_install_and_uninstall_roundtrip(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    results = install(("claude", "codex", "cursor"), "user", SKILLS_DIR, project, home)
    assert all(r[3] == "copied" for r in results)
    assert (home / ".claude" / "skills" / "stag-techops-incident-triage" / "SKILL.md").is_file()
    assert (home / ".agents" / "skills" / "stag-multi-agent-orchestration" / "SKILL.md").is_file()
    assert (home / ".cursor" / "skills" / "stag-skill-authoring" / "SKILL.md").is_file()

    # re-install without force leaves existing entries untouched
    again = install(("claude",), "user", SKILLS_DIR, project, home)
    assert all(r[3] == "exists" for r in again)

    linked = install(("codex",), "project", SKILLS_DIR, project, home)
    assert all(r[3] in ("linked", "copied") for r in linked)
    assert (project / ".agents" / "skills" / "stag-techops-postmortem" / "SKILL.md").is_file()

    removed = uninstall(("claude", "codex", "cursor"), "user", SKILLS_DIR, project, home)
    assert len(removed) == 3 * len(discover_skills(SKILLS_DIR))
    assert not (home / ".claude" / "skills" / "stag-techops-incident-triage").exists()


def test_cli_list_and_validate(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "stag-techops-incident-triage" in out
    assert main(["validate"]) == 0
