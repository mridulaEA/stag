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


# =========================================================================== recheck N01-N12


def _crew_with_jump_bypass() -> Crew:
    """N01: verify.on_not_recovered → a generic transition to VERIFYING → resolve."""
    data = _crew_dict(LOGS)
    wf = data["workflow"]
    vi = next(i for i, st in enumerate(wf) if st["action"] == "verify")
    wf[vi]["on_not_recovered"] = "force_verifying"
    wf.insert(vi + 1, {"step": "force_verifying", "action": "transition", "to": "verifying", "role": "log_analyst"})
    crew = Crew.model_validate(data)
    crew.path = LOGS.path
    return crew


def test_n01_failed_verdict_cannot_resolve_through_a_workflow_jump():
    crew = _crew_with_jump_bypass()
    scenario = load_scenario(LOGS)
    scenario.verification = {"default": Verdict.NOT_RECOVERED}
    report = run_crew(crew, scenario, auto_approve=True)
    assert report.resolved == 0
    assert report.failed_steps >= 1  # the generic resolve step is refused by the coordinator
    for inc in report.incidents:
        assert inc.final_state != IncidentState.RESOLVED.value
    assert crews_main(["run", "log-management", "--auto-approve"]) == 0  # bundled crews unaffected


def test_n02_reapproval_does_not_replenish_retry_budget():
    from stag.techops import ActionLedger, RetryBudgetExceeded

    ledger = ActionLedger(max_retries=1)
    rec = ledger.prepare("INC", "restart", "svc")
    ledger.authorize(rec.operation_id, "h", 1)
    for _ in range(2):
        ledger.dispatch(rec.operation_id)
        ledger.mark_outcome_unknown(rec.operation_id)
        ledger.reconcile(rec.operation_id, target_state_observed=False)
    ledger.revalidate(rec.operation_id, 2)
    ledger.authorize(rec.operation_id, "h", 2)
    with pytest.raises(RetryBudgetExceeded):
        ledger.dispatch(rec.operation_id)
    assert rec.dispatch_attempts == 2
    ledger.extend_retry_budget(rec.operation_id, by=1, authorized_by="platform-owner", note="post-incident exception")
    ledger.dispatch(rec.operation_id)
    assert rec.dispatch_attempts == 3
    with pytest.raises(ValueError):
        ActionLedger(max_retries=-1)


def test_n03_mutated_terms_are_refused_without_counting_an_attempt():
    from stag.techops import ActionLedger, IntegrityError

    ledger = ActionLedger()
    params = {"replicas": 1}
    rec = ledger.prepare("INC", "scale", "svc", params, preconditions=["reviewed"])
    params["replicas"] = 50  # caller's object is detached
    ledger.authorize(rec.operation_id, "h", 1)
    assert rec.parameters == {"replicas": 1}
    rec.parameters["replicas"] = 999
    rec.preconditions.clear()
    with pytest.raises(IntegrityError):
        ledger.dispatch(rec.operation_id)
    assert rec.dispatch_attempts == 0
    with pytest.raises(IntegrityError):
        ledger.authorize(rec.operation_id, "h", 1)
    view = ledger.view(rec.operation_id)
    view.parameters["replicas"] = 7
    assert ledger.get(rec.operation_id).parameters["replicas"] == 999  # view is detached


def test_n04_replay_cannot_feed_live_rules_or_suppress_live_copies():
    from stag.models.schemas import EventType, Severity, StreamEvent
    from stag.techops import IncidentCoordinator
    from stag.triggers import TriggerEngine
    from stag.triggers.rules import FrequencyRule, RuleRegistry

    reg = RuleRegistry()
    reg.register(FrequencyRule("burst", window_seconds=60, max_events=2, group_by="user"))
    engine = TriggerEngine(reg)
    coord = IncidentCoordinator()

    def ev(replay=False, id=None):
        kw = dict(source="s", event_type=EventType.LOG, severity=Severity.LOW, payload={"user": "u"},
                  metadata={"service": "x", "environment": "p", **({"replay": True} if replay else {})})
        if id:
            kw["id"] = id
        return StreamEvent(**kw)

    for _ in range(3):
        reg_ = coord.register(ev(replay=True))
        assert not reg_.accepted and reg_.replay  # never reaches the stateful rule
    live = ev()
    r = coord.register(live)
    assert r.accepted
    assert engine.evaluate(live) is None  # live count is 1, not 4
    coord2 = IncidentCoordinator()
    coord2.register(ev(replay=True, id="same"))
    assert coord2.register(ev(id="same")).accepted  # replay copy did not consume the live identity


