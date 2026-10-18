"""Incident coordinator: dedup, correlate, and decide whether an investigation is warranted.

The coordinator sits between the trigger engine and the LLM investigation path.

    Event + Trigger
        │
        ▼
    Observation identity  ──► seen before, same payload?  → duplicate delivery (count only)
        │                      seen before, other payload? → conflicting (quarantine)
        ▼
    Signal grouping key   ──► open incident?  → attach, bump version, evaluate material change
        │                      none?          → new incident episode (DETECTED)
        ▼
    Material change?      ──► yes → InvestigationRequest (deterministic id)
                               no  → evidence updated, no model call

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
from stag.techops.identity import ObservationIdentity, SignalKey, payload_hash
from stag.techops.incident import Incident, IncidentState, MaterialChange

SEVERITY_ORDER = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


def _now() -> datetime:
    return datetime.now(UTC)


class Observation(BaseModel):
    identity: ObservationIdentity
    payload_hash: str
    received_at: datetime = Field(default_factory=_now)
    event_time: datetime | None = None
    incident_id: str | None = None


class InvestigationRequest(BaseModel):
    """A uniquely identified request for bounded investigation.

    The id is derived from (incident, evidence version), so two workers processing the same
    transition produce one durable request, not two.
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
    incident_id: str | None = None
    incident_state: IncidentState | None = None
    incident_version: int | None = None
    material_change: MaterialChange = MaterialChange.NONE
    investigation_request: InvestigationRequest | None = None
    reason: str = ""


class IncidentCoordinator:
    """Deterministic incident coordination with controlled investigation cadence.

    Parameters
    ----------
    tenant:
        Tenant scope enforced on every identity.
    investigation_cooldown_seconds:
        Minimum time between routine re-investigations of the same incident. Severity increases
        and new signatures bypass the cooldown.
    recurrence_window_seconds:
        A matching signal arriving within this window after RESOLVED re-opens as a *new* episode
        tagged RECURRENCE_AFTER_RESOLVED; after the window it is simply a new incident.
    dedup_retention:
        Number of observation identities kept for duplicate detection. Size it to the supported
        redelivery / replay horizon; a tiny cache cannot protect against replay weeks later.
    signal_key_fn:
        Optional override to derive a SignalKey from (event, trigger). The default maps
        event.source → service, metadata.environment → environment, event_type → failure domain,
        trigger.rule_name → symptom class.
    """

    def __init__(
        self,
        tenant: str = "default",
        investigation_cooldown_seconds: int = 300,
        recurrence_window_seconds: int = 3600,
        dedup_retention: int = 50_000,
        signal_key_fn: Callable[[StreamEvent, TriggerResult], SignalKey] | None = None,
    ) -> None:
        self.tenant = tenant
        self.cooldown = timedelta(seconds=investigation_cooldown_seconds)
        self.recurrence_window = timedelta(seconds=recurrence_window_seconds)
        self.dedup_retention = dedup_retention
        self._signal_key_fn = signal_key_fn or self.default_signal_key

        self._seen: OrderedDict[str, Observation] = OrderedDict()
        self._quarantine: list[Observation] = []
        self._incidents: dict[str, Incident] = {}
        self._open_by_signal: dict[str, str] = {}
        self._resolved_at: dict[str, datetime] = {}
        self._requests: dict[str, InvestigationRequest] = {}
        self._stats = {
            "observations_accepted": 0,
            "duplicate_deliveries": 0,
            "conflicting_deliveries": 0,
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
        parts = [trigger.rule_name, str(payload.get("tool", "")), str(payload.get("data_type", ""))]
        return "|".join(p for p in parts if p)

    # ------------------------------------------------------------------ main entry point

    def ingest(self, event: StreamEvent, trigger: TriggerResult | None) -> CoordinatorDecision:
        """Register an observation and decide whether it warrants investigation."""
        identity = ObservationIdentity(tenant=self.tenant, source=event.source, event_id=event.id)
        digest = payload_hash(event.payload or {})

        prior = self._seen.get(identity.key)
        if prior is not None:
            if prior.payload_hash == digest:
                self._stats["duplicate_deliveries"] += 1
                incident = self._incidents.get(prior.incident_id) if prior.incident_id else None
                if incident:
                    incident.record_duplicate_delivery()
                return CoordinatorDecision(
                    accepted=False,
                    duplicate=True,
                    incident_id=prior.incident_id,
                    incident_state=incident.state if incident else None,
                    incident_version=incident.version if incident else None,
                    reason="duplicate delivery of an already accepted observation",
                )
            self._stats["conflicting_deliveries"] += 1
            conflicting = Observation(identity=identity, payload_hash=digest, event_time=event.timestamp)
            self._quarantine.append(conflicting)
            incident = self._incidents.get(prior.incident_id) if prior.incident_id else None
            if incident:
                incident.record_conflicting_delivery()
            return CoordinatorDecision(
                accepted=False,
                conflicting=True,
                incident_id=prior.incident_id,
                reason="same observation identity with a different payload; quarantined",
            )

        observation = Observation(identity=identity, payload_hash=digest, event_time=event.timestamp)
        self._remember(observation)
        self._stats["observations_accepted"] += 1

        if trigger is None or not trigger.fired:
            return CoordinatorDecision(accepted=True, reason="observation recorded; no trigger fired")

        signal = self._signal_key_fn(event, trigger)
        signature = self.signature(event, trigger)
        incident_id = self._open_by_signal.get(signal.key)
        incident = self._incidents.get(incident_id) if incident_id else None

        if incident is None or not incident.is_open:
            return self._open_incident(event, signal, signature, observation)

        return self._update_incident(incident, event, signature, observation)

    # ------------------------------------------------------------------ incident handling

    def _open_incident(
        self, event: StreamEvent, signal: SignalKey, signature: str, observation: Observation
    ) -> CoordinatorDecision:
        reason = MaterialChange.NEW_INCIDENT
        resolved_at = self._resolved_at.get(signal.key)
        if resolved_at and _now() - resolved_at <= self.recurrence_window:
            reason = MaterialChange.RECURRENCE_AFTER_RESOLVED

        incident = Incident(tenant=self.tenant, signal_key=signal.key, severity=event.severity)
        incident.attributes.update(
            {
                "service": signal.service,
                "environment": signal.environment,
                "failure_domain": signal.failure_domain,
                "symptom_class": signal.symptom_class,
            }
        )
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
        elif incident.last_investigation_at is None:
            change = MaterialChange.COOLDOWN_ELAPSED
        elif _now() - incident.last_investigation_at >= self.cooldown:
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
        incident = self.get_incident(incident_id)
        incident.transition(to, actor=actor, reason=reason)
        if not incident.is_open:
            self._open_by_signal.pop(incident.signal_key, None)
            if to is IncidentState.RESOLVED:
                self._resolved_at[incident.signal_key] = _now()
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
