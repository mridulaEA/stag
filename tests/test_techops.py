"""Tests for the STAG TechOps deterministic backbone.

These mirror the failure-scenario matrix from the STAG TechOps workflow series:
same event delivered repeatedly, similar messages from different requests, two workers on the
same transition, action succeeds but response is lost, stale approvals, budget exhaustion, and
malicious capability requests from telemetry.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from stag.models import StreamEvent
from stag.models.schemas import EventType, Severity, TriggerResult, TriggerType
from stag.techops import (
    ActionLedger,
    ActionState,
    BoundedRun,
    BudgetExhausted,
    BudgetLedger,
    Incident,
    IncidentCoordinator,
    IncidentState,
    InvalidTransition,
    InvestigationAssignment,
    InvestigationFindings,
    LimitExceeded,
    MaterialChange,
    PermissionDenied,
    VerificationRecord,
    VerificationResult,
    payload_hash,
)
from stag.techops.actions import (
    InvalidActionTransition,  # noqa: F401  (re-exported; kept for clarity)
)
from stag.techops.assignment import Fact


def _event(**kwargs) -> StreamEvent:
    defaults = {
        "source": "checkout-api",
        "event_type": EventType.ANOMALY,
        "severity": Severity.HIGH,
        "payload": {"tool": "db-pool", "data_type": "timeout", "description": "connection pool timeout"},
        "metadata": {"environment": "production", "service": "checkout-api"},
    }
    defaults.update(kwargs)
    return StreamEvent(**defaults)


def _trigger(event: StreamEvent, rule="pool_timeouts", confidence=0.9) -> TriggerResult:
    return TriggerResult(
        trigger_type=TriggerType.THRESHOLD,
        rule_name=rule,
        fired=True,
        confidence=confidence,
        event_id=event.id,
        reason="test",
    )


EVIDENCE = {"source": "probe:health", "window": "15m", "signals": ["error_rate", "p99"]}


def _resolve(coord: IncidentCoordinator, incident_id: str) -> None:
    for state in (IncidentState.TRIAGED, IncidentState.INVESTIGATING, IncidentState.VERIFYING):
        coord.transition(incident_id, state, "op", "test path")
    coord.record_verification(incident_id, VerificationResult(healthy=True, **EVIDENCE))
    coord.transition(incident_id, IncidentState.RESOLVED, "op", "verified")


# --------------------------------------------------------------------------- identity


class TestIdentity:
    def test_payload_hash_is_stable_and_order_independent(self):
        assert payload_hash({"a": 1, "b": 2}) == payload_hash({"b": 2, "a": 1})
        assert payload_hash({"a": 1}) != payload_hash({"a": 2})

    def test_delimiters_inside_fields_cannot_collide(self):
        from stag.techops import ObservationIdentity, SignalKey

        a = ObservationIdentity(source="a|b", event_id="c")
        b = ObservationIdentity(source="a", event_id="b|c")
        assert a.key != b.key
        s1 = SignalKey(service="pay|ments", symptom_class="x")
        s2 = SignalKey(service="pay", symptom_class="ments|x")
        assert s1.key != s2.key

    def test_same_event_id_from_two_sources_has_distinct_doc_keys(self):
        e1 = _event(source="datadog")
        e2 = _event(source="splunk")
        e2 = e2.model_copy(update={"id": e1.id})
        assert e1.doc_key != e2.doc_key


# --------------------------------------------------------------------------- incident lifecycle


class TestIncidentLifecycle:
    def test_valid_path_to_resolved(self):
        inc = Incident(signal_key="k")
        for state in (
            IncidentState.TRIAGED,
            IncidentState.INVESTIGATING,
            IncidentState.ACTION_PROPOSED,
            IncidentState.AUTHORIZED,
            IncidentState.EXECUTING,
            IncidentState.VERIFYING,
        ):
            inc.transition(state, actor="test", reason="step")
        with pytest.raises(InvalidTransition):
            inc.transition(IncidentState.RESOLVED, actor="test", reason="no verification yet")
        inc.record_verification(VerificationRecord(healthy=True, at_version=inc.version, source="probe", window="15m", signals=["p99"]))
        inc.transition(IncidentState.RESOLVED, actor="test", reason="verified")
        assert inc.state is IncidentState.RESOLVED
        assert not inc.is_open
        assert len(inc.history) == 7
        assert inc.version == 8  # 1 + 7 transitions

    def test_stale_or_failed_verification_cannot_resolve(self):
        inc = Incident(signal_key="k")
        for state in (IncidentState.TRIAGED, IncidentState.INVESTIGATING, IncidentState.VERIFYING):
            inc.transition(state, actor="t", reason="s")
        inc.record_verification(VerificationRecord(healthy=False, at_version=inc.version, source="probe", window="15m", signals=["p99"]))
        with pytest.raises(InvalidTransition):
            inc.transition(IncidentState.RESOLVED, actor="t", reason="failed verdict")
        inc.record_verification(VerificationRecord(healthy=True, at_version=inc.version, source="probe", window="15m", signals=["p99"]))
        inc.attach_observation("obs-new")  # new evidence → verification is stale
        with pytest.raises(InvalidTransition):
            inc.transition(IncidentState.RESOLVED, actor="t", reason="stale verdict")

    def test_agent_cannot_jump_to_resolved(self):
        inc = Incident(signal_key="k")
        with pytest.raises(InvalidTransition):
            inc.transition(IncidentState.RESOLVED, actor="agent", reason="I think it's fixed")

    def test_terminal_state_has_no_exits(self):
        inc = Incident(signal_key="k")
        inc.transition(IncidentState.DISMISSED, actor="operator", reason="false positive")
        with pytest.raises(InvalidTransition):
            inc.transition(IncidentState.TRIAGED, actor="operator", reason="oops")

    def test_duplicate_delivery_does_not_change_version(self):
        inc = Incident(signal_key="k")
        v = inc.version
        inc.record_duplicate_delivery()
        assert inc.version == v
        assert inc.duplicate_delivery_count == 1


# --------------------------------------------------------------------------- coordinator


class TestCoordinatorDedup:
    def test_same_event_delivered_repeatedly_counts_once(self):
        coord = IncidentCoordinator()
        ev = _event()
        tr = _trigger(ev)
        first = coord.ingest(ev, tr)
        second = coord.ingest(ev, tr)
        third = coord.ingest(ev, tr)

        assert first.accepted and first.investigation_request is not None
        assert second.duplicate and not second.accepted
        assert third.duplicate
        inc = coord.get_incident(first.incident_id)
        assert inc.observation_count == 1
        assert inc.duplicate_delivery_count == 2
        assert coord.stats["investigations_requested"] == 1

    def test_same_identity_different_payload_is_quarantined(self):
        coord = IncidentCoordinator()
        ev = _event()
        coord.ingest(ev, _trigger(ev))
        conflicting = ev.model_copy(update={"payload": {**ev.payload, "description": "changed"}})
        decision = coord.ingest(conflicting, _trigger(conflicting))
        assert decision.conflicting and not decision.accepted
        assert len(coord.quarantine) == 1

    def test_similar_messages_from_different_requests_all_count(self):
        coord = IncidentCoordinator()
        decisions = [coord.ingest(e, _trigger(e)) for e in (_event(), _event(), _event())]
        incident_ids = {d.incident_id for d in decisions}
        assert len(incident_ids) == 1
        inc = coord.get_incident(decisions[0].incident_id)
        assert inc.observation_count == 3
        assert inc.duplicate_delivery_count == 0

    def test_repeated_symptoms_do_not_reinvestigate_within_cooldown(self):
        coord = IncidentCoordinator(investigation_cooldown_seconds=3600)
        first = coord.ingest(_event(), _trigger(_event()))
        follow = [coord.ingest(e, _trigger(e)) for e in (_event(), _event())]
        assert first.material_change is MaterialChange.NEW_INCIDENT
        assert all(d.material_change is MaterialChange.NONE for d in follow)
        assert all(d.investigation_request is None for d in follow)
        assert coord.stats["evidence_only_updates"] == 2
        assert coord.stats["investigations_requested"] == 1

    def test_severity_increase_bypasses_cooldown(self):
        coord = IncidentCoordinator(investigation_cooldown_seconds=3600)
        coord.ingest(_event(severity=Severity.MEDIUM), _trigger(_event()))
        worse = _event(severity=Severity.CRITICAL)
        decision = coord.ingest(worse, _trigger(worse))
        assert decision.material_change is MaterialChange.SEVERITY_INCREASE
        assert decision.investigation_request is not None
        assert coord.get_incident(decision.incident_id).severity is Severity.CRITICAL

    def test_new_signature_triggers_investigation(self):
        coord = IncidentCoordinator(investigation_cooldown_seconds=3600)
        coord.ingest(_event(), _trigger(_event()))
        other = _event(payload={"tool": "redis", "data_type": "timeout"})
        decision = coord.ingest(other, _trigger(other))
        assert decision.material_change is MaterialChange.NEW_SIGNATURE

    def test_cooldown_elapsed_reinvestigates(self):
        coord = IncidentCoordinator(investigation_cooldown_seconds=60)
        first = coord.ingest(_event(), _trigger(_event()))
        inc = coord.get_incident(first.incident_id)
        inc.last_investigation_at = datetime.now(UTC) - timedelta(seconds=120)
        decision = coord.ingest(_event(), _trigger(_event()))
        assert decision.material_change is MaterialChange.COOLDOWN_ELAPSED

    def test_two_workers_same_transition_one_request(self):
        coord = IncidentCoordinator()
        first = coord.ingest(_event(), _trigger(_event()))
        inc = coord.get_incident(first.incident_id)
        inc.attach_observation("obs-x")  # one evidence transition ...
        a = coord._request_investigation(inc, MaterialChange.MANUAL)
        b = coord._request_investigation(inc, MaterialChange.MANUAL)  # ... claimed by two workers
        assert a.id == b.id
        assert a.id != first.investigation_request.id
        assert len(coord.investigation_requests) == 2  # original + exactly one for the new version

    def test_two_independent_signals_are_not_merged(self):
        coord = IncidentCoordinator()
        a = _event(metadata={"environment": "production", "service": "checkout-api"})
        b = _event(metadata={"environment": "production", "service": "identity-adapter"})
        da = coord.ingest(a, _trigger(a))
        db = coord.ingest(b, _trigger(b))
        assert da.incident_id != db.incident_id

    def test_recurrence_after_resolved_is_new_episode(self):
        coord = IncidentCoordinator(recurrence_window_seconds=3600)
        first = coord.ingest(_event(), _trigger(_event()))
        _resolve(coord, first.incident_id)
        again = coord.ingest(_event(), _trigger(_event()))
        assert again.incident_id != first.incident_id
        assert again.material_change is MaterialChange.RECURRENCE_AFTER_RESOLVED
        assert coord.get_incident(again.incident_id).attributes["related_incident"] == first.incident_id

    def test_resolved_requires_verification_step(self):
        coord = IncidentCoordinator()
        d = coord.ingest(_event(), _trigger(_event()))
        coord.transition(d.incident_id, IncidentState.TRIAGED, "op", "triaged")
        with pytest.raises(InvalidTransition):
            coord.transition(d.incident_id, IncidentState.RESOLVED, "op", "looks fine")

    def test_late_evidence_corrects_timeline_instead_of_reopening(self):
        coord = IncidentCoordinator(recurrence_window_seconds=3600)
        first = coord.ingest(_event(), _trigger(_event()))
        _resolve(coord, first.incident_id)
        old = _event(timestamp=datetime.now(UTC) - timedelta(days=3))
        decision = coord.ingest(old, _trigger(old))
        assert decision.late_evidence and decision.incident_id == first.incident_id
        assert decision.investigation_request is None
        assert coord.get_incident(first.incident_id).late_evidence_count == 1
        assert coord.stats["total_incidents"] == 1

    def test_replay_tagged_events_are_rejected_in_live_mode(self):
        coord = IncidentCoordinator()
        ev = _event(metadata={"environment": "production", "service": "checkout-api", "replay": True})
        decision = coord.ingest(ev, _trigger(ev))
        assert not decision.accepted and decision.replay
        assert coord.stats["total_incidents"] == 0 and coord.stats["replay_rejected"] == 1
        # the live copy of the same identity is NOT suppressed by the rejected replay copy
        live = ev.model_copy(update={"metadata": {"environment": "production", "service": "checkout-api"}})
        assert coord.ingest(live, _trigger(live)).incident_id is not None

    def test_replay_mode_isolates_replay_traffic(self):
        coord = IncidentCoordinator(processing_mode="replay")
        ev = _event(metadata={"environment": "production", "service": "checkout-api", "replay": True})
        decision = coord.ingest(ev, _trigger(ev))
        assert decision.accepted and decision.replay and decision.incident_id is None
        assert coord.ingest(ev, _trigger(ev)).duplicate
        assert not coord.ingest(_event(), _trigger(_event())).accepted  # live traffic refused
        assert coord.stats["total_incidents"] == 0

    def test_register_then_correlate_lets_callers_defer_stateful_work(self):
        coord = IncidentCoordinator()
        ev = _event()
        reg = coord.register(ev)
        assert reg.accepted
        assert coord.register(ev).duplicate  # before any indexing happened
        decision = coord.correlate(ev, _trigger(ev), reg)
        assert decision.incident_id is not None
        with pytest.raises(ValueError):
            coord.correlate(_event(), None)  # never registered

    def test_changed_severity_with_same_payload_is_a_conflict_not_a_duplicate(self):
        coord = IncidentCoordinator()
        ev = _event(severity=Severity.HIGH)
        coord.ingest(ev, _trigger(ev))
        changed = ev.model_copy(update={"severity": Severity.CRITICAL})
        decision = coord.ingest(changed, _trigger(changed))
        assert decision.conflicting and not decision.duplicate

    def test_delivery_metadata_does_not_make_a_redelivery_a_conflict(self):
        coord = IncidentCoordinator()
        ev = _event(metadata={"environment": "production", "service": "checkout-api", "offset": 1})
        coord.ingest(ev, _trigger(ev))
        again = ev.model_copy(update={"metadata": {**ev.metadata, "offset": 2, "delivery_attempt": 2}})
        assert coord.ingest(again, _trigger(again)).duplicate

    def test_no_trigger_records_observation_only(self):
        coord = IncidentCoordinator()
        decision = coord.ingest(_event(), None)
        assert decision.accepted and decision.incident_id is None
        assert coord.stats["total_incidents"] == 0

    def test_external_mapping_is_durable(self):
        coord = IncidentCoordinator()
        d = coord.ingest(_event(), _trigger(_event()))
        coord.link_external(d.incident_id, "pagerduty", "PD-123")
        assert coord.get_incident(d.incident_id).external_refs["pagerduty"] == "PD-123"

    def test_dedup_retention_is_bounded(self):
        coord = IncidentCoordinator(dedup_retention=5)
        for _ in range(10):
            coord.ingest(_event(), None)
        assert coord.stats["dedup_cache_size"] == 5


# --------------------------------------------------------------------------- assignment contract

ASSIGNMENT_YAML = """
task: investigate_incident
scope:
  incident_id: INC-4821
  service_ids: [checkout-api, identity-adapter]
  environment: production
  evidence_snapshot: incident-evidence-v17
