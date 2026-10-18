"""Crew runner: executes a crew's workflow end-to-end against the TechOps backbone.

The runner is deterministic by construction. Every step maps onto a backbone operation:

    coordinate      → IncidentCoordinator.ingest (dedup, correlate, material change)
    transition      → coordinator.transition (explicit, recorded)
    investigate     → InvestigationAssignment + BoundedRun (limits enforced) → findings
    propose_action  → ActionLedger.prepare (deterministic operation id)
    authorize       → ActionLedger.authorize by a human or by a severity-bounded policy; else HELD
    dispatch        → ActionLedger.revalidate (staleness) + dispatch
    verify          → ActionLedger.verify from independent evidence; RESOLVED or INVESTIGATING again
    notify / record → provenance-tagged communications and *draft* artifacts

Role executors supply the content (findings text, messages, artifact drafts). The default
``DeterministicExecutor`` needs no LLM, so every crew can be checked end-to-end offline; plug an
LLM-backed executor in for production and the control flow stays the same.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from stag.crews.schema import (
    POLICY_SEVERITY_CEILING,
    AuthorizePolicy,
    Crew,
    Role,
    Scenario,
    Step,
    StepAction,
    Verdict,
)
from stag.models.schemas import StreamEvent, TriggerResult
from stag.techops import (
    ActionLedger,
    ActionRecord,
    ActionState,
    BudgetExhausted,
    BudgetLedger,
    Incident,
    IncidentCoordinator,
    IncidentState,
    InvalidActionTransition,
    InvalidTransition,
    InvestigationAssignment,
    InvestigationFindings,
    LimitExceeded,
    PermissionDenied,
    SignalKey,
)
from stag.techops.assignment import (
    AssignmentLimits,
    AssignmentPermissions,
    AssignmentScope,
    Fact,
)
from stag.triggers import RuleRegistry, TriggerEngine

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
MAX_REINVESTIGATION_LOOPS = 2
MAX_STEP_EXECUTIONS_FACTOR = 3  # a workflow may execute at most 3x its step count (bounded loops)


def _now() -> datetime:
    return datetime.now(UTC)


def _lookup(event: StreamEvent, dotted: str, default: str = "unknown") -> str:
    """Resolve ``metadata.service`` / ``payload.region`` / ``source`` against an event."""
    head, _, rest = dotted.partition(".")
    container: Any
    if head == "metadata":
        container = event.metadata
    elif head == "payload":
        container = event.payload
    elif head == "source":
        return event.source
    else:
        return default
    value = container.get(rest) if isinstance(container, dict) else None
    return str(value) if value not in (None, "") else default


# --------------------------------------------------------------------------- executors


class RoleExecutor(Protocol):
    """Content provider for the roles. The runner owns control flow and enforcement."""

    def investigate(
        self, crew: Crew, role: Role, incident: Incident, run: Any, evidence: dict[str, Any]
    ) -> InvestigationFindings: ...

    def verify(
        self, crew: Crew, incident: Incident, action: ActionRecord | None, hint: str
    ) -> tuple[bool | None, dict[str, Any]]: ...

    def notify(
        self, crew: Crew, step: Step, incident: Incident, context: dict[str, Any]
    ) -> dict[str, Any]: ...

    def record(
        self, crew: Crew, step: Step, incident: Incident, context: dict[str, Any]
    ) -> dict[str, Any]: ...


class DeterministicExecutor:
    """Template-based executor: produces structured, evidence-referenced output without an LLM."""

    def investigate(self, crew, role, incident, run, evidence):
        caps = run.assignment.permissions.capabilities
        used = []
        for cap in caps[: min(3, run.assignment.limits.max_tool_calls)]:
            try:
                run.use_capability(cap, incident_id=incident.id)
                used.append(cap)
            except (PermissionDenied, LimitExceeded):
                break
        try:
            run.record_model_call()
        except LimitExceeded:
            pass

        attrs = incident.attributes
        refs = [f"incident:{incident.id}@v{incident.version}"] + [
            f"obs:{o}" for o in incident.example_observation_ids[:2]
        ]
        facts = [
            Fact(
                statement=(
                    f"{incident.observation_count} distinct observation(s) of "
                    f"'{attrs.get('symptom_class')}' on {attrs.get('service')} "
                    f"({attrs.get('environment')}); {incident.duplicate_delivery_count} duplicate "
                    f"deliveries counted separately"
                ),
                evidence_refs=refs,
            )
        ]
        if incident.signatures:
            facts.append(
                Fact(
                    statement=f"failure signatures observed: {', '.join(incident.signatures[:5])}",
                    evidence_refs=refs[:1],
                )
            )
        missing = []
        if "inspect_deployment_metadata" not in used:
            missing.append("deployment metadata not retrieved (capability not used or not granted)")
        if "read_service_health" not in used:
            missing.append("current service health not read")
        next_step = evidence.get("next_plan_step")
        return InvestigationFindings(
            observed_facts_with_references=facts,
            hypotheses=[
                f"{attrs.get('symptom_class')} on {attrs.get('service')} is a continuing condition "
                f"(severity {incident.severity.value}); a recent change is a candidate contributor, not a confirmed cause"
            ],
            contradictory_or_missing_evidence=missing or ["none identified within the bounded evidence package"],
            proposed_next_step=(
                f"propose bounded step '{next_step}' for review" if next_step else "continue monitoring; no action proposed"
            ),
        )

    def verify(self, crew, incident, action, hint):
        """Fixture verdicts only. Anything that is not an exact Verdict is UNKNOWN (fail closed)."""
        try:
            verdict = Verdict(hint)
        except ValueError:
            verdict = Verdict.UNKNOWN
        healthy = {Verdict.HEALTHY: True, Verdict.NOT_RECOVERED: False, Verdict.UNKNOWN: None}[verdict]
        evidence = {
            "simulated": True,
            "source": "scenario fixture (not a measured observation)",
            "window": "observation window per crew policy",
            "signals": ["service_health", "impact_metric", "telemetry_freshness"],
            "fixture_verdict": verdict.value,
        }
        return healthy, evidence

    def notify(self, crew, step, incident, context):
        return {
            "channel": step.channel,
            "template": step.template,
            "incident_id": incident.id,
            "operation_ids": context.get("operation_ids", []),
            "message": (
                f"[{crew.display_name}] {incident.attributes.get('service')}: "
                f"{incident.attributes.get('symptom_class')} — state {incident.state.value}, "
                f"severity {incident.severity.value}, {incident.observation_count} observation(s)"
            ),
        }

    def record(self, crew, step, incident, context):
        return {
            "artifact": step.artifact,
            "status": "draft — pending review",
            "incident_id": incident.id,
            "timeline": [
                {"at": t.at.isoformat(), "from": t.from_state.value, "to": t.to_state.value, "actor": t.actor, "reason": t.reason}
                for t in incident.history
            ],
            "actions": context.get("operation_ids", []),
            "findings": context.get("findings"),
        }


# --------------------------------------------------------------------------- run records


class StepRecord(BaseModel):
    step: str
    action: StepAction
    role: str | None = None
    status: str  # done | held | skipped | failed | awaiting
    detail: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=_now)


class IncidentRun(BaseModel):
    incident_id: str
    signal_key: str
    severity: str
    final_state: str
    steps: list[StepRecord] = Field(default_factory=list)
    operation_ids: list[str] = Field(default_factory=list)
    halted_at: str | None = None
    halt_reason: str | None = None


class CrewRunReport(BaseModel):
    """Result of a crew run. ``mode`` is always ``simulation`` for this runner: verdicts come from
    scenario fixtures, actions change ledger records only, and approvals are simulated flags.
    """

    run_id: str = Field(default_factory=lambda: f"RUN-{uuid4().hex[:8]}")
    mode: str = "simulation"
    crew: str
    scenario: str
    started_at: datetime = Field(default_factory=_now)
    events_ingested: int = 0
    triggers_fired: int = 0
    duplicate_deliveries: int = 0
    conflicting_deliveries: int = 0
    replay_observations: int = 0
    late_evidence: int = 0
    evidence_only_updates: int = 0
    investigations_requested: int = 0
    incidents: list[IncidentRun] = Field(default_factory=list)
    notifications: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    ledger: dict[str, Any] = Field(default_factory=dict)

    @property
    def model_calls_avoided(self) -> int:
        """Investigations *not started* because of dedup or evidence-only updates.

        This counts suppressed investigation triggers, not measured provider spend; see
        docs/review/FINDINGS.md R31 for the measured-cost plan.
        """
        return self.duplicate_deliveries + self.evidence_only_updates

    @property
    def failed_steps(self) -> int:
        return sum(1 for inc in self.incidents for s in inc.steps if s.status == "failed")

    @property
    def resolved(self) -> int:
        return sum(1 for i in self.incidents if i.final_state == IncidentState.RESOLVED.value)

    @property
    def held(self) -> int:
        return sum(1 for i in self.incidents if i.halted_at is not None)

    def to_markdown(self) -> str:
        lines = [
            f"# Crew run {self.run_id} [{self.mode.upper()}]: {self.crew} / {self.scenario}",
            "",
            "_Simulation: fixture verdicts, ledger-only actions, simulated approvals. Not a live operation._",
            "",
            f"- events ingested: {self.events_ingested}; triggers fired: {self.triggers_fired}",
            f"- duplicate deliveries: {self.duplicate_deliveries}; conflicting: {self.conflicting_deliveries}; "
            f"replay: {self.replay_observations}; late evidence: {self.late_evidence}; "
            f"evidence-only updates: {self.evidence_only_updates}",
            f"- investigations requested: {self.investigations_requested}; "
            f"**investigations suppressed by dedup/evidence-only updates: {self.model_calls_avoided}**",
            f"- incidents: {len(self.incidents)}; resolved: {self.resolved}; halted/awaiting: {self.held}",
            f"- budget: {self.budget}",
            "",
        ]
        for inc in self.incidents:
            lines.append(f"## {inc.incident_id} — {inc.signal_key} (severity {inc.severity}) → {inc.final_state}")
            for s in inc.steps:
                extra = ""
                if s.detail.get("operation_id"):
                    extra = f" · op {s.detail['operation_id']}"
                if s.detail.get("reason"):
                    extra += f" · {s.detail['reason']}"
                lines.append(f"- `{s.step}` ({s.action.value}, {s.role or 'system'}): **{s.status}**{extra}")
            if inc.halted_at:
                lines.append(f"- ⏸ halted at `{inc.halted_at}`: {inc.halt_reason}")
            lines.append("")
        if self.notifications:
            lines.append(f"Notifications: {len(self.notifications)} · Artifacts (drafts): {len(self.artifacts)}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- runner


class CrewRunner:
    def __init__(
        self,
        crew: Crew,
        executor: RoleExecutor | None = None,
        auto_approve: bool = False,
        approver: str = "human:on-call",
    ) -> None:
        self.crew = crew
        self.executor = executor or DeterministicExecutor()
        self.auto_approve = auto_approve
        self.approver = approver

        registry = RuleRegistry.from_specs([t.model_dump() for t in crew.triggers]) if crew.triggers else RuleRegistry.default()
        self.engine = TriggerEngine(registry)
        self.coordinator = IncidentCoordinator(signal_key_fn=self._signal_key, **crew.coordinator)
        self.ledger = ActionLedger()
        self.budget = BudgetLedger(crew.budget_allocation, name=crew.name)
        self._first_event: dict[str, StreamEvent] = {}
        self._verification_hints: dict[str, str] = {}
        self.notifications: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []

    # ----------------------------------------------------------------- coordinate

    def _signal_key(self, event: StreamEvent, trigger: TriggerResult) -> SignalKey:
        spec = self.crew.signal_key
        return SignalKey(
            tenant=self.coordinator.tenant,
            environment=_lookup(event, spec.environment),
            service=_lookup(event, spec.service, default=event.source),
            failure_domain=event.event_type.value if spec.failure_domain_from_event_type else "any",
            symptom_class=trigger.rule_name if spec.symptom_from_rule else "signal",
        )

    def ingest(self, event: StreamEvent):
        """Identity first: duplicates and conflicts never reach stateful trigger rules."""
        registration = self.coordinator.register(event)
        if not registration.accepted:
            incident = (
                self.coordinator.get_incident(registration.prior_incident_id)
                if registration.prior_incident_id and self.coordinator.has_incident(registration.prior_incident_id)
                else None
            )
            from stag.techops import CoordinatorDecision

            return None, CoordinatorDecision(
                accepted=False, duplicate=registration.duplicate, conflicting=registration.conflicting,
                replay=registration.replay, incident_id=registration.prior_incident_id,
                incident_state=incident.state if incident else None,
                incident_version=incident.version if incident else None, reason=registration.reason,
            )
        trigger = self.engine.evaluate(event)
        decision = self.coordinator.correlate(event, trigger, registration)
        if decision.incident_id and decision.incident_id not in self._first_event:
            self._first_event[decision.incident_id] = event
        return trigger, decision

    # ----------------------------------------------------------------- scenario

    @staticmethod
    def _expand(scenario: Scenario) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        for spec in scenario.events:
            for _ in range(max(1, spec.repeat)):
                kwargs: dict[str, Any] = dict(
                    source=spec.source,
                    event_type=spec.event_type,
                    severity=spec.severity,
                    payload=dict(spec.payload),
                    metadata=dict(spec.metadata),
                )
                if spec.id:
                    kwargs["id"] = spec.id  # same identity on every repeat → redelivery
                events.append(StreamEvent(**kwargs))
        return events

    def run_scenario(self, scenario: Scenario) -> CrewRunReport:
        report = CrewRunReport(crew=self.crew.name, scenario=scenario.name)
        self._verification_hints = dict(scenario.verification)
        for event in self._expand(scenario):
            trigger, decision = self.ingest(event)
            report.events_ingested += 1
            if trigger is not None:
                report.triggers_fired += 1
            if decision.duplicate:
                report.duplicate_deliveries += 1
            elif decision.conflicting:
                report.conflicting_deliveries += 1
            elif decision.replay:
                report.replay_observations += 1
            elif decision.late_evidence:
                report.late_evidence += 1
            elif decision.incident_id and decision.investigation_request is None:
                report.evidence_only_updates += 1
            if decision.investigation_request is not None:
                report.investigations_requested += 1

        for incident in self.coordinator.open_incidents:
            if incident.last_investigation_version is None:
                continue
            report.incidents.append(self.run_workflow(incident))

        report.budget = self.budget.stats
        report.ledger = self.ledger.stats
        report.notifications = list(self.notifications)
        report.artifacts = list(self.artifacts)
        return report

    # ----------------------------------------------------------------- workflow

    def run_workflow(self, incident: Incident) -> IncidentRun:
        run = IncidentRun(
            incident_id=incident.id,
            signal_key=incident.signal_key,
            severity=incident.severity.value,
            final_state=incident.state.value,
        )
        steps = self.crew.workflow
        index = 1  # step 0 is coordinate (already done at ingest)
        context: dict[str, Any] = {"operation_ids": [], "findings": None, "loops": 0, "verifications": 0}
        executed = 0
        budget_steps = MAX_STEP_EXECUTIONS_FACTOR * len(steps)
        while index < len(steps):
            step = steps[index]
            executed += 1
            if executed > budget_steps:
                run.halted_at, run.halt_reason = step.step, f"step budget exceeded ({budget_steps}); possible loop"
                run.steps.append(StepRecord(step=step.step, action=step.action, role=step.role, status="failed", detail={"reason": run.halt_reason}))
                break
            try:
                record, jump = self._execute(step, incident, context)
            except Exception as e:  # a bug in a step must surface, not be swallowed
                record = StepRecord(step=step.step, action=step.action, role=step.role, status="failed", detail={"error": str(e)})
                jump = None
                run.steps.append(record)
                run.halted_at, run.halt_reason = step.step, f"error: {e}"
                break
            run.steps.append(record)
            if record.status in ("held", "awaiting"):
                run.halted_at, run.halt_reason = step.step, record.detail.get("reason", record.status)
                break
            if jump is not None:
                index = jump
                continue
            index += 1
        run.operation_ids = list(context["operation_ids"])
        run.final_state = incident.state.value
        return run

    def _ensure(
        self,
        incident: Incident,
        target: IncidentState,
        actor: str,
        reason: str,
        via: list[IncidentState] | tuple[IncidentState, ...] = (),
    ) -> list[str]:
        """Bring the incident to ``target`` through permitted intermediate states, recording each."""
        applied: list[str] = []
        if incident.state is target:
            return applied
        for state in [*via, target]:
            if incident.state is state:
                continue
            if incident.can_transition(state):
                self.coordinator.transition(incident.id, state, actor=actor, reason=reason)
                applied.append(state.value)
        if incident.state is not target:
            raise InvalidTransition(f"{incident.id}: cannot reach {target.value} from {incident.state.value}")
        return applied

    def _execute(self, step: Step, incident: Incident, context: dict[str, Any]) -> tuple[StepRecord, int | None]:
        actor = f"role:{step.role}" if step.role else "crew"
        detail: dict[str, Any] = {}

        if step.action is StepAction.COORDINATE:
            return StepRecord(step=step.step, action=step.action, role=step.role, status="skipped", detail={"reason": "coordinate runs at ingest"}), None

        if step.action is StepAction.TRANSITION:
            if incident.state is step.to:
                return StepRecord(step=step.step, action=step.action, role=step.role, status="skipped", detail={"reason": f"already {step.to.value}"}), None
            self.coordinator.transition(incident.id, step.to, actor=actor, reason=step.description or step.step)
            return StepRecord(step=step.step, action=step.action, role=step.role, status="done", detail={"to": step.to.value}), None

        if step.action is StepAction.INVESTIGATE:
            role = self.crew.role(step.role) if step.role else None
            spec = step.assignment
            caps = (spec.capabilities if spec and spec.capabilities else (role.permissions.capabilities if role else []))
            assignment = InvestigationAssignment(
                scope=AssignmentScope(
                    incident_id=incident.id,
                    service_ids=[str(incident.attributes.get("service", "unknown"))],
                    environment=str(incident.attributes.get("environment", "unknown")),
                    evidence_snapshot=f"{incident.id}-evidence-v{incident.version}",
                ),
                permissions=AssignmentPermissions(capabilities=caps, production_mutations=bool(role and role.permissions.production_mutations)),
                limits=AssignmentLimits(
                    max_model_calls=spec.max_model_calls if spec else 4,
                    max_tool_calls=spec.max_tool_calls if spec else 8,
                    max_runtime_seconds=spec.max_runtime_seconds if spec else 120,
                    budget_profile=spec.budget_profile if spec else "standard-investigation",
                ),
            )
            detail["implicit_transitions"] = self._ensure(
                incident, IncidentState.INVESTIGATING, actor, step.step, via=[IncidentState.TRIAGED]
            )
            ref = f"{incident.id}:{step.step}:v{incident.version}"
            try:
                self.budget.reserve(ref, 1.0 * max(1, assignment.limits.max_model_calls))
            except BudgetExhausted as e:
                self._ensure(incident, IncidentState.HELD, actor, "budget exhausted")
                return StepRecord(step=step.step, action=step.action, role=step.role, status="held", detail={"reason": f"budget exhausted: {e}"}), None
            bounded = assignment.start()
            next_plan = next((s.plan_step for s in self.crew.workflow if s.action is StepAction.PROPOSE_ACTION), None)
            try:
                findings = self.executor.investigate(self.crew, role, incident, bounded, {"next_plan_step": next_plan})
                findings = bounded.finish(findings)
            except LimitExceeded as e:
                findings = InvestigationFindings(partial=True, escalate=True, contradictory_or_missing_evidence=[f"limit reached: {e}"])
            finally:
                # settle what was actually consumed, even when the executor failed
                self.budget.settle(ref, float(bounded.model_calls))
            context["findings"] = findings.model_dump(mode="json")
            detail.update(
                {
                    "evidence_snapshot": assignment.scope.evidence_snapshot,
                    "capabilities_used": [c["capability"] for c in bounded.capability_log],
                    "denied": bounded.denied,
                    "model_calls": bounded.model_calls,
                    "partial": findings.partial,
                    "facts": len(findings.observed_facts_with_references),
                    "proposed_next_step": findings.proposed_next_step,
                }
            )
            if findings.partial:
                # Partial or escalated findings never advance to proposal/dispatch on their own.
                self._ensure(incident, IncidentState.HELD, actor, "partial findings; human review required")
                detail["reason"] = (
                    "partial findings (missing: "
                    + ", ".join(findings.missing_required(assignment)) + "); escalated for human review"
                )
                return StepRecord(step=step.step, action=step.action, role=step.role, status="held", detail=detail), None
            return StepRecord(step=step.step, action=step.action, role=step.role, status="done", detail=detail), None

        if step.action is StepAction.PROPOSE_ACTION:
            first = self._first_event.get(incident.id)
            target = _lookup(first, step.target_from, default=str(incident.attributes.get("service", "unknown"))) if first else str(incident.attributes.get("service", "unknown"))
            record = self.ledger.prepare(
                incident.id, step.plan_step, target, dict(step.parameters),
                policy_version=step.policy_version, preconditions=list(step.preconditions),
            )
            if record.operation_id not in context["operation_ids"]:
                context["operation_ids"].append(record.operation_id)
            detail.update({"operation_id": record.operation_id, "target": target, "state": record.state.value})
            detail["implicit_transitions"] = self._ensure(incident, IncidentState.ACTION_PROPOSED, actor, step.step)
            return StepRecord(step=step.step, action=step.action, role=step.role, status="done", detail=detail), None

        if step.action is StepAction.AUTHORIZE:
            policy = step.policy or AuthorizePolicy()
            op_id = context["operation_ids"][-1] if context["operation_ids"] else None
            if op_id is None:
                return StepRecord(step=step.step, action=step.action, role=step.role, status="skipped", detail={"reason": "no proposed action"}), None
            record = self.ledger.get(op_id)
            if record.state is ActionState.STALE:
                pass  # re-authorization path below
            elif record.state is ActionState.RETRY_READY:
                return StepRecord(step=step.step, action=step.action, role=step.role, status="skipped", detail={"reason": "operation already authorized; retry pending", "operation_id": op_id}), None
            elif record.state is not ActionState.PREPARED:
                return StepRecord(step=step.step, action=step.action, role=step.role, status="skipped", detail={"reason": f"operation already {record.state.value}", "operation_id": op_id}), None
            severity_ok = (
                policy.mode == "policy"
                and policy.auto_if_severity_at_most in POLICY_SEVERITY_CEILING
                and SEVERITY_ORDER[incident.severity.value] <= SEVERITY_ORDER[policy.auto_if_severity_at_most]
            )
            if severity_ok:
                who = f"policy:{step.step}"
            elif self.auto_approve:
                who = self.approver
            else:
                self._ensure(incident, IncidentState.HELD, actor, "awaiting human authorization")
                return StepRecord(
                    step=step.step, action=step.action, role=step.role, status="held",
                    detail={"operation_id": op_id, "reason": "awaiting human authorization (simulation: re-run with auto_approve to model the approver)"},
                ), None
            # Record the lifecycle transition first so the approval is bound to the resulting version;
            # any later evidence or state change then makes the approval STALE at dispatch time.
            detail["implicit_transitions"] = self._ensure(incident, IncidentState.AUTHORIZED, who, step.step)
            self.ledger.authorize(op_id, actor=who, incident_version=incident.version, note=step.description or step.step)
            detail.update({"operation_id": op_id, "authorized_by": who, "incident_version": incident.version})
            return StepRecord(step=step.step, action=step.action, role=step.role, status="done", detail=detail), None

        if step.action is StepAction.DISPATCH:
            op_id = context["operation_ids"][-1] if context["operation_ids"] else None
            if op_id is None:
                return StepRecord(step=step.step, action=step.action, role=step.role, status="skipped", detail={"reason": "no action to dispatch"}), None
            record = self.ledger.get(op_id)
            if record.state not in (ActionState.AUTHORIZED, ActionState.RETRY_READY):
                return StepRecord(step=step.step, action=step.action, role=step.role, status="skipped", detail={"operation_id": op_id, "reason": f"operation is {record.state.value}"}), None
            try:
                # Freshness is decided by the ledger from the incident's current version, not by the caller.
                self.ledger.dispatch(op_id, actor=actor, current_incident_version=incident.version)
            except InvalidActionTransition as e:
                return StepRecord(step=step.step, action=step.action, role=step.role, status="held", detail={"operation_id": op_id, "reason": f"not dispatched: {e}"}), None
            detail.update({"operation_id": op_id, "attempts": record.dispatch_attempts, "simulated": True})
            detail["implicit_transitions"] = self._ensure(incident, IncidentState.EXECUTING, actor, step.step)
            return StepRecord(step=step.step, action=step.action, role=step.role, status="done", detail=detail), None

        if step.action is StepAction.VERIFY:
            context["verifications"] += 1
            op_id = context["operation_ids"][-1] if context["operation_ids"] else None
            record = self.ledger.get(op_id) if op_id else None
            hints = self._verification_hints
            hint = hints.get(
                f"{incident.signal_key}#{context['verifications']}",
                hints.get(incident.signal_key, hints.get("default", Verdict.UNKNOWN)),
            )
            healthy, evidence = self.executor.verify(self.crew, incident, record, hint)
            if healthy not in (True, False, None):
                healthy = None  # an executor returning anything else is treated as unknown
            verdict = {True: "healthy", False: "not_recovered", None: "unknown"}[healthy]
            detail.update({"operation_id": op_id, "verdict": verdict, "evidence": evidence})
            detail["implicit_transitions"] = self._ensure(incident, IncidentState.VERIFYING, actor, step.step)
            if record is not None and record.state in (ActionState.DISPATCHED, ActionState.APPLIED):
                self.ledger.verify(op_id, healthy=healthy, evidence=evidence, actor=actor)
            if healthy is None:
                return StepRecord(
                    step=step.step, action=step.action, role=step.role, status="awaiting",
                    detail={**detail, "reason": "no verification evidence (missing fixture or telemetry); recovery unknown — not resolving"},
                ), None
            if healthy:
                jump = self._index_of(step.on_recovered) if step.on_recovered else None
                return StepRecord(step=step.step, action=step.action, role=step.role, status="done", detail=detail), jump
            self.coordinator.transition(
                incident.id, IncidentState.INVESTIGATING, actor=actor, reason="verification failed: impact persists"
            )
            context["loops"] += 1
            if step.on_not_recovered and context["loops"] <= MAX_REINVESTIGATION_LOOPS:
                detail["reason"] = f"not recovered; continuing at '{step.on_not_recovered}' (attempt {context['loops'] + 1})"
                return StepRecord(step=step.step, action=step.action, role=step.role, status="done", detail=detail), self._index_of(step.on_not_recovered)
            return StepRecord(
                step=step.step, action=step.action, role=step.role, status="held",
                detail={**detail, "reason": "not recovered; escalate to human"},
            ), None

        if step.action is StepAction.NOTIFY:
            message = self.executor.notify(self.crew, step, incident, context)
            message.setdefault("incident_id", incident.id)
            self.notifications.append(message)
            return StepRecord(step=step.step, action=step.action, role=step.role, status="done", detail={"channel": step.channel}), None

        if step.action is StepAction.RECORD:
            artifact = self.executor.record(self.crew, step, incident, context)
            artifact.setdefault("incident_id", incident.id)
            self.artifacts.append(artifact)
            return StepRecord(step=step.step, action=step.action, role=step.role, status="done", detail={"artifact": step.artifact, "status": artifact.get("status")}), None

        return StepRecord(step=step.step, action=step.action, role=step.role, status="skipped", detail={"reason": "unknown action"}), None

    def _index_of(self, step_name: str) -> int:
        return next(i for i, s in enumerate(self.crew.workflow) if s.step == step_name)


def check_expectations(report: CrewRunReport, scenario: Scenario, auto_approve: bool) -> list[str]:
    """Compare a report with the scenario's ``expect`` block. Returns human-readable failures."""
    e = scenario.expect
    failures: list[str] = []

    def eq(key: str, actual: int) -> None:
        if key in e and actual != e[key]:
            failures.append(f"{key}: expected {e[key]}, got {actual}")

    def at_least(key: str, actual: int) -> None:
        if key in e and actual < e[key]:
            failures.append(f"{key}: expected >= {e[key]}, got {actual}")

    def at_most(key: str, actual: int) -> None:
        if key in e and actual > e[key]:
            failures.append(f"{key}: expected <= {e[key]}, got {actual}")

    eq("incidents", len(report.incidents))
    at_least("incidents_at_least", len(report.incidents))
    eq("duplicate_deliveries", report.duplicate_deliveries)
    eq("evidence_only_updates", report.evidence_only_updates)
    at_most("investigations_at_most", report.investigations_requested)
    at_least("investigations_at_least", report.investigations_requested)
    if auto_approve:
        at_least("operations_at_least", report.ledger.get("operations", 0))
        eq("resolved_with_auto_approve", report.resolved)
        at_least("resolved_with_auto_approve_at_least", report.resolved)
        policy_approvals = sum(
            1 for inc in report.incidents for s in inc.steps
            if s.action is StepAction.AUTHORIZE and str(s.detail.get("authorized_by", "")).startswith("policy:")
        )
        at_least("policy_approved_at_least", policy_approvals)
    else:
        eq("held_without_auto_approve", report.held)
        at_least("held_without_auto_approve_at_least", report.held)
        eq("resolved_without_auto_approve", report.resolved)
    return failures


def run_crew(crew: Crew, scenario: Scenario, auto_approve: bool = False, executor: RoleExecutor | None = None) -> CrewRunReport:
    runner = CrewRunner(crew, executor=executor, auto_approve=auto_approve)
    return runner.run_scenario(scenario)
