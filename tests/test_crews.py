"""End-to-end checks for every crew: definitions validate, scenarios run, invariants hold."""

import pytest

from stag.crews import (
    CREWS_DIR,
    Crew,
    StepAction,
    discover_crews,
    load_scenario,
    run_crew,
    validate_all,
    validate_crew,
)
from stag.crews.runner import DeterministicExecutor
from stag.techops import ActionState, IncidentState

CREWS = discover_crews(CREWS_DIR)
EXPECTED_CREWS = {
    "techops-incident-triage",
    "problem-management",
    "release-management",
    "ai-automation",
    "customer-care",
    "log-management",
    "data-ops",
    "security-governance",
}


def test_all_expected_crews_present_and_valid():
    assert {c.name for c in CREWS} >= EXPECTED_CREWS
    report = validate_all(CREWS_DIR)
    assert {k: v for k, v in report.items() if v} == {}


@pytest.mark.parametrize("crew", CREWS, ids=lambda c: c.name)
def test_crew_structure(crew: Crew):
    assert validate_crew(crew) == []
    assert crew.workflow[0].action is StepAction.COORDINATE
    automated = [r for r in crew.roles if not r.human]
    assert all(not r.permissions.production_mutations for r in automated), "automated roles never mutate"
    humans = [r for r in crew.roles if r.human]
    assert humans, "every crew needs a human approver role"
    for step in crew.workflow:
        if step.action is StepAction.AUTHORIZE:
            policy = step.policy
            assert policy is not None
            if policy.mode == "policy":
                assert policy.auto_if_severity_at_most in ("low", "medium")
            approver = policy.approver_role or step.role
            assert crew.role(approver).human
    assert crew.kpis and crew.guardrails and crew.purpose


def _check_expect(report, crew, scenario, auto):
    e = scenario.expect
    if "incidents" in e:
        assert len(report.incidents) == e["incidents"], crew.name
    if "incidents_at_least" in e:
        assert len(report.incidents) >= e["incidents_at_least"], crew.name
    if "duplicate_deliveries" in e:
        assert report.duplicate_deliveries == e["duplicate_deliveries"], crew.name
    if "evidence_only_updates" in e:
        assert report.evidence_only_updates == e["evidence_only_updates"], crew.name
    if "investigations_at_most" in e:
        assert report.investigations_requested <= e["investigations_at_most"], crew.name
    if "investigations_at_least" in e:
        assert report.investigations_requested >= e["investigations_at_least"], crew.name
    if auto:
        if "operations_at_least" in e:
            assert report.ledger["operations"] >= e["operations_at_least"], crew.name
        if "resolved_with_auto_approve" in e:
            assert report.resolved == e["resolved_with_auto_approve"], crew.name
        if "resolved_with_auto_approve_at_least" in e:
            assert report.resolved >= e["resolved_with_auto_approve_at_least"], crew.name
        if "policy_approved_at_least" in e:
            policy_approvals = sum(
                1 for inc in report.incidents for s in inc.steps
                if s.action is StepAction.AUTHORIZE and str(s.detail.get("authorized_by", "")).startswith("policy:")
            )
            assert policy_approvals >= e["policy_approved_at_least"], crew.name
    else:
        if "held_without_auto_approve" in e:
            assert report.held == e["held_without_auto_approve"], crew.name
        if "held_without_auto_approve_at_least" in e:
            assert report.held >= e["held_without_auto_approve_at_least"], crew.name
        if "resolved_without_auto_approve" in e:
            assert report.resolved == e["resolved_without_auto_approve"], crew.name


@pytest.mark.parametrize("crew", CREWS, ids=lambda c: c.name)
def test_crew_runs_end_to_end_with_human_approval_simulated(crew: Crew):
    scenario = load_scenario(crew)
    report = run_crew(crew, scenario, auto_approve=True)
    assert report.events_ingested == sum(max(1, ev.repeat) for ev in scenario.events)
    assert report.incidents, "scenario must create at least one incident"
    _check_expect(report, crew, scenario, auto=True)

    for inc in report.incidents:
        assert inc.halted_at is None, f"{crew.name}/{inc.incident_id} halted: {inc.halt_reason}"
        assert inc.final_state == IncidentState.RESOLVED.value
        statuses = {s.step: s.status for s in inc.steps}
        assert "failed" not in statuses.values(), statuses
    for note in report.notifications:
        assert note["incident_id"]
    for art in report.artifacts:
        assert art["status"].startswith("draft"), "artifacts stay drafts until reviewed"
    assert report.budget["available"] >= 0
    assert report.budget["reserved"] == 0, "every reservation is settled"


@pytest.mark.parametrize("crew", CREWS, ids=lambda c: c.name)
def test_crew_halts_at_human_gate_without_approval(crew: Crew):
    scenario = load_scenario(crew)
    report = run_crew(crew, scenario, auto_approve=False)
    _check_expect(report, crew, scenario, auto=False)
    for inc in report.incidents:
        if inc.halted_at:
            held = next(s for s in inc.steps if s.status == "held")
            assert held.action is StepAction.AUTHORIZE
            assert inc.final_state == IncidentState.HELD.value
            assert not any(s.action is StepAction.DISPATCH and s.status == "done" for s in inc.steps)


def test_no_operation_is_executed_twice_and_ledger_states_are_terminal():
    crew = next(c for c in CREWS if c.name == "techops-incident-triage")
    from stag.crews.runner import CrewRunner

    runner = CrewRunner(crew, auto_approve=True)
    report = runner.run_scenario(load_scenario(crew))
    assert report.resolved == 2
    for rec in runner.ledger.records:
        assert rec.dispatch_attempts == 1
        assert rec.state in (ActionState.VERIFIED, ActionState.FAILED)
    # the payments incident took the rollback path: two distinct operations, one failed, one verified
    payments = next(i for i in report.incidents if "payments-api" in i.signal_key)
    assert len(payments.operation_ids) == 2
    states = {runner.ledger.get(op).state for op in payments.operation_ids}
    assert states == {ActionState.FAILED, ActionState.VERIFIED}
    verify_steps = [s for s in payments.steps if s.action is StepAction.VERIFY]
    assert [s.detail["verdict"] for s in verify_steps] == ["not_recovered", "healthy"]


def test_unknown_telemetry_never_resolves():
    crew = next(c for c in CREWS if c.name == "log-management")
    scenario = load_scenario(crew)
    scenario.verification = {"default": "unknown"}
    report = run_crew(crew, scenario, auto_approve=True)
    for inc in report.incidents:
        assert inc.final_state == IncidentState.VERIFYING.value
        assert inc.halted_at == "verify"


def test_custom_executor_is_used_for_content():
    class LoudExecutor(DeterministicExecutor):
        def notify(self, crew, step, incident, context):
            msg = super().notify(crew, step, incident, context)
            msg["message"] = "CUSTOM " + msg["message"]
            return msg

    crew = next(c for c in CREWS if c.name == "customer-care")
    report = run_crew(crew, load_scenario(crew), auto_approve=True, executor=LoudExecutor())
    assert report.notifications and all(n["message"].startswith("CUSTOM") for n in report.notifications)


def test_crew_history_is_fully_recorded():
    from stag.crews.runner import CrewRunner

    crew = next(c for c in CREWS if c.name == "security-governance")
    runner = CrewRunner(crew, auto_approve=True)
    runner.run_scenario(load_scenario(crew))
    for incident in runner.coordinator.incidents:
        assert incident.history, "every state change is a recorded transition"
        assert all(t.actor and t.reason for t in incident.history)
        assert incident.history[-1].to_state is IncidentState.RESOLVED