permissions:
  capabilities:
    - query_scoped_logs
    - read_service_health
    - retrieve_approved_runbooks
    - inspect_deployment_metadata
  production_mutations: false
limits:
  max_model_calls: 2
  max_tool_calls: 3
  max_runtime_seconds: 120
  budget_profile: standard-investigation
output:
  required:
    - observed_facts_with_references
    - hypotheses
    - contradictory_or_missing_evidence
    - proposed_next_step
on_limit_or_uncertainty: return_partial_findings_and_escalate
"""


class TestAssignment:
    def test_loads_from_yaml_and_roundtrips(self):
        a = InvestigationAssignment.from_yaml(ASSIGNMENT_YAML)
        assert a.scope.incident_id == "INC-4821"
        assert a.limits.max_model_calls == 2
        again = InvestigationAssignment.from_yaml(a.to_yaml())
        assert again == a

    def test_malicious_instruction_in_telemetry_cannot_expand_permissions(self):
        run = InvestigationAssignment.from_yaml(ASSIGNMENT_YAML).start()
        run.use_capability("query_scoped_logs", query="errors last 5m")
        with pytest.raises(PermissionDenied):
            run.use_capability("restart_component", target="checkout-api")
        assert "restart_component" in run.denied

    def test_mutating_capability_requires_flag_even_if_listed(self):
        a = InvestigationAssignment.from_yaml(ASSIGNMENT_YAML)
        a.permissions.capabilities.append("restart_component")
        run = a.start()
        with pytest.raises(PermissionDenied):
            run.use_capability("restart_component")
        a.permissions.production_mutations = True
        a.start().use_capability("restart_component")

    def test_tool_and_model_limits_enforced(self):
        run = InvestigationAssignment.from_yaml(ASSIGNMENT_YAML).start()
        for _ in range(3):
            run.use_capability("read_service_health")
        with pytest.raises(LimitExceeded):
            run.use_capability("read_service_health")
        run.record_model_call()
        run.record_model_call()
        with pytest.raises(LimitExceeded):
            run.record_model_call()

    def test_incomplete_findings_become_partial_and_escalate(self):
        a = InvestigationAssignment.from_yaml(ASSIGNMENT_YAML)
        run: BoundedRun = a.start()
        findings = run.finish(InvestigationFindings(hypotheses=["deploy v42 shrank pool size"]))
        assert findings.partial and findings.escalate
        assert "observed_facts_with_references" in findings.missing_required(a)
        assert findings.evidence_snapshot == "incident-evidence-v17"

    def test_complete_findings_are_not_partial(self):
        a = InvestigationAssignment.from_yaml(ASSIGNMENT_YAML)
        findings = a.start().finish(
            InvestigationFindings(
                observed_facts_with_references=[
                    Fact(statement="pool timeouts rose after deploy v42", evidence_refs=["trace:abc", "metric:pool.wait"])
                ],
                hypotheses=["v42 reduced pool size"],
                contradictory_or_missing_evidence=["no config diff retrieved yet"],
                proposed_next_step="inspect_deployment_metadata for v42",
            )
        )
        assert not findings.partial

    def test_unknown_capability_is_denied_even_if_listed(self):
        a = InvestigationAssignment.from_yaml(ASSIGNMENT_YAML)
        a.permissions.capabilities.append("arbitrary_shell")
        with pytest.raises(PermissionDenied):
            a.start().use_capability("arbitrary_shell", cmd="rm -rf /")

    def test_assignment_is_snapshotted_at_start(self):
        a = InvestigationAssignment.from_yaml(ASSIGNMENT_YAML)
        run = a.start()
        a.permissions.capabilities.append("read_metrics")
        a.permissions.production_mutations = True
        with pytest.raises(PermissionDenied):
            run.use_capability("read_metrics")

    def test_scope_is_enforced_on_parameters(self):
        run = InvestigationAssignment.from_yaml(ASSIGNMENT_YAML).start()
        run.use_capability("query_scoped_logs", service="checkout-api", environment="production")
        with pytest.raises(PermissionDenied):
            run.use_capability("query_scoped_logs", service="payments-api")
        with pytest.raises(PermissionDenied):
            run.use_capability("read_service_health", environment="staging")
        with pytest.raises(PermissionDenied):
            run.use_capability("read_incident_history", incident_id="INC-other")

    def test_fact_requires_evidence_reference(self):
        with pytest.raises(ValidationError):
            Fact(statement="unsupported claim", evidence_refs=[])


# --------------------------------------------------------------------------- action ledger


class TestActionLedger:
    def test_same_plan_step_yields_same_operation_id(self):
        ledger = ActionLedger()
        a = ledger.prepare("INC-1", "restart", "checkout-api", {"replicas": 2})
        b = ledger.prepare("INC-1", "restart", "checkout-api", {"replicas": 2})
        assert a.operation_id == b.operation_id
        assert len(ledger.records) == 1

    def test_changed_parameters_are_a_different_operation(self):
        ledger = ActionLedger()
        a = ledger.prepare("INC-1", "restart", "checkout-api", {"replicas": 2})
        b = ledger.prepare("INC-1", "restart", "checkout-api", {"replicas": 3})
        assert a.operation_id != b.operation_id

    def test_cannot_dispatch_without_authorization(self):
        ledger = ActionLedger()
        rec = ledger.prepare("INC-1", "restart", "checkout-api")
        with pytest.raises(InvalidActionTransition):
            ledger.dispatch(rec.operation_id)

    def test_lost_response_reconciles_instead_of_blind_retry(self):
        ledger = ActionLedger()
        rec = ledger.prepare("INC-1", "restart", "checkout-api")
        ledger.authorize(rec.operation_id, actor="oncall@corp", incident_version=5)
        ledger.dispatch(rec.operation_id)
        ledger.mark_outcome_unknown(rec.operation_id)
        assert rec.state is ActionState.RECONCILING
        with pytest.raises(InvalidActionTransition):
            ledger.dispatch(rec.operation_id)  # blind retry from an uncertain state is refused
        ledger.reconcile(rec.operation_id, target_state_observed=True)
        assert rec.state is ActionState.APPLIED  # applied is not verified
        assert rec.dispatch_attempts == 1  # the action was not repeated
        ledger.verify(rec.operation_id, healthy=True, evidence={**EVIDENCE, "details": {"error_rate": 0.001}})
        assert rec.state is ActionState.VERIFIED

    def test_reconcile_with_no_effect_retries_same_operation_id(self):
        ledger = ActionLedger()
        rec = ledger.prepare("INC-1", "restart", "checkout-api")
        ledger.authorize(rec.operation_id, actor="oncall@corp", incident_version=5)
        ledger.dispatch(rec.operation_id)
        ledger.mark_outcome_unknown(rec.operation_id)
        ledger.reconcile(rec.operation_id, target_state_observed=False)
        assert rec.state is ActionState.RETRY_READY
        assert rec.dispatch_attempts == 1  # reconciliation itself dispatches nothing
        ledger.dispatch(rec.operation_id)  # one explicit retry, same operation id
        assert rec.state is ActionState.DISPATCHED and rec.dispatch_attempts == 2
        assert len(ledger.records) == 1
        # a second lost response exhausts the retry budget
        ledger.mark_outcome_unknown(rec.operation_id)
        ledger.reconcile(rec.operation_id, target_state_observed=False)
        from stag.techops import RetryBudgetExceeded

        with pytest.raises(RetryBudgetExceeded):
            ledger.dispatch(rec.operation_id)

    def test_terms_are_part_of_the_operation_identity(self):
        ledger = ActionLedger()
        a = ledger.prepare("INC-1", "restart", "svc", {}, policy_version="RB-1@v1", preconditions=["x"])
        b = ledger.prepare("INC-1", "restart", "svc", {}, policy_version="RB-1@v2", preconditions=["x"])
        c = ledger.prepare("INC-1", "restart", "svc", {}, policy_version="RB-1@v1", preconditions=["x", "y"])
        assert len({a.operation_id, b.operation_id, c.operation_id}) == 3

    def test_verify_validates_before_writing_and_protects_reserved_fields(self):
        ledger = ActionLedger()
        rec = ledger.prepare("INC-1", "restart", "svc")
        ledger.authorize(rec.operation_id, "oncall", 1)
        ledger.dispatch(rec.operation_id)
        ledger.verify(rec.operation_id, healthy=False, evidence={**EVIDENCE, "details": {"healthy": True, "error_rate": 0.2}})
        assert rec.state is ActionState.FAILED and rec.verification["healthy"] is False
        before = dict(rec.verification)
        with pytest.raises(InvalidActionTransition):
            ledger.verify(rec.operation_id, healthy=True, evidence=EVIDENCE)
        assert rec.verification == before  # rejected verification changed nothing

    def test_unknown_verdict_is_recorded_without_changing_state(self):
        ledger = ActionLedger()
        rec = ledger.prepare("INC-1", "restart", "svc")
        ledger.authorize(rec.operation_id, "oncall", 1)
        ledger.dispatch(rec.operation_id)
        ledger.verify(rec.operation_id, healthy=None, evidence={**EVIDENCE, "details": {"reason": "collector silent"}})
        assert rec.state is ActionState.DISPATCHED
        assert len(rec.verification["unknown_verdicts"]) == 1

    def test_dispatch_revalidates_freshness_when_given_current_version(self):
        ledger = ActionLedger()
        rec = ledger.prepare("INC-1", "restart", "svc")
        ledger.authorize(rec.operation_id, "oncall", incident_version=4)
        with pytest.raises(InvalidActionTransition):
            ledger.dispatch(rec.operation_id, current_incident_version=6)
        assert rec.state is ActionState.STALE
        ledger.authorize(rec.operation_id, "oncall", incident_version=6)
        with pytest.raises(InvalidActionTransition):
            ledger.dispatch(rec.operation_id, current_incident_version=5)  # approval ahead of incident

    def test_stale_approval_requires_reauthorization(self):
        ledger = ActionLedger(max_staleness_versions=0)
        rec = ledger.prepare("INC-1", "restart", "checkout-api")
        ledger.authorize(rec.operation_id, actor="oncall@corp", incident_version=5)
        ledger.revalidate(rec.operation_id, current_incident_version=7)
        assert rec.state is ActionState.STALE
        with pytest.raises(InvalidActionTransition):
            ledger.dispatch(rec.operation_id)
        ledger.authorize(rec.operation_id, actor="oncall@corp", incident_version=7)
        ledger.dispatch(rec.operation_id)

    def test_successful_command_is_not_verified_recovery(self):
        ledger = ActionLedger()
        rec = ledger.prepare("INC-1", "restart", "checkout-api")
        ledger.authorize(rec.operation_id, actor="oncall@corp", incident_version=1)
        ledger.dispatch(rec.operation_id)
        assert rec.state is ActionState.DISPATCHED
        ledger.verify(rec.operation_id, healthy=False, evidence={**EVIDENCE, "details": {"error_rate": 0.12}})
        assert rec.state is ActionState.FAILED


# --------------------------------------------------------------------------- budget


class TestBudget:
    def test_reserve_settle_release(self):
        b = BudgetLedger(allocation=10.0)
        b.reserve("inv-1", 4.0)
        assert b.available == 6.0
        b.settle("inv-1", 2.5)
        assert b.spent == 2.5 and b.available == 7.5
        b.reserve("inv-2", 3.0)
        b.release("inv-2")
        assert b.available == 7.5

    def test_exhaustion_blocks_admission(self):
        b = BudgetLedger(allocation=5.0)
        b.reserve("a", 4.0)
        with pytest.raises(BudgetExhausted):
            b.reserve("b", 2.0)

    def test_reservation_is_idempotent(self):
        b = BudgetLedger(allocation=5.0)
        b.reserve("a", 4.0)
        b.reserve("a", 4.0)
        assert b.reserved == 4.0

    def test_negative_nan_and_infinite_amounts_are_rejected(self):
        b = BudgetLedger(allocation=10.0)
        b.reserve("a", 1.0)
        for bad in (-100, float("nan"), float("inf"), "ten"):
            with pytest.raises(ValueError):
                b.settle("a", bad)
            with pytest.raises(ValueError):
                b.reserve("b", bad)
        assert b.available == 9.0 and b.spent == 0.0
        with pytest.raises(ValueError):
            BudgetLedger(allocation=-1)

    def test_settled_reservation_ref_cannot_be_reused(self):
        b = BudgetLedger(allocation=10.0)
        b.reserve("attempt-1", 2.0)
        b.settle("attempt-1", 1.0)
        with pytest.raises(ValueError):
            b.reserve("attempt-1", 2.0)
        assert b.settle("attempt-1", 5.0).actual == 1.0  # idempotent: second settle is a no-op
        assert b.spent == 1.0

    def test_zero_allocation_admits_nothing(self):
        b = BudgetLedger(allocation=0)
        with pytest.raises(BudgetExhausted):
            b.reserve("x", 0.01)

    def test_allocation_change_requires_authorizer(self):
        b = BudgetLedger(allocation=5.0)
        with pytest.raises(ValueError):
            b.set_allocation(100.0, authorized_by="")
        b.set_allocation(100.0, authorized_by="finops@corp")
        assert b.allocation == 100.0
