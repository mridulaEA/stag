"""Incident lifecycle as a versioned state machine.

    DETECTED → CORRELATED → TRIAGED → INVESTIGATING
                                          ↓
                                  ACTION_PROPOSED
                                          ↓
                             AUTHORIZED / HELD / REJECTED
                                          ↓
                                     EXECUTING
                                          ↓
                                     VERIFYING
                                          ↓
                              RESOLVED / INVESTIGATING (again)

Transitions are implemented and versioned here. An agent saying "resolved" does not move an
incident to RESOLVED; only an explicit, recorded transition does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, ClassVar
from uuid import uuid4

from pydantic import BaseModel, Field

from stag.models.schemas import Severity


def _now() -> datetime:
    return datetime.now(UTC)


class IncidentState(str, Enum):
    DETECTED = "detected"
    CORRELATED = "correlated"
    TRIAGED = "triaged"
    INVESTIGATING = "investigating"
    ACTION_PROPOSED = "action_proposed"
    AUTHORIZED = "authorized"
    HELD = "held"
    REJECTED = "rejected"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


TERMINAL_STATES = {IncidentState.RESOLVED, IncidentState.DISMISSED}

# Allowed transitions. Every edge is explicit so a reviewer can audit the lifecycle.
TRANSITIONS: dict[IncidentState, set[IncidentState]] = {
    IncidentState.DETECTED: {IncidentState.CORRELATED, IncidentState.TRIAGED, IncidentState.DISMISSED},
    IncidentState.CORRELATED: {IncidentState.TRIAGED, IncidentState.DISMISSED},
    IncidentState.TRIAGED: {IncidentState.INVESTIGATING, IncidentState.DISMISSED, IncidentState.RESOLVED},
    IncidentState.INVESTIGATING: {
        IncidentState.ACTION_PROPOSED,
        IncidentState.TRIAGED,
        IncidentState.VERIFYING,
        IncidentState.DISMISSED,
    },
    IncidentState.ACTION_PROPOSED: {IncidentState.AUTHORIZED, IncidentState.HELD, IncidentState.REJECTED},
    IncidentState.AUTHORIZED: {IncidentState.EXECUTING, IncidentState.HELD, IncidentState.REJECTED},
    IncidentState.HELD: {IncidentState.AUTHORIZED, IncidentState.REJECTED, IncidentState.INVESTIGATING},
    IncidentState.REJECTED: {IncidentState.INVESTIGATING, IncidentState.DISMISSED},
    IncidentState.EXECUTING: {IncidentState.VERIFYING, IncidentState.INVESTIGATING},
    IncidentState.VERIFYING: {IncidentState.RESOLVED, IncidentState.INVESTIGATING},
    IncidentState.RESOLVED: set(),
    IncidentState.DISMISSED: set(),
}


class MaterialChange(str, Enum):
    """Why an investigation may be (re)started. Repeated evidence alone is NONE."""

    NONE = "none"
    NEW_INCIDENT = "new_incident"
    SEVERITY_INCREASE = "severity_increase"
    NEW_SIGNATURE = "new_signature"
    RECURRENCE_AFTER_RESOLVED = "recurrence_after_resolved"
    MITIGATION_FAILED = "mitigation_failed"
    COOLDOWN_ELAPSED = "cooldown_elapsed"
    MANUAL = "manual"


class InvalidTransition(Exception):
    """Raised when a lifecycle transition is not permitted."""


class Transition(BaseModel):
    from_state: IncidentState
    to_state: IncidentState
    actor: str
    reason: str
    at: datetime = Field(default_factory=_now)
    version: int


class Incident(BaseModel):
    """One incident episode with durable, versioned state."""

    id: str = Field(default_factory=lambda: f"INC-{uuid4().hex[:8].upper()}")
    tenant: str = "default"
    signal_key: str
    state: IncidentState = IncidentState.DETECTED
    version: int = 1
    severity: Severity = Severity.LOW
    first_seen: datetime = Field(default_factory=_now)
    last_seen: datetime = Field(default_factory=_now)
    observation_count: int = 0
    duplicate_delivery_count: int = 0
    conflicting_delivery_count: int = 0
    example_observation_ids: list[str] = Field(default_factory=list)
    signatures: list[str] = Field(default_factory=list)
    external_refs: dict[str, str] = Field(default_factory=dict)
    last_investigation_version: int | None = None
    last_investigation_at: datetime | None = None
    history: list[Transition] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)

    MAX_EXAMPLES: ClassVar[int] = 10

    @property
    def is_open(self) -> bool:
        return self.state not in TERMINAL_STATES

    def can_transition(self, to: IncidentState) -> bool:
        return to in TRANSITIONS[self.state]

    def transition(self, to: IncidentState, actor: str, reason: str) -> Transition:
        """Apply a lifecycle transition. Raises InvalidTransition when the edge is not allowed."""
        if not self.can_transition(to):
            raise InvalidTransition(f"{self.id}: {self.state.value} → {to.value} is not permitted")
        self.version += 1
        record = Transition(
            from_state=self.state, to_state=to, actor=actor, reason=reason, version=self.version
        )
        self.state = to
        self.history.append(record)
        return record

    def attach_observation(self, observation_id: str, signature: str | None = None) -> None:
        """Attach a genuinely new observation (not a redelivery). Bumps the evidence version."""
        self.observation_count += 1
        self.version += 1
        self.last_seen = _now()
        if len(self.example_observation_ids) < self.MAX_EXAMPLES:
            self.example_observation_ids.append(observation_id)
        if signature and signature not in self.signatures:
            self.signatures.append(signature)

    def record_duplicate_delivery(self) -> None:
        """A redelivery of an already-accepted observation. Counted separately; no state change."""
        self.duplicate_delivery_count += 1

    def record_conflicting_delivery(self) -> None:
        """Same identity, different payload. Quarantined and counted; never silently overwritten."""
        self.conflicting_delivery_count += 1

    def mark_investigated(self) -> None:
        self.last_investigation_version = self.version
        self.last_investigation_at = _now()