def test_n04_orchestrator_ignores_replay_tagged_events():
    import asyncio

    from stag.config import Settings
    from stag.models.schemas import EventType, Severity, StreamEvent
    from stag.pipeline.orchestrator import PipelineOrchestrator
    from stag.techops import IncidentCoordinator
    from stag.triggers import TriggerEngine

    class R:
        added = 0

        async def initialize(self): ...

        async def add_event(self, e):
            self.added += 1

        async def search(self, q, top_k=8):
            from stag.models.schemas import RetrievalResult

            return RetrievalResult(query=q)

        stats = {}

    class G:
        async def initialize(self): ...

        async def generate(self, e, t, r):
            raise AssertionError("replay must never reach generation")

        stats = {}

    class S:
        async def start(self): ...

        async def stop(self): ...

        async def events(self):
            return
            yield

    r = R()
    orch = PipelineOrchestrator(S(), TriggerEngine(), r, G(), Settings(), coordinator=IncidentCoordinator())
    msgs = []

    async def cb(m):
        msgs.append(m["type"])

    orch.on_output(cb)
    ev = StreamEvent(source="s", event_type=EventType.DATA_TRANSFER, severity=Severity.CRITICAL,
                     payload={"data_type": "source_code", "sanctioned": False}, metadata={"replay": True})
    asyncio.run(orch.inject_event(ev))
    assert msgs == ["incident_update"] and r.added == 0 and orch.stats["triggers_fired"] == 0


def test_n05_canonical_keys_do_not_collide_anywhere():
    from stag.models.schemas import EventType, StreamEvent
    from stag.techops import SignalKey

    assert SignalKey(service="a|b").key != SignalKey(service="a%7Cb").key
    assert SignalKey(extra={"a": "b,c=d"}).key != SignalKey(extra={"a": "b", "c": "d"}).key
    assert SignalKey(service="pay", symptom_class="x").key != SignalKey(service="pay|x").key
    assert SignalKey(service="ünïcode|x").key != SignalKey(service="ünïcode", symptom_class="x").key
    a = StreamEvent(source="a::b", event_type=EventType.LOG, id="c")
    b = StreamEvent(source="a", event_type=EventType.LOG, id="b::c")
    assert a.doc_key != b.doc_key
    # equal semantic mappings yield equal keys regardless of insertion order
    assert SignalKey(extra={"a": "1", "b": "2"}).key == SignalKey(extra={"b": "2", "a": "1"}).key


def test_n05_sparse_store_keeps_both_observations():
    import asyncio

    pytest.importorskip("rank_bm25")
    from stag.knowledge.sparse_store import SparseStore
    from stag.models.schemas import EventType, StreamEvent

    store = SparseStore()
    asyncio.run(store.add_event(StreamEvent(source="a::b", event_type=EventType.LOG, id="c", payload={"m": "x"})))
    asyncio.run(store.add_event(StreamEvent(source="a", event_type=EventType.LOG, id="b::c", payload={"m": "y"})))
    assert store.count == 2


def test_n06_escalation_holds_even_when_findings_are_complete():
    class Escalating(DeterministicExecutor):
        def investigate(self, *a, **k):
            findings = super().investigate(*a, **k)
            findings.escalate = True
            return findings

    report = run_crew(RELEASE, load_scenario(RELEASE), auto_approve=True, executor=Escalating())
    assert report.resolved == 0
    for inc in report.incidents:
        assert inc.final_state == IncidentState.HELD.value and not inc.operation_ids
        held = next(s for s in inc.steps if s.status == "held")
        assert held.detail["disposition"] == "escalated"


def test_n07_integer_verdicts_and_empty_evidence_never_verify():
    class One(DeterministicExecutor):
        def verify(self, crew, incident, action, hint):
            return 1, {}

    report = run_crew(LOGS, load_scenario(LOGS), auto_approve=True, executor=One())
    assert report.resolved == 0 and all(i.halted_at == "verify" for i in report.incidents)

    from stag.techops import ActionLedger, IncidentCoordinator, VerificationResult

    ledger = ActionLedger()
    rec = ledger.prepare("INC", "restart", "svc")
    ledger.authorize(rec.operation_id, "h", 1)
    ledger.dispatch(rec.operation_id)
    with pytest.raises(ValueError):
        ledger.verify(rec.operation_id, healthy=1, evidence={"source": "p", "window": "1m", "signals": ["x"]})
    with pytest.raises(ValueError):
        ledger.verify(rec.operation_id, healthy=True, evidence={})
    assert rec.state.value == "dispatched"
    # simulated evidence is refused by a live coordinator
    live = IncidentCoordinator()
    from stag.models.schemas import EventType, Severity, StreamEvent, TriggerResult, TriggerType

    ev = StreamEvent(source="s", event_type=EventType.ALERT, severity=Severity.HIGH, metadata={"service": "x", "environment": "p"})
    d = live.ingest(ev, TriggerResult(trigger_type=TriggerType.THRESHOLD, rule_name="r", fired=True, confidence=0.9, event_id=ev.id, reason="t"))
    from stag.techops import InvalidTransition

    with pytest.raises(InvalidTransition):
        live.record_verification(d.incident_id, VerificationResult(healthy=True, source="fixture", window="1m", signals=["x"], simulated=True))


