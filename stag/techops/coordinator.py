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
from stag.techops.actions import VerificationResult
from stag.techops.identity import ObservationIdentity, SignalKey, envelope_hash
from stag.techops.incident import (
    Incident,
    IncidentState,
    InvalidTransition,
    MaterialChange,
    VerificationRecord,
)
from stag.techops.store import ConflictError, ControlStore, InMemoryControlStore

PROCESSING_MODES = ("live", "replay", "simulation")

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
    # work lifecycle (R16): queued → running (leased) → succeeded | failed; failed is re-claimable
    status: str = "queued"
    attempts: int = 0
    lease_owner: str | None = None
    lease_until: datetime | None = None
    result_summary: str | None = None
    completed_at: datetime | None = None

    @staticmethod
    def make_id(incident_id: str, version: int) -> str:
        digest = hashlib.sha256(f"{incident_id}:{version}".encode()).hexdigest()[:12]
        return f"INV-{digest}"


class _Ref(BaseModel):
    incident_id: str


class _ResolvedRef(BaseModel):
    incident_id: str
    at: datetime


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
    processing_mode: ``live`` (default) rejects replay-tagged events before any effect; ``replay``
        accepts only replay-tagged events into an isolated namespace and never opens incidents;
        ``simulation`` is live behaviour that additionally accepts simulated verification evidence.
        The mode is runtime configuration, never chosen by event metadata.
    """

    def __init__(
        self,
        tenant: str = "default",
        investigation_cooldown_seconds: int = 300,
        recurrence_window_seconds: int = 3600,
        dedup_retention: int = 50_000,
        signal_key_fn: Callable[[StreamEvent, TriggerResult], SignalKey] | None = None,
        processing_mode: str = "live",
        store: ControlStore | None = None,
        max_cas_retries: int = 5,
    ) -> None:
        if investigation_cooldown_seconds < 0 or recurrence_window_seconds < 0 or dedup_retention < 1:
            raise ValueError("cooldown and recurrence window must be >= 0 and dedup_retention >= 1")
        if processing_mode not in PROCESSING_MODES:
            raise ValueError(f"processing_mode must be one of {PROCESSING_MODES}")
        self.processing_mode = processing_mode
        self.store = store or InMemoryControlStore()
        self.max_cas_retries = max_cas_retries
        self.tenant = tenant
        self.cooldown = timedelta(seconds=investigation_cooldown_seconds)
        self.recurrence_window = timedelta(seconds=recurrence_window_seconds)
        self.dedup_retention = dedup_retention
        self._signal_key_fn = signal_key_fn or self.default_signal_key

        # dedup cache for the hot path; the store is the source of truth
        self._seen: OrderedDict[str, Observation] = OrderedDict()
        self._seen_replay: OrderedDict[str, Observation] = OrderedDict()
        self._stats = {
            "observations_accepted": 0,
            "duplicate_deliveries": 0,
            "conflicting_deliveries": 0,
            "replay_observations": 0,
            "replay_rejected": 0,
            "verifications_recorded": 0,
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

    # ------------------------------------------------------------------ store access

    OBS, OBS_REPLAY, INCIDENTS, OPEN, RESOLVED, REQUESTS, QUARANTINE = (
        "observations", "observations_replay", "incidents", "open_signals", "resolved_signals",
        "investigation_requests", "quarantine",
    )

    def _load_observation(self, key: str, replay: bool = False) -> Observation | None:
        cache = self._seen_replay if replay else self._seen
        cached = cache.get(key)
        if cached is not None:
            return cached
        row = self.store.get(self.OBS_REPLAY if replay else self.OBS, key)
        if row is None:
            return None
        obs = self.store.materialize(row[0], Observation)
        cache[key] = obs
        while len(cache) > self.dedup_retention:
            cache.popitem(last=False)
        return obs

    def _save_observation(self, obs: Observation, replay: bool = False) -> None:
        coll = self.OBS_REPLAY if replay else self.OBS
        row = self.store.get(coll, obs.identity.key)
        self.store.put(coll, obs.identity.key, obs, row[1] if row else None)

    def _load_incident(self, incident_id: str) -> tuple[Incident, int]:
        row = self.store.get(self.INCIDENTS, incident_id)
        if row is None:
            raise KeyError(f"unknown incident {incident_id}")
        return self.store.materialize(row[0], Incident), row[1]

    def _save_incident(self, incident: Incident, row_version: int | None) -> int:
        return self.store.put(
            self.INCIDENTS, incident.id, incident, row_version,
            status="open" if incident.is_open else incident.state.value, scope=incident.signal_key,
        )

    def _open_incident_id(self, signal_key: str) -> str | None:
        row = self.store.get(self.OPEN, signal_key)
        return row[0]["incident_id"] if row and isinstance(row[0], dict) else (
            self.store.materialize(row[0], _Ref).incident_id if row else None
        )

    def _last_resolved_for(self, signal_key: str) -> tuple[str, datetime] | None:
        row = self.store.get(self.RESOLVED, signal_key)
        if row is None:
            return None
        ref = self.store.materialize(row[0], _ResolvedRef)
        return ref.incident_id, ref.at

    def _with_cas_retry(self, fn):
        """Run ``fn()`` which performs load → mutate → CAS save; retry on conflict."""
        last: ConflictError | None = None
        for _ in range(self.max_cas_retries):
            try:
                return fn()
            except ConflictError as e:
                last = e
        raise last  # type: ignore[misc]

    # ------------------------------------------------------------------ phase 1: register

    def register(self, event: StreamEvent) -> RegistrationResult:
        """Identity check. Call this before indexing or evaluating stateful trigger rules.

        Replay-tagged events are rejected in live/simulation mode and isolated in replay mode, so
        historical traffic can never touch live dedup state, frequency counts, indexes or incidents.
        """
        identity = ObservationIdentity(tenant=self.tenant, source=event.source, event_id=event.id)
        digest = self._hash(event)
        if self.processing_mode == "replay":
            if not event.is_replay:
                return RegistrationResult(accepted=False, observation_key=identity.key,
                                          reason="live event rejected: coordinator is in replay mode")
            obs = Observation(identity=identity, payload_hash=digest, event_time=event.event_time, replay=True)
            if not self.store.insert_unique(self.OBS_REPLAY, identity.key, obs, status="replay"):
                self._stats["duplicate_deliveries"] += 1
                return RegistrationResult(accepted=False, duplicate=True, replay=True, observation_key=identity.key,
                                          reason="duplicate replay delivery")
            self._seen_replay[identity.key] = obs
            self._stats["replay_observations"] += 1
            return RegistrationResult(accepted=True, replay=True, observation_key=identity.key,
                                      reason="replay observation recorded in the isolated replay namespace")
        if event.is_replay:
            self._stats["replay_rejected"] += 1
            return RegistrationResult(accepted=False, replay=True, observation_key=identity.key,
                                      reason="replay-tagged event rejected in live mode; use a replay-mode coordinator")

        observation = Observation(identity=identity, payload_hash=digest, event_time=event.event_time)
        # Unique insert is the race arbiter: exactly one worker accepts a given identity.
        if self.store.insert_unique(self.OBS, identity.key, observation, status="accepted"):
            self._remember(observation)
            self._stats["observations_accepted"] += 1
            return RegistrationResult(accepted=True, observation_key=identity.key, reason="observation accepted")

        prior = self._load_observation(identity.key)
        if prior is None:  # row vanished (purged) between insert and load; treat as new
            self._remember(observation)
            self._save_observation(observation)
            self._stats["observations_accepted"] += 1
            return RegistrationResult(accepted=True, observation_key=identity.key, reason="observation accepted")
        if prior.payload_hash == digest:
            self._stats["duplicate_deliveries"] += 1
            if prior.incident_id:
                self._bump_counter(prior.incident_id, "duplicate")
            return RegistrationResult(
                accepted=False, duplicate=True, observation_key=identity.key,
                prior_incident_id=prior.incident_id,
                reason="duplicate delivery of an already accepted observation",
            )
        self._stats["conflicting_deliveries"] += 1
        self.store.append(self.QUARANTINE, Observation(identity=identity, payload_hash=digest, event_time=event.event_time))
        if prior.incident_id:
            self._bump_counter(prior.incident_id, "conflicting")
        return RegistrationResult(
            accepted=False, conflicting=True, observation_key=identity.key,
            prior_incident_id=prior.incident_id,
            reason="same observation identity with a different envelope; quarantined",
        )

    def _bump_counter(self, incident_id: str, kind: str) -> None:
        def op():
            incident, rv = self._load_incident(incident_id)
            if kind == "duplicate":
                incident.record_duplicate_delivery()
            else:
                incident.record_conflicting_delivery()
            self._save_incident(incident, rv)

        try:
            self._with_cas_retry(op)
        except KeyError:
            pass

    # ------------------------------------------------------------------ phase 2: correlate

    def correlate(self, event: StreamEvent, trigger: TriggerResult | None, registration: RegistrationResult | None = None) -> CoordinatorDecision:
        """Attach an *accepted* observation to an incident episode and decide on investigation."""
        identity_key = registration.observation_key if registration else ObservationIdentity(
            tenant=self.tenant, source=event.source, event_id=event.id
        ).key
        if self.processing_mode == "replay" or event.is_replay:
            return CoordinatorDecision(
                accepted=True, replay=True,
                reason="replay: observation recorded in the replay namespace; no incident, paging or investigation",
            )
        observation = self._load_observation(identity_key)
        if observation is None:
            raise ValueError("correlate() requires a registered observation; call register() first")
        if trigger is None or not trigger.fired:
            return CoordinatorDecision(accepted=True, reason="observation recorded; no trigger fired")

        signal = self._signal_key_fn(event, trigger)
        signature = self.signature(event, trigger)

        def op():
            with self.store.transaction():
                incident_id = self._open_incident_id(signal.key)
                if incident_id is not None:
                    incident, rv = self._load_incident(incident_id)
                    if incident.is_open:
                        return self._update_incident(incident, rv, event, signature, observation)
                resolved = self._last_resolved_for(signal.key)
                if resolved is not None:
                    resolved_id, resolved_at = resolved
                    if event.event_time <= resolved_at:
                        return self._late_evidence(resolved_id, observation)
                return self._open_incident(event, signal, signature, observation, resolved)

        return self._with_cas_retry(op)

    def ingest(self, event: StreamEvent, trigger: TriggerResult | None) -> CoordinatorDecision:
        """register() + correlate() for callers with no stateful work in between."""
        registration = self.register(event)
        if not registration.accepted:
            incident = None
            if registration.prior_incident_id and self.has_incident(registration.prior_incident_id):
                incident = self.get_incident(registration.prior_incident_id)
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
        incident, rv = self._load_incident(incident_id)
        incident.record_late_evidence(observation.identity.key)
        self._save_incident(incident, rv)
        observation.incident_id = incident.id
        self._save_observation(observation)
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
                "signal_display": signal.display,
            }
        )
        if related:
            incident.attributes["related_incident"] = related
        incident.attach_observation(observation.identity.key, signature)
        # The open-signal row is the race arbiter for episode creation: one winner per signal key.
        if not self.store.insert_unique(self.OPEN, signal.key, _Ref(incident_id=incident.id)):
            raise ConflictError(f"another worker opened an incident for {signal.key}")
        observation.incident_id = incident.id
        self._save_observation(observation)
        self._stats["incidents_created"] += 1
        request = self._request_investigation(incident, reason)
        self._save_incident(incident, None)
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
        self, incident: Incident, row_version: int, event: StreamEvent, signature: str, observation: Observation
    ) -> CoordinatorDecision:
        previous_severity = incident.severity
        known_signature = signature in incident.signatures
        incident.attach_observation(observation.identity.key, signature)
        observation.incident_id = incident.id
        self._save_observation(observation)

        change = MaterialChange.NONE
        if SEVERITY_ORDER[event.severity] > SEVERITY_ORDER[previous_severity]:
            incident.severity = event.severity
            change = MaterialChange.SEVERITY_INCREASE
        elif not known_signature:
            change = MaterialChange.NEW_SIGNATURE
        elif incident.last_investigation_at is None or _now() - incident.last_investigation_at >= self.cooldown:
            change = MaterialChange.COOLDOWN_ELAPSED

        if change is MaterialChange.NONE:
            self._save_incident(incident, row_version)
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
        self._save_incident(incident, row_version)
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
        request = InvestigationRequest(
            id=request_id,
            incident_id=incident.id,
            evidence_snapshot_version=incident.version,
            reason=reason,
            context={"signal_key": incident.signal_key, "severity": incident.severity.value},
        )
        # Unique id = (incident, version): two workers on the same transition → one durable request.
        if not self.store.insert_unique(self.REQUESTS, request_id, request, status="queued", scope=incident.id):
            return self.get_request(request_id)
        incident.mark_investigated()
        self._stats["investigations_requested"] += 1
        return request

    # ------------------------------------------------------------------ investigation work lifecycle (R16)

    def get_request(self, request_id: str) -> InvestigationRequest:
        row = self.store.get(self.REQUESTS, request_id)
        if row is None:
            raise KeyError(f"unknown investigation request {request_id}")
        return self.store.materialize(row[0], InvestigationRequest)

    def claim_investigation(self, request_id: str, worker: str, lease_seconds: int = 300) -> InvestigationRequest | None:
        """Lease a queued (or failed / lease-expired) request to one worker. None when unavailable."""
        claimed = self.store.claim(self.REQUESTS, request_id, worker, lease_seconds, from_statuses=("queued", "failed"))
        if claimed is None:
            return None
        request = self.store.materialize(claimed[0], InvestigationRequest)
        request.status = "running"
        request.attempts += 1
        request.lease_owner = worker
        request.lease_until = _now() + timedelta(seconds=lease_seconds)
        self.store.put(self.REQUESTS, request_id, request, claimed[1], status="running", scope=request.incident_id)
        return request

    def complete_investigation(self, request_id: str, worker: str, succeeded: bool, summary: str = "") -> InvestigationRequest:
        """Record the outcome of leased work. A stale lease (another worker claimed it) is refused."""
        row = self.store.get(self.REQUESTS, request_id)
        if row is None:
            raise KeyError(f"unknown investigation request {request_id}")
        request = self.store.materialize(row[0], InvestigationRequest)
        if request.status != "running" or request.lease_owner != worker:
            raise ConflictError(f"{request_id}: not leased by {worker} (status {request.status}, owner {request.lease_owner})")
        request.status = "succeeded" if succeeded else "failed"
        request.result_summary = summary[:500] if summary else None
        request.completed_at = _now()
        request.lease_owner = None
        request.lease_until = None
        self.store.put(self.REQUESTS, request_id, request, row[1], status=request.status, scope=request.incident_id)
        return request

    def pending_investigations(self, limit: int | None = None) -> list[InvestigationRequest]:
        rows = self.store.list(self.REQUESTS, status="queued", limit=limit) + self.store.list(self.REQUESTS, status="failed", limit=limit)
        return [self.store.materialize(doc, InvestigationRequest) for _, doc, _ in rows]

    def is_request_stale(self, request_id: str) -> bool:
        """True when the incident's evidence moved past the snapshot the request was made for."""
        request = self.get_request(request_id)
        incident = self.get_incident(request.incident_id)
        return incident.version != request.evidence_snapshot_version

    # ------------------------------------------------------------------ lifecycle operations

    def transition(self, incident_id: str, to: IncidentState, actor: str, reason: str) -> Incident:
        """Apply an explicit, recorded lifecycle transition (the only way state changes)."""
        if not actor:
            raise ValueError("a transition requires an actor")

        def op():
            with self.store.transaction():
                incident, rv = self._load_incident(incident_id)
                incident.transition(to, actor=actor, reason=reason)
                self._save_incident(incident, rv)
                if not incident.is_open:
                    open_row = self.store.get(self.OPEN, incident.signal_key)
                    if open_row and self.store.materialize(open_row[0], _Ref).incident_id == incident.id:
                        self.store.delete(self.OPEN, incident.signal_key)
                    if to is IncidentState.RESOLVED:
                        self.store.put(self.RESOLVED, incident.signal_key, _ResolvedRef(incident_id=incident.id, at=_now()), None)
                return incident

        return self._with_cas_retry(op)

    def record_verification(
        self,
        incident_id: str,
        result: VerificationResult,
        operation_id: str | None = None,
        verified_by: str = "verifier",
    ) -> Incident:
        """Attach an independent verification result to an incident at its current version.

        Simulated evidence is accepted only by a coordinator in ``simulation`` mode; a live
        coordinator refuses it so a fixture can never authorize a live RESOLVED.
        """
        if result.simulated and self.processing_mode != "simulation":
            raise InvalidTransition(f"{incident_id}: simulated verification evidence is not accepted in {self.processing_mode} mode")
        if result.healthy is None:
            return self.get_incident(incident_id)  # unknown: recorded nowhere as success

        def op():
            incident, rv = self._load_incident(incident_id)
            incident.record_verification(
                VerificationRecord(
                    healthy=result.healthy, at_version=incident.version, operation_id=operation_id,
                    source=result.source, window=result.window, signals=list(result.signals),
                    simulated=result.simulated, verified_by=verified_by,
                )
            )
            self._save_incident(incident, rv)
            return incident

        incident = self._with_cas_retry(op)
        self._stats["verifications_recorded"] += 1
        return incident

    def link_external(self, incident_id: str, system: str, external_id: str) -> None:
        """Durable mapping to an external incident/ticket. Never search-by-title-and-create."""

        def op():
            incident, rv = self._load_incident(incident_id)
            incident.external_refs[system] = external_id
            self._save_incident(incident, rv)

        self._with_cas_retry(op)

    # ------------------------------------------------------------------ accessors

    def get_incident(self, incident_id: str) -> Incident:
        return self._load_incident(incident_id)[0]

    def has_incident(self, incident_id: str) -> bool:
        return self.store.get(self.INCIDENTS, incident_id) is not None

    @property
    def incidents(self) -> list[Incident]:
        return [self.store.materialize(doc, Incident) for _, doc, _ in self.store.list(self.INCIDENTS)]

    @property
    def open_incidents(self) -> list[Incident]:
        return [self.store.materialize(doc, Incident) for _, doc, _ in self.store.list(self.INCIDENTS, status="open")]

    @property
    def investigation_requests(self) -> list[InvestigationRequest]:
        return [self.store.materialize(doc, InvestigationRequest) for _, doc, _ in self.store.list(self.REQUESTS)]

    @property
    def quarantine(self) -> list[Observation]:
        log = getattr(self.store, "log", None)
        return [self.store.materialize(d, Observation) for d in log(self.QUARANTINE)] if log else []

    @property
    def stats(self) -> dict[str, Any]:
        return {
            **self._stats,
            "open_incidents": len(self.store.list(self.INCIDENTS, status="open")),
            "total_incidents": self.store.count(self.INCIDENTS),
            "quarantined": len(self.quarantine),
            "dedup_cache_size": len(self._seen),
            "store": type(self.store).__name__,
        }

    # ------------------------------------------------------------------ internals

    def _remember(self, observation: Observation) -> None:
        self._seen[observation.identity.key] = observation
        while len(self._seen) > self.dedup_retention:
            self._seen.popitem(last=False)
