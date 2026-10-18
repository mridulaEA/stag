"""Negative probes from the October 2026 engineering review (docs/review/FINDINGS.md).

Each test fails on the reviewed revision 16e70b2 and passes after the Phase 0 repairs.
"""

from pathlib import Path

import pytest
import yaml

from stag._resources import asset_dir, asset_source
from stag.crews import (
    CREWS_DIR,
    Crew,
    StepAction,
    load_crew,
    load_scenario,
    run_crew,
    validate_crew,
)
from stag.crews.cli import main as crews_main
from stag.crews.runner import DeterministicExecutor
from stag.crews.schema import Verdict
from stag.skills.cli import install, uninstall
from stag.techops import IncidentState, InvestigationFindings

RELEASE = load_crew("release-management", CREWS_DIR)
LOGS = load_crew("log-management", CREWS_DIR)


def _crew_dict(crew: Crew) -> dict:
    return yaml.safe_load((crew.path / "crew.yaml").read_text())


# --------------------------------------------------------------------------- R06 verification fails closed


def test_missing_verification_hints_never_resolve():
    scenario = load_scenario(LOGS)
    scenario.verification = {}
    report = run_crew(LOGS, scenario, auto_approve=True)
    assert report.resolved == 0
    assert all(i.final_state == IncidentState.VERIFYING.value and i.halted_at == "verify" for i in report.incidents)


def test_misspelled_verdict_is_a_validation_error():
    data = load_scenario(LOGS).model_dump()
    data["verification"] = {"default": "healhty"}
    with pytest.raises(ValueError):
        type(load_scenario(LOGS)).model_validate(data)


def test_executor_returning_garbage_verdict_is_unknown():
    class Garbage(DeterministicExecutor):
        def verify(self, crew, incident, action, hint):
            return "yes please", {"simulated": True}

    report = run_crew(LOGS, load_scenario(LOGS), auto_approve=True, executor=Garbage())
    assert report.resolved == 0 and all(i.halted_at == "verify" for i in report.incidents)


def test_fixture_evidence_is_labelled_simulated():
    report = run_crew(LOGS, load_scenario(LOGS), auto_approve=True)
    assert report.mode == "simulation"
    verify_steps = [s for i in report.incidents for s in i.steps if s.action is StepAction.VERIFY]
    assert verify_steps and all(s.detail["evidence"]["simulated"] is True for s in verify_steps)
    assert "[SIMULATION]" in report.to_markdown()
    assert Verdict("healthy") is Verdict.HEALTHY


# --------------------------------------------------------------------------- R07 policy limits in schema


@pytest.mark.parametrize("ceiling", ["high", "critical"])
def test_policy_ceiling_above_medium_is_rejected(ceiling):
    data = _crew_dict(RELEASE)
    for step in data["workflow"]:
        if step["action"] == "authorize":
            step["policy"]["auto_if_severity_at_most"] = ceiling
    with pytest.raises(ValueError):
        Crew.model_validate(data)


def test_policy_mode_requires_a_ceiling_and_human_mode_forbids_one():
    data = _crew_dict(RELEASE)
    for step in data["workflow"]:
        if step["action"] == "authorize":
            step["policy"] = {"mode": "policy", "approver_role": "release_manager"}
    with pytest.raises(ValueError):
        Crew.model_validate(data)
    for step in data["workflow"]:
        if step["action"] == "authorize":
            step["policy"] = {"mode": "human", "auto_if_severity_at_most": "low", "approver_role": "release_manager"}
    with pytest.raises(ValueError):
        Crew.model_validate(data)


def test_duplicate_role_ids_rejected():
    data = _crew_dict(RELEASE)
    data["roles"].append(dict(data["roles"][0]))
    with pytest.raises(ValueError):
        Crew.model_validate(data)


def test_automated_role_cannot_hold_production_mutations():
    data = _crew_dict(RELEASE)
    data["roles"][0]["permissions"] = {"capabilities": ["read_metrics"], "production_mutations": True}
    with pytest.raises(ValueError):
        Crew.model_validate(data)


def test_missing_human_approver_rejected():
    data = _crew_dict(RELEASE)
    for role in data["roles"]:
        role["human"] = False
        role.pop("agent", None)
    with pytest.raises(ValueError):
        Crew.model_validate(data)


def test_dispatch_requires_authorize_and_verify_before_resolve():
    data = _crew_dict(RELEASE)
    data["workflow"] = [s for s in data["workflow"] if s["action"] != "verify"]
    with pytest.raises(ValueError):
        Crew.model_validate(data)


# --------------------------------------------------------------------------- R08 capability widening


def test_step_cannot_widen_role_capabilities():
    data = _crew_dict(RELEASE)
    for step in data["workflow"]:
        if step["action"] == "investigate":
            step["assignment"] = {"capabilities": ["restart_component"]}
    with pytest.raises(ValueError):
        Crew.model_validate(data)


def test_unknown_capability_in_role_is_rejected():
    data = _crew_dict(RELEASE)
    data["roles"][0]["permissions"] = {"capabilities": ["arbitrary_shell"]}
    with pytest.raises(ValueError):
        Crew.model_validate(data)