def test_n08_blocked_index_does_not_delay_detection():
    import asyncio

    from stag.config import Settings
    from stag.models.schemas import (
        EventType,
        GeneratedInsight,
        RetrievalResult,
        Severity,
        StreamEvent,
    )
    from stag.pipeline.orchestrator import PipelineOrchestrator
    from stag.triggers import TriggerEngine

    class BlockedRetriever:
        def __init__(self):
            self.gate = None

        async def initialize(self): ...

        async def add_event(self, e):
            await self.gate.wait()

        async def search(self, q, top_k=8):
            return RetrievalResult(query=q)

        stats = {}

    class G:
        async def initialize(self): ...

        async def generate(self, e, t, r):
            return GeneratedInsight(trigger_id=t.trigger_id, event_id=e.id, title="t", summary="s", detail="d", severity=e.severity)

        stats = {}

    class S:
        async def start(self): ...

        async def stop(self): ...

        async def events(self):
            return
            yield

    async def main():
        r = BlockedRetriever()
        r.gate = asyncio.Event()
        settings = Settings(index_timeout_seconds=0.3)
        orch = PipelineOrchestrator(S(), TriggerEngine(), r, G(), settings)
        msgs = []

        async def cb(m):
            msgs.append(m["type"])

        orch.on_output(cb)
        ev = StreamEvent(source="s", event_type=EventType.DATA_TRANSFER, severity=Severity.CRITICAL,
                         payload={"data_type": "source_code", "sanctioned": False})
        task = asyncio.create_task(orch.inject_event(ev))
        await asyncio.sleep(0.05)
        assert msgs == ["trigger"], "detection must be emitted while indexing is blocked"
        await task  # index timeout fires; the insight still completes
        assert msgs == ["trigger", "insight"]

    asyncio.run(main())


def test_n09_leaf_symlink_uninstall_removes_only_the_link(tmp_path: Path):
    proj = _skill_tree(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    res = install(("codex",), "project", proj / "skills", other, tmp_path / "home")
    assert res[0][3] == "linked"
    link = other / ".agents" / "skills" / "stag-demo"
    assert link.is_symlink()
    out = uninstall(("codex",), "project", proj / "skills", other, tmp_path / "home")
    assert out == [("codex", "stag-demo", "removed")]
    assert not link.exists() and not link.is_symlink()
    assert (proj / "skills" / "stag-demo" / "SKILL.md").is_file()
    # force re-install through a leaf link is fine; through the discovery parent link is still protected
    install(("codex",), "project", proj / "skills", other, tmp_path / "home", force=True)
    assert install(("codex",), "project", proj / "skills", proj, tmp_path / "home", force=True)[0][3] == "source"


def test_n10_unexpected_model_object_is_degraded_not_llm():
    from stag.config import Settings
    from stag.generation.generator import InsightGenerator
    from stag.models.schemas import (
        EventType,
        RetrievalResult,
        Severity,
        StreamEvent,
        TriggerResult,
        TriggerType,
    )

    gen = InsightGenerator(Settings())
    ev = StreamEvent(source="s", event_type=EventType.ANOMALY, severity=Severity.HIGH)
    tr = TriggerResult(trigger_type=TriggerType.THRESHOLD, rule_name="r", fired=True, confidence=0.5, event_id=ev.id, reason="x")
    for bad in ({"unexpected": "not an insight"}, {"title": "t"}, {"summary": "s", "detail": "d", "severity": "weird"}):
        ins = gen._build_insight(ev, tr, RetrievalResult(query="q"), bad, "llm", None)
        assert ins.generation_mode == "degraded" and ins.degraded_reason, bad


def test_n12_dead_stream_fails_readiness():
    import asyncio

    from stag.config import Settings
    from stag.models.schemas import RetrievalResult
    from stag.pipeline.orchestrator import PipelineOrchestrator
    from stag.triggers import TriggerEngine

    class R:
        async def initialize(self): ...

        async def add_event(self, e): ...

        async def search(self, q, top_k=8):
            return RetrievalResult(query=q)

        stats = {}

    class G:
        async def initialize(self): ...

        stats = {}

    class Dead:
        async def start(self): ...

        async def stop(self): ...

        async def events(self):
            raise RuntimeError("boom")
            yield

    async def main():
        orch = PipelineOrchestrator(Dead(), TriggerEngine(), R(), G(), Settings())
        await orch.start()
        await asyncio.sleep(0.05)
        assert not orch.healthy and orch.status == "failed" and "boom" in orch.last_error
        await orch.start()  # controlled recovery: restart is allowed after a failure
        assert orch._task is not None
        await orch.stop()

    asyncio.run(main())
