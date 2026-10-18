"""Action ledger: retry-safe remediation with durable operation identity.

    PREPARED → AUTHORIZED → DISPATCHED → VERIFIED
                                ↓
                         OUTCOME_UNKNOWN → RECONCILING → VERIFIED / FAILED

The operation id is derived from (incident, plan step, target, parameters). Retries reuse it.
A different operation requires a new reviewed decision. Routine evidence updates on the incident
must not mint a fresh action identity, or the same mitigation can be authorized repeatedly.

A successful command is not verified recovery. Verification checks externally observable health
over a defined observation period; missing telemetry is an unknown state, not evidence of recovery.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(UTC)


class ActionState(str, Enum):
    PREPARED = "prepared"
    AUTHORIZED = "authorized"
    DISPATCHED = "dispatched"
    VERIFIED = "verified"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECONCILING = "reconciling"
    FAILED = "failed"
    REJECTED = "rejected"
    STALE = "stale"


ACTION_TRANSITIONS: dict[ActionState, set[ActionState]] = {
    ActionState.PREPARED: {ActionState.AUTHORIZED, ActionState.REJECTED},
    ActionState.AUTHORIZED: {ActionState.DISPATCHED, ActionState.REJECTED, ActionState.STALE},
    ActionState.DISPATCHED: {ActionState.VERIFIED, ActionState.OUTCOME_UNKNOWN, ActionState.FAILED},
    ActionState.OUTCOME_UNKNOWN: {ActionState.RECONCILING},
    ActionState.RECONCILING: {ActionState.VERIFIED, ActionState.FAILED, ActionState.DISPATCHED},
    ActionState.STALE: {ActionState.AUTHORIZED, ActionState.REJECTED},
    ActionState.FAILED: set(),
    ActionState.VERIFIED: set(),
    ActionState.REJECTED: set(),
}


class InvalidActionTransition(Exception):
    pass


class ActionEvent(BaseModel):
    from_state: ActionState
    to_state: ActionState
    actor: str
    note: str = ""
    at: datetime = Field(default_factory=_now)


class ActionRecord(BaseModel):
    operation_id: str
    incident_id: str
    plan_step: str
    target: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    policy_version: str = "unversioned"
    preconditions: list[str] = Field(default_factory=list)
    state: ActionState = ActionState.PREPARED
    authorized_by: str | None = None
    authorized_at_incident_version: int | None = None
    dispatch_attempts: int = 0
    last_dispatched_at: datetime | None = None
    verification: dict[str, Any] = Field(default_factory=dict)
    history: list[ActionEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)

    @staticmethod
    def make_operation_id(incident_id: str, plan_step: str, target: str, parameters: dict[str, Any]) -> str:
        canonical = json.dumps(
            {"i": incident_id, "s": plan_step, "t": target, "p": parameters},
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return "OP-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]

    def _move(self, to: ActionState, actor: str, note: str = "") -> None:
        if to not in ACTION_TRANSITIONS[self.state]:
            raise InvalidActionTransition(
                f"{self.operation_id}: {self.state.value} → {to.value} is not permitted"
            )
        self.history.append(ActionEvent(from_state=self.state, to_state=to, actor=actor, note=note))
        self.state = to


class ActionLedger:
    """Durable record of every remediation operation and its outcome."""

    def __init__(self, max_staleness_versions: int = 0) -> None:
        """
        max_staleness_versions: how many incident versions may pass between authorization and
        dispatch before the approval must be revalidated. 0 means any change makes it stale.
        """
        self._records: dict[str, ActionRecord] = {}
        self.max_staleness_versions = max_staleness_versions

    # ----------------------------------------------------------------- lifecycle

    def prepare(
        self,
        incident_id: str,
        plan_step: str,
        target: str,
        parameters: dict[str, Any] | None = None,
        policy_version: str = "unversioned",
        preconditions: list[str] | None = None,
    ) -> ActionRecord:
        """Create (or return the existing) operation record. Same plan step → same operation id."""
        parameters = parameters or {}
        op_id = ActionRecord.make_operation_id(incident_id, plan_step, target, parameters)
        if op_id in self._records:
            return self._records[op_id]
        record = ActionRecord(
            operation_id=op_id,
            incident_id=incident_id,
            plan_step=plan_step,
            target=target,
            parameters=parameters,
            policy_version=policy_version,
            preconditions=preconditions or [],
        )
        self._records[op_id] = record
        return record

    def authorize(self, operation_id: str, actor: str, incident_version: int, note: str = "") -> ActionRecord:
        """Bind an explicit approval to the exact target, parameters and incident version."""
        record = self.get(operation_id)
        record._move(ActionState.AUTHORIZED, actor, note or "approved")
        record.authorized_by = actor
        record.authorized_at_incident_version = incident_version
        return record

    def reject(self, operation_id: str, actor: str, note: str = "") -> ActionRecord:
        record = self.get(operation_id)
        record._move(ActionState.REJECTED, actor, note)
        return record

    def revalidate(self, operation_id: str, current_incident_version: int, actor: str = "system") -> ActionRecord:
        """Before dispatch, confirm the approval is not stale relative to the incident's evidence."""
        record = self.get(operation_id)
        if record.state is not ActionState.AUTHORIZED:
            return record
        drift = current_incident_version - (record.authorized_at_incident_version or 0)
        if drift > self.max_staleness_versions:
            record._move(
                ActionState.STALE,
                actor,
                f"incident moved {drift} version(s) since authorization; re-approval required",
            )
        return record

    def dispatch(self, operation_id: str, actor: str = "executor") -> ActionRecord:
        """Dispatch (or re-dispatch) using the same operation id. Never mint a new id for a retry."""
        record = self.get(operation_id)
        if record.state is ActionState.RECONCILING:
            record._move(ActionState.DISPATCHED, actor, "retry after reconciliation confirmed no effect")
        else:
            record._move(ActionState.DISPATCHED, actor, "dispatched")
        record.dispatch_attempts += 1
        record.last_dispatched_at = _now()
        return record

    def mark_outcome_unknown(self, operation_id: str, actor: str = "executor", note: str = "") -> ActionRecord:
        """The response was lost or timed out. Not a failure, not a success: reconcile."""
        record = self.get(operation_id)
        record._move(ActionState.OUTCOME_UNKNOWN, actor, note or "response lost or timed out")
        record._move(ActionState.RECONCILING, "system", "reconciliation started")
        return record

    def reconcile(
        self, operation_id: str, target_state_observed: bool, actor: str = "reconciler", note: str = ""
    ) -> ActionRecord:
        """Resolve an uncertain outcome from the *target's* observed state, not from a local flag."""
        record = self.get(operation_id)
        if record.state is not ActionState.RECONCILING:
            raise InvalidActionTransition(f"{operation_id} is not reconciling")
        if target_state_observed:
            record._move(ActionState.VERIFIED, actor, note or "target state confirms the operation applied")
        else:
            record._move(ActionState.DISPATCHED, actor, note or "target shows no effect; safe to retry once")
            record.dispatch_attempts += 1
            record.last_dispatched_at = _now()
        return record

    def verify(self, operation_id: str, healthy: bool, evidence: dict[str, Any], actor: str = "verifier") -> ActionRecord:
        """Record independent verification of the externally observable outcome."""
        record = self.get(operation_id)
        record.verification = {"healthy": healthy, **evidence}
        if healthy:
            record._move(ActionState.VERIFIED, actor, "health checks passed over observation window")
        else:
            record._move(ActionState.FAILED, actor, "health checks did not confirm recovery")
        return record

    # ----------------------------------------------------------------- accessors

    def get(self, operation_id: str) -> ActionRecord:
        try:
            return self._records[operation_id]
        except KeyError:
            raise KeyError(f"unknown operation {operation_id}") from None

    def for_incident(self, incident_id: str) -> list[ActionRecord]:
        return [r for r in self._records.values() if r.incident_id == incident_id]

    @property
    def records(self) -> list[ActionRecord]:
        return list(self._records.values())

    @property
    def stats(self) -> dict[str, Any]:
        by_state: dict[str, int] = {}
        for r in self._records.values():
            by_state[r.state.value] = by_state.get(r.state.value, 0) + 1
        return {"operations": len(self._records), "by_state": by_state}