# --------------------------------------------------------------------------- R09 partial findings hold


def test_empty_findings_hold_instead_of_dispatching():
    class Empty(DeterministicExecutor):
        def investigate(self, crew, role, incident, run, evidence):
            return InvestigationFindings()

    report = run_crew(RELEASE, load_scenario(RELEASE), auto_approve=True, executor=Empty())
    assert report.resolved == 0
    for inc in report.incidents:
        assert inc.halted_at == "freeze_and_canary_check"
        assert inc.final_state == IncidentState.HELD.value
        assert not inc.operation_ids, "no action may be proposed on partial findings"
        assert "partial findings" in inc.halt_reason


def test_failing_executor_settles_budget():
    class Boom(DeterministicExecutor):
        def investigate(self, crew, role, incident, run, evidence):
            run.record_model_call()
            raise RuntimeError("executor crashed")

    report = run_crew(RELEASE, load_scenario(RELEASE), auto_approve=True, executor=Boom())
    assert report.budget["reserved"] == 0
    assert report.failed_steps >= 1


# --------------------------------------------------------------------------- R24 validation and exit codes


def test_unknown_trigger_type_fails_validation():
    data = _crew_dict(RELEASE)
    data["triggers"].append({"type": "magic", "name": "x", "params": {}})
    crew = Crew.model_validate(data)
    problems = validate_crew(crew)
    assert any("invalid trigger spec" in p for p in problems)


def test_negative_limits_rejected_by_schema():
    data = _crew_dict(RELEASE)
    for step in data["workflow"]:
        if step["action"] == "investigate":
            step["assignment"] = {"max_model_calls": -1}
    with pytest.raises(ValueError):
        Crew.model_validate(data)


def test_cli_run_fails_on_unmet_expectations(tmp_path: Path, capsys):
    scenario = load_scenario(RELEASE).model_dump(mode="json")
    scenario["expect"] = {"incidents": 99}
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(scenario))
    assert crews_main(["run", "release-management", "--scenario", str(path), "--auto-approve"]) == 1
    assert "RUN FAILED" in capsys.readouterr().err


def test_cli_run_succeeds_on_bundled_scenarios(capsys):
    assert crews_main(["run-all", "--auto-approve"]) == 0
    assert crews_main(["run-all"]) == 0


# --------------------------------------------------------------------------- R25 bounded scenarios


def test_oversized_scenarios_rejected():
    from stag.crews.schema import Scenario

    base = load_scenario(RELEASE).model_dump(mode="json")
    base["events"][0]["repeat"] = 10_000
    with pytest.raises(ValueError):
        Scenario.model_validate(base)
    base = load_scenario(RELEASE).model_dump(mode="json")
    base["events"][0]["event_type"] = "nope"
    with pytest.raises(ValueError):
        Scenario.model_validate(base)


# --------------------------------------------------------------------------- R27 installer source safety


def _skill_tree(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / "skills" / "stag-demo").mkdir(parents=True)
    (proj / "skills" / "stag-demo" / "SKILL.md").write_text("---\nname: stag-demo\ndescription: Use when testing.\n---\nbody\n")
    (proj / ".agents").mkdir()
    (proj / ".agents" / "skills").symlink_to(Path("..") / "skills", target_is_directory=True)
    return proj


def test_force_install_through_symlinked_parent_preserves_source(tmp_path: Path):
    proj = _skill_tree(tmp_path)
    results = install(("codex",), "project", proj / "skills", proj, tmp_path / "home", force=True)
    assert results[0][3] == "source"
    assert (proj / "skills" / "stag-demo" / "SKILL.md").is_file()


def test_uninstall_through_symlinked_parent_preserves_source(tmp_path: Path):
    proj = _skill_tree(tmp_path)
    results = uninstall(("codex",), "project", proj / "skills", proj, tmp_path / "home")
    assert results == [("codex", "stag-demo", "kept (canonical source)")]
    assert (proj / "skills" / "stag-demo" / "SKILL.md").is_file()


def test_copy_mode_survives_source_removal(tmp_path: Path):
    proj = _skill_tree(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    install(("codex",), "project", proj / "skills", other, tmp_path / "home", mode="copy")
    import shutil

    shutil.rmtree(proj)
    assert (other / ".agents" / "skills" / "stag-demo" / "SKILL.md").is_file()


def test_unknown_skill_name_and_empty_pack_are_errors(tmp_path: Path):
    proj = _skill_tree(tmp_path)
    with pytest.raises(ValueError):
        install(("codex",), "user", proj / "skills", proj, tmp_path / "home", only={"stag-nope"})
    from stag._resources import MissingCatalog

    with pytest.raises(MissingCatalog):
        install(("codex",), "user", tmp_path / "empty", proj, tmp_path / "home")


# --------------------------------------------------------------------------- R28 catalogs resolve


def test_catalogs_resolve_from_checkout():
    assert asset_source() in ("checkout", "env")
    for name in ("skills", "crews", "agents"):
        assert asset_dir(name, required=True).is_dir()
