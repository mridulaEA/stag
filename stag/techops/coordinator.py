"""Incident coordinator: dedup, correlate, and decide whether an investigation is warranted.

The coordinator sits between the stream and every stateful effect (indexing, stateful trigger
rules, incident evidence, the LLM path). It exposes two phases so callers can apply them in the
right order:

    register(event)            ─► accepted | duplicate delivery | conflicting delivery
         │ (only accepted observations may be indexed and evaluated by stateful rules)
    correlate(event, trigger)  ─► attach to / open an incident episode; material change?
                                  replay events never create live incidents or requests
                                  late evidence for a resolved episode corrects its timeline

``ingest`` composes the two for callers that have no stateful work in between.

A thousand repeated occurrences can update an incident's count without creating a thousand
reasoning tasks. Worsening impact, a new failure signature, recurrence after recovery or an
elapsed cooldown justify a new investigation.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from stag.models.schemas import Severity, StreamEvent, TriggerResult
from stag.techops.identity import ObservationIdentity, SignalKey, envelope_hash
from stag.techops.incident import Incident, IncidentState, MaterialChange

SEVERITY_ORDER = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


def _now() -> datetime:
    return datetime.now(UTC)


class Observation(BaseModel):
    identity: ObservationIdentity
    payload_hash: str  # envelope hash (source, type, severity, payload, non-delivery metadata)
    received_at: datetime = Field(default_factory=_now)
    event_time: datetime | None = None
    incident_id: str | None = None
    replay: bool = False


class RegistrationResult(BaseModel):
    """Outcome of the identity check, before any stateful effect."""

    accepted: bool
    duplicate: bool = False
    conflicting: bool = False
    replay: bool = False
    observation_key: str
    prior_incident_id: str | None = None
    reason: str = ""


class InvestigationRequest(BaseModel):
    """A uniquely identified request for bounded investigation.

    The id is derived from (incident, evidence version), so two workers processing the same
    transition produce one durable request, not two. Creation is a *request*, not a completion.
    """

    id: str
    incident_id: str
    evidence_snapshot_version: int
    reason: MaterialChange
    created_at: datetime = Field(default_factory=_now)
    context: dict[str, Any] = Field(default_factory=dict)

    @staticmethod
    def make_id(incident_id: str, version: int) -> str:
        digest = hashlib.sha256(f"{incident_id}:{version}".encode()).hexdigest()[:12]
        return f"INV-{digest}"


class CoordinatorDecision(BaseModel):
    accepted: bool
    duplicate: bool = False
    conflicting: bool = False
    replay: bool = False
    late_evidence: bool = False
    incident_id: str | None = None
    incident_state: IncidentState | None = None
    incident_version: int | None = None
    material_change: MaterialChange = MaterialChange.NONE
    investigation_request: InvestigationRequest | None = None
    reason: str = ""


class IncidentCoordinator:
    """Deterministic incident coordination with controlled investigation cadence.

    This is the in-memory reference implementation. Observations, incidents and requests live in
    process-local structures; a transactional store with worker coordination is a documented gap
    (docs/review/FINDINGS.md R14).

    Parameters
    ----------
    tenant: Tenant scope enforced on every identity.
    investigation_cooldown_seconds: Minimum time between routine re-investigations of one incident.
        Severity increases and new signatures bypass the cooldown.
    recurrence_window_seconds: A signal *occurring* within this window after RESOLVED opens a new
        episode tagged RECURRENCE_AFTER_RESOLVED; later it is simply a new incident. Evidence that
        occurred *before* the resolution is late evidence for the resolved episode, never a new one.
    dedup_retention: Observation identities kept for duplicate detection. Size it to the supported
        redelivery / replay horizon.
    signal_key_fn: Derive a SignalKey from (event, trigger). Default: metadata.service / environment,
        event_type as failure domain, rule name as symptom class.
    """

    def __init__(
        self,
        tenant: str = "default",
        investigation_cooldown_seconds: int = 300,
        recurrence_window_seconds: int = 3600,
        dedup_retention: int = 50_000,
        signal_key_fn: Callable[[StreamEvent, TriggerResult], SignalKey] | None = None,
    ) -> None:
        if investigation_cooldown_seconds < 0 or recurrence_window_seconds < 0 or dedup_retention < 1:
            raise ValueError("cooldown and recurrence window must be >= 0 and dedup_retention >= 1")
        self.tenant = tenant
        self.cooldown = timedelta(seconds=investigation_cooldown_seconds)
        self.recurrence_window = timedelta(seconds=recurrence_window_seconds)
        self.dedup_retention = dedup_retention
        self._signal_key_fn = signal_key_fn or self.default_signal_key

        self._seen: OrderedDict[str, Observation] = OrderedDict()
        self._quarantine: list[Observation] = []
        self._incidents: dict[str, Incident] = {}
        self._open_by_signal: dict[str, str] = {}
        self._last_resolved: dict[str, tuple[str, datetime]] = {}
        self._requests: dict[str, InvestigationRequest] = {}
        self._stats = {
            "observations_accepted": 0,
            "duplicate_deliveries": 0,
            "conflicting_deliveries": 0,
            "replay_observations": 0,
            "late_evidence": 0,
            "incidents_created": 0,
            "investigations_requested": 0,
            "evidence_only_updates": 0,
        }

    # ------------------------------------------------------------------ identity helpers

    def default_signal_key(self, event: StreamEvent, trigger: TriggerResult) -> SignalKey:
        payload = event.payload or {}
        meta = event.metadata or {}
        return SignalKey(
            tenant=self.tenant,
            environment=str(meta.get("environment", payload.get("environment", "unknown"))),
            service=str(meta.get("service", payload.get("service", event.source))),
            failure_domain=event.event_type.value,
            symptom_class=trigger.rule_name,
        )

    @staticmethod
    def signature(event: StreamEvent, trigger: TriggerResult) -> str:
        """A failure signature: coarse enough to group, fine enough to notice something new."""
        payload = event.payload or {}
        parts = [
            trigger.rule_name,
            str(payload.get("tool", "")),
            str(payload.get("data_type", "")),
            str(payload.get("error_code", payload.get("template_id", ""))),
        ]
        return "|".join(p for p in parts if p)

    @staticmethod
    def _hash(event: StreamEvent) -> str:
        return envelope_hash(
            source=event.source,
            event_type=event.event_type.value,
            severity=event.severity.value,
            payload=event.payload or {},
            metadata=event.metadata or {},
        )

    # ------------------------------------------------------------------ phase 1: register

    def register(self, event: StreamEvent) -> RegistrationResult:
        """Identity check. Call this before indexing or evaluating stateful trigger rules."""
        identity = ObservationIdentity(tenant=self.tenant, source=event.source, event_id=event.id)
        digest = self._hash(event)
        prior = self._seen.get(identity.key)
        if prior is not None:
            incident = self._incidents.get(prior.incident_id) if prior.incident_id else None
            if prior.payload_hash == digest:
                self._stats["duplicate_deliveries"] += 1
                if incident:
                    incident.record_duplicate_delivery()
                return RegistrationResult(
                    accepted=False, duplicate=True, observation_key=identity.key,
                    prior_incident_id=prior.incident_id, replay=prior.replay,
                    reason="duplicate delivery of an already accepted observation",
                )
            self._stats["conflicting_deliveries"] += 1
            self._quarantine.append(Observation(identity=identity, payload_hash=digest, event_time=event.event_time))
            if incident:
                incident.record_conflicting_delivery()
            return RegistrationResult(
                accepted=False, conflicting=True, observation_key=identity.key,
                prior_incident_id=prior.incident_id,
                reason="same observation identity with a different envelope; quarantined",
            )
        observation = Observation(
            identity=identity, payload_hash=digest, event_time=event.event_time, replay=event.is_replay
        )
        self._remember(observation)
        self._stats["observations_accepted"] += 1
        if event.is_replay:
            self._stats["replay_observations"] += 1
        return RegistrationResult(
            accepted=True, replay=event.is_replay, observation_key=identity.key,
            reason="replay observation recorded; isolated from live incidents" if event.is_replay else "observation accepted",
        )

    # ------------------------------------------------------------------ phase 2: correlate

    def correlate(self, event: StreamEvent, trigger: TriggerResult | None, registration: RegistrationResult | None = None) -> CoordinatorDecision:
        """Attach an *accepted* observation to an incident episode and decide on investigation."""
        identity_key = registration.observation_key if registration else ObservationIdentity(
            tenant=self.tenant, source=event.source, event_id=event.id
        ).key
        observation = self._seen.get(identity_key)
        if observation is None:
            raise ValueError("correlate() requires a registered observation; call register() first")

        if event.is_replay:
            return CoordinatorDecision(
                accepted=True, replay=True,
                reason="replay mode: observation recorded, no live incident, paging or investigation",
            )
        if trigger is None or not trigger.fired:
            return CoordinatorDecision(accepted=True, reason="observation recorded; no trigger fired")

        signal = self._signal_key_fn(event, trigger)
        signature = self.signature(event, trigger)
        incident_id = self._open_by_signal.get(signal.key)
        incident = self._incidents.get(incident_id) if incident_id else None
        if incident is not None and incident.is_open:
            return self._update_incident(incident, event, signature, observation)

        resolved = self._last_resolved.get(signal.key)
        if resolved is not None:
            resolved_id, resolved_at = resolved
            if event.event_time <= resolved_at:
                return self._late_evidence(resolved_id, observation)
        return self._open_incident(event, signal, signature, observation, resolved)

    def ingest(self, event: StreamEvent, trigger: TriggerResult | None) -> CoordinatorDecision:
        """register() + correlate() for callers with no stateful work in between."""
        registration = self.register(event)
        if not registration.accepted:
            incident = self._incidents.get(registration.prior_incident_id) if registration.prior_incident_id else None
            return CoordinatorDecision(
                accepted=False,
                duplicate=registration.duplicate,
                conflicting=registration.conflicting,
                replay=registration.replay,
                incident_id=registration.prior_incident_id,
                incident_state=incident.state if incident else None,
                incident_version=incident.version if incident else None,
                reason=registration.reason,
            )
        return self.correlate(event, trigger, registration)

    # ------------------------------------------------------------------ incident handling

    def _late_evidence(self, incident_id: str, observation: Observation) -> CoordinatorDecision:
        incident = self._incidents[incident_id]
        incident.record_late_evidence(observation.identity.key)
        observation.incident_id = incident.id
        self._stats["late_evidence"] += 1
        return CoordinatorDecision(
            accepted=True, late_evidence=True, incident_id=incident.id,
            incident_state=incident.state, incident_version=incident.version,
            reason="evidence occurred before the episode was resolved; timeline corrected, no new episode",
        )

    def _open_incident(
        self,
        event: StreamEvent,
        signal: SignalKey,
        signature: str,
        observation: Observation,
        resolved: tuple[str, datetime] | None,
    ) -> CoordinatorDecision:
        reason = MaterialChange.NEW_INCIDENT
        related: str | None = None
        if resolved is not None:
            resolved_id, resolved_at = resolved
            if event.event_time - resolved_at <= self.recurrence_window:
                reason = MaterialChange.RECURRENCE_AFTER_RESOLVED
                related = resolved_id

        incident = Incident(tenant=self.tenant, signal_key=signal.key, severity=event.severity)
        incident.attributes.update(
            {
                "service": signal.service,
                "environment": signal.environment,
                "failure_domain": signal.failure_domain,
                "symptom_class": signal.symptom_class,
            }
        )
        if related:
            incident.attributes["related_incident"] = related
        incident.attach_observation(observation.identity.key, signature)
        observation.incident_id = incident.id
        self._incidents[incident.id] = incident
        self._open_by_signal[signal.key] = incident.id
        self._stats["incidents_created"] += 1

        request = self._request_investigation(incident, reason)
        return CoordinatorDecision(
            accepted=True,
            incident_id=incident.id,
            incident_state=incident.state,
            incident_version=incident.version,
            material_change=reason,
            investigation_request=request,
            reason=f"new incident episode ({reason.value})",
        )

    def _update_incident(
        self, incident: Incident, event: StreamEvent, signature: str, observation: Observation
    ) -> CoordinatorDecision:
        previous_severity = incident.severity
        known_signature = signature in incident.signatures
        incident.attach_observation(observation.identity.key, signature)
        observation.incident_id = incident.id

        change = MaterialChange.NONE
        if SEVERITY_ORDER[event.severity] > SEVERITY_ORDER[previous_severity]:
            incident.severity = event.severity
            change = MaterialChange.SEVERITY_INCREASE
        elif not known_signature:
            change = MaterialChange.NEW_SIGNATURE
        elif incident.last_investigation_at is None or _now() - incident.last_investigation_at >= self.cooldown:
            change = MaterialChange.COOLDOWN_ELAPSED

        if change is MaterialChange.NONE:
            self._stats["evidence_only_updates"] += 1
            return CoordinatorDecision(
                accepted=True,
                incident_id=incident.id,
                incident_state=incident.state,
                incident_version=incident.version,
                material_change=change,
                reason="evidence updated on existing incident; no material change",
            )

        request = self._request_investigation(incident, change)
        return CoordinatorDecision(
            accepted=True,
            incident_id=incident.id,
            incident_state=incident.state,
            incident_version=incident.version,
            material_change=change,
            investigation_request=request,
            reason=f"material change on existing incident ({change.value})",
        )

    def _request_investigation(self, incident: Incident, reason: MaterialChange) -> InvestigationRequest:
        request_id = InvestigationRequest.make_id(incident.id, incident.version)
        existing = self._requests.get(request_id)
        if existing:
            return existing  # two workers on the same transition → one durable request
        request = InvestigationRequest(
            id=request_id,
            incident_id=incident.id,
            evidence_snapshot_version=incident.version,
            reason=reason,
            context={"signal_key": incident.signal_key, "severity": incident.severity.value},
        )
        self._requests[request_id] = request
        incident.mark_investigated()
        self._stats["investigations_requested"] += 1
        return request

    # ------------------------------------------------------------------ lifecycle operations

    def transition(self, incident_id: str, to: IncidentState, actor: str, reason: str) -> Incident:
        """Apply an explicit, recorded lifecycle transition (the only way state changes)."""
        if not actor:
            raise ValueError("a transition requires an actor")
        incident = self.get_incident(incident_id)
        incident.transition(to, actor=actor, reason=reason)
        if not incident.is_open:
            self._open_by_signal.pop(incident.signal_key, None)
            if to is IncidentState.RESOLVED:
                self._last_resolved[incident.signal_key] = (incident.id, _now())
        return incident

    def link_external(self, incident_id: str, system: str, external_id: str) -> None:
        """Durable mapping to an external incident/ticket. Never search-by-title-and-create."""
        self.get_incident(incident_id).external_refs[system] = external_id

    # ------------------------------------------------------------------ accessors

    def get_incident(self, incident_id: str) -> Incident:
        try:
            return self._incidents[incident_id]
        except KeyError:
            raise KeyError(f"unknown incident {incident_id}") from None

    def has_incident(self, incident_id: str) -> bool:
        return incident_id in self._incidents

    @property
    def incidents(self) -> list[Incident]:
        return list(self._incidents.values())

    @property
    def open_incidents(self) -> list[Incident]:
        return [i for i in self._incidents.values() if i.is_open]

    @property
    def investigation_requests(self) -> list[InvestigationRequest]:
        return list(self._requests.values())

    @property
    def quarantine(self) -> list[Observation]:
        return list(self._quarantine)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            **self._stats,
            "open_incidents": len(self.open_incidents),
            "total_incidents": len(self._incidents),
            "quarantined": len(self._quarantine),
            "dedup_cache_size": len(self._seen),
        }

    # ------------------------------------------------------------------ internals

    def _remember(self, observation: Observation) -> None:
        self._seen[observation.identity.key] = observation
        while len(self._seen) > self.dedup_retention:
            self._seen.popitem(last=False)
