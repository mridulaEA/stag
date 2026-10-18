"""Action ledger: retry-safe remediation with durable operation identity and immutable terms.

    PREPARED → AUTHORIZED → DISPATCHED → APPLIED → VERIFIED
                                ↓            ↓
                         OUTCOME_UNKNOWN   FAILED
                                ↓
                           RECONCILING ──► APPLIED (destination shows the effect; still needs verification)
                                       ──► RETRY_READY (destination proves absence; one explicit retry)
                                       ──► FAILED
    AUTHORIZED / RETRY_READY → STALE when the incident moved since authorization → re-authorize

Rules the ledger enforces rather than documents:

* The operation id covers incident, plan step, target, parameters, policy version and
  preconditions. Changing any approved term is a different operation that needs its own approval.
* Nothing is dispatchable from OUTCOME_UNKNOWN or RECONCILING. Only a destination check that proves
  absence yields RETRY_READY, and the retry budget is enforced.
* ``APPLIED`` (the destination confirms the change happened) is distinct from ``VERIFIED`` (an
  independent health observation over a window). Reconciliation never produces VERIFIED.
* ``verify`` validates the transition before it writes anything; reserved evidence fields cannot be
  overwritten by caller-supplied evidence.
"""

from __future__ import annotations

import copy
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
    APPLIED = "applied"
    VERIFIED = "verified"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECONCILING = "reconciling"
    RETRY_READY = "retry_ready"
    FAILED = "failed"
    REJECTED = "rejected"
    STALE = "stale"


ACTION_TRANSITIONS: dict[ActionState, set[ActionState]] = {
    ActionState.PREPARED: {ActionState.AUTHORIZED, ActionState.REJECTED},
    ActionState.AUTHORIZED: {ActionState.DISPATCHED, ActionState.REJECTED, ActionState.STALE},
    ActionState.DISPATCHED: {ActionState.APPLIED, ActionState.VERIFIED, ActionState.OUTCOME_UNKNOWN, ActionState.FAILED},
    ActionState.APPLIED: {ActionState.VERIFIED, ActionState.FAILED},
    ActionState.OUTCOME_UNKNOWN: {ActionState.RECONCILING},
    ActionState.RECONCILING: {ActionState.APPLIED, ActionState.RETRY_READY, ActionState.FAILED},
    ActionState.RETRY_READY: {ActionState.DISPATCHED, ActionState.REJECTED, ActionState.STALE},
    ActionState.STALE: {ActionState.AUTHORIZED, ActionState.REJECTED},
    ActionState.FAILED: set(),
    ActionState.VERIFIED: set(),
    ActionState.REJECTED: set(),
}

TERMINAL_ACTION_STATES = {ActionState.FAILED, ActionState.VERIFIED, ActionState.REJECTED}
DISPATCHABLE_STATES = {ActionState.AUTHORIZED, ActionState.RETRY_READY}


class InvalidActionTransition(Exception):
    pass


class RetryBudgetExceeded(InvalidActionTransition):
    pass


class IntegrityError(InvalidActionTransition):
    """The record's terms no longer match the terms that were reviewed and approved."""


class VerificationResult(BaseModel):
    """A validated verification claim. Empty or unsubstantiated evidence does not validate.

    ``simulated`` marks scenario fixtures; a live coordinator refuses them.
    """

    healthy: bool | None
    source: str = Field(min_length=1, max_length=200)
    window: str = Field(min_length=1, max_length=200)
    signals: list[str] = Field(min_length=1, max_length=64)
    target: str | None = Field(default=None, max_length=200)
    observed_at: datetime = Field(default_factory=_now)
    simulated: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


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
    terms_hash: str = ""
    retry_budget_extension: int = 0
    state: ActionState = ActionState.PREPARED
    authorized_by: str | None = None
    authorized_at_incident_version: int | None = None
    authorized_at: datetime | None = None
    dispatch_attempts: int = 0
    last_dispatched_at: datetime | None = None
    verification: dict[str, Any] = Field(default_factory=dict)
    history: list[ActionEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_now)

    @staticmethod
    def terms(
        incident_id: str,
        plan_step: str,
        target: str,
        parameters: dict[str, Any],
        policy_version: str,
        preconditions: list[str],
    ) -> str:
        canonical = json.dumps(
            {
                "incident": incident_id,
                "step": plan_step,
                "target": target,
                "parameters": parameters,
                "policy": policy_version,
                "preconditions": sorted(preconditions),
            },
            sort_keys=True,
            default=str,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def make_operation_id(
        incident_id: str,
        plan_step: str,
        target: str,
        parameters: dict[str, Any],
        policy_version: str = "unversioned",
        preconditions: list[str] | None = None,
    ) -> str:
        digest = ActionRecord.terms(incident_id, plan_step, target, parameters, policy_version, preconditions or [])
        return "OP-" + digest[:12]

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_ACTION_STATES

    def _can(self, to: ActionState) -> bool:
        return to in ACTION_TRANSITIONS[self.state]

    def _move(self, to: ActionState, actor: str, note: str = "") -> None:
        if not self._can(to):
            raise InvalidActionTransition(f"{self.operation_id}: {self.state.value} → {to.value} is not permitted")
        self.history.append(ActionEvent(from_state=self.state, to_state=to, actor=actor, note=note))
        self.state = to


class ActionLedger:
    """Durable record of every remediation operation and its outcome (in-memory reference impl)."""

    def __init__(self, max_staleness_versions: int = 0, max_retries: int = 1) -> None:
        """
        max_staleness_versions: incident versions that may pass between authorization and dispatch
            before re-approval is required. 0 means any change makes the approval stale.
        max_retries: explicit retries allowed after a reconciliation proved the effect absent. The
            ceiling is cumulative for the operation: re-approval does not replenish it.
        """
        if int(max_staleness_versions) < 0 or int(max_retries) < 0:
            raise ValueError("max_staleness_versions and max_retries must be >= 0")
        self._records: dict[str, ActionRecord] = {}
        self.max_staleness_versions = int(max_staleness_versions)
        self.max_retries = int(max_retries)

    # ----------------------------------------------------------------- integrity

    @staticmethod
    def _check_integrity(record: ActionRecord) -> None:
        """Recompute the terms digest; any drift means the record was altered outside the ledger."""
        current = ActionRecord.terms(
            record.incident_id, record.plan_step, record.target, record.parameters,
            record.policy_version, record.preconditions,
        )
        if current != record.terms_hash:
            raise IntegrityError(
                f"{record.operation_id}: operation terms were modified after review; "
                "changing target, parameters, policy or preconditions requires a new operation"
            )

    def _attempt_ceiling(self, record: ActionRecord) -> int:
        return 1 + self.max_retries + record.retry_budget_extension

    def extend_retry_budget(self, operation_id: str, by: int, authorized_by: str, note: str = "") -> ActionRecord:
        """Audited extension of one operation's retry allowance. Never implicit."""
        if by < 1 or not authorized_by:
            raise ValueError("a retry-budget extension needs a positive amount and an authorizer")
        record = self._record(operation_id)
        record.retry_budget_extension += int(by)
        record.history.append(
            ActionEvent(from_state=record.state, to_state=record.state, actor=authorized_by,
                        note=note or f"retry budget extended by {by}")
        )
        return record

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
        """Create (or return the existing) operation record for these exact terms."""
        parameters = copy.deepcopy(parameters or {})  # detached from the caller's object
        preconditions = list(preconditions or [])
        terms = ActionRecord.terms(incident_id, plan_step, target, parameters, policy_version, preconditions)
        op_id = "OP-" + terms[:12]
        existing = self._records.get(op_id)
        if existing is not None:
            if existing.terms_hash != terms:  # defensive: id prefix collision with different terms
                raise InvalidActionTransition(f"{op_id}: operation id collides with different terms")
            return existing
        record = ActionRecord(
            operation_id=op_id,
            incident_id=incident_id,
            plan_step=plan_step,
            target=target,
            parameters=parameters,
            policy_version=policy_version,
            preconditions=preconditions,
            terms_hash=terms,
        )
        self._records[op_id] = record
        return record

    def authorize(self, operation_id: str, actor: str, incident_version: int, note: str = "") -> ActionRecord:
        """Bind an explicit approval to the exact terms and the incident evidence version."""
        if not actor:
            raise ValueError("authorization requires an actor")
        record = self._record(operation_id)
        self._check_integrity(record)
        record._move(ActionState.AUTHORIZED, actor, note or "approved")
        record.authorized_by = actor
        record.authorized_at_incident_version = int(incident_version)
        record.authorized_at = _now()
        return record

    def reject(self, operation_id: str, actor: str, note: str = "") -> ActionRecord:
        record = self._record(operation_id)
        record._move(ActionState.REJECTED, actor, note)
        return record

    def revalidate(self, operation_id: str, current_incident_version: int, actor: str = "system") -> ActionRecord:
        """Before dispatch, confirm the approval is not stale relative to the incident's evidence."""
        record = self._record(operation_id)
        if record.state not in DISPATCHABLE_STATES:
            return record
        authorized_at = record.authorized_at_incident_version
        if authorized_at is None or current_incident_version < authorized_at:
            record._move(ActionState.STALE, actor, "approval version is missing or ahead of the incident; re-approval required")
            return record
        drift = current_incident_version - authorized_at
        if drift > self.max_staleness_versions:
            record._move(ActionState.STALE, actor, f"incident moved {drift} version(s) since authorization; re-approval required")
        return record

    def dispatch(self, operation_id: str, actor: str = "executor", current_incident_version: int | None = None) -> ActionRecord:
        """Dispatch from AUTHORIZED, or retry once from RETRY_READY. Never from an uncertain state.

        When ``current_incident_version`` is given, freshness is revalidated first.
        """
        record = self._record(operation_id)
        self._check_integrity(record)  # before any state change or attempt count
        if current_incident_version is not None:
            self.revalidate(operation_id, current_incident_version, actor=actor)
        if record.state not in DISPATCHABLE_STATES:
            raise InvalidActionTransition(
                f"{operation_id}: cannot dispatch from {record.state.value}; "
                "only authorized or retry_ready operations are dispatchable"
            )
        if record.dispatch_attempts >= self._attempt_ceiling(record):
            raise RetryBudgetExceeded(
                f"{operation_id}: cumulative dispatch ceiling ({self._attempt_ceiling(record)}) reached; "
                "re-approval does not replenish it — record an explicit retry-budget extension"
            )
        note = "retry after reconciliation proved the effect absent" if record.state is ActionState.RETRY_READY else "dispatched"
        record._move(ActionState.DISPATCHED, actor, note)
        record.dispatch_attempts += 1
        record.last_dispatched_at = _now()
        return record

    def mark_applied(self, operation_id: str, actor: str = "executor", receipt: dict[str, Any] | None = None) -> ActionRecord:
        """The destination confirmed the change was applied. Not recovery: verification still required."""
        record = self._record(operation_id)
        self._check_integrity(record)
        record._move(ActionState.APPLIED, actor, "destination confirmed application")
        if receipt:
            record.verification["application_receipt"] = receipt
        return record

    def mark_outcome_unknown(self, operation_id: str, actor: str = "executor", note: str = "") -> ActionRecord:
        """The response was lost or timed out. Not a failure, not a success: reconcile."""
        record = self._record(operation_id)
        record._move(ActionState.OUTCOME_UNKNOWN, actor, note or "response lost or timed out")
        record._move(ActionState.RECONCILING, "system", "reconciliation started")
        return record

    def reconcile(
        self, operation_id: str, target_state_observed: bool, actor: str = "reconciler", note: str = ""
    ) -> ActionRecord:
        """Resolve an uncertain outcome from the *target's* observed state, not from a local flag.

        Observed → APPLIED (verification still required). Absent → RETRY_READY; the retry itself is
        a separate, explicit ``dispatch`` that re-checks freshness and the retry budget.
        """
        record = self._record(operation_id)
        if record.state is not ActionState.RECONCILING:
            raise InvalidActionTransition(f"{operation_id} is not reconciling")
        if target_state_observed:
            record._move(ActionState.APPLIED, actor, note or "target state confirms the operation applied")
        else:
            record._move(ActionState.RETRY_READY, actor, note or "target shows no effect; one explicit retry permitted")
        return record

    def verify(
        self,
        operation_id: str,
        healthy: bool | None,
        evidence: VerificationResult | dict[str, Any],
        actor: str = "verifier",
    ) -> VerificationResult:
        """Record independent verification of the externally observable outcome.

        ``evidence`` must validate as a :class:`VerificationResult` (source, window, signals);
        an empty mapping or a bare boolean claim is rejected before anything is written. ``healthy``
        must be a real bool or None; integers are not accepted. The transition is validated before
        any field is written. Returns the validated result.
        """
        if not (healthy is None or type(healthy) is bool):
            raise ValueError("healthy must be True, False or None (unknown); integers are not verdicts")
        result = evidence if isinstance(evidence, VerificationResult) else VerificationResult(
            **{**dict(evidence), "healthy": healthy}
        )
        if result.healthy is not healthy:
            result = result.model_copy(update={"healthy": healthy})
        record = self._record(operation_id)
        self._check_integrity(record)
        if healthy is None:
            if record.is_terminal:
                raise InvalidActionTransition(f"{operation_id} is terminal ({record.state.value})")
            attempts = record.verification.setdefault("unknown_verdicts", [])
            attempts.append(result.model_dump(mode="json"))
            return result
        target = ActionState.VERIFIED if healthy else ActionState.FAILED
        if not record._can(target):
            raise InvalidActionTransition(f"{operation_id}: {record.state.value} → {target.value} is not permitted")
        record.verification = {
            **{k: v for k, v in record.verification.items() if k in ("application_receipt", "unknown_verdicts")},
            **result.model_dump(mode="json"),
            "healthy": bool(healthy),
            "verified_at": _now().isoformat(),
            "verified_by": actor,
        }
        record._move(target, actor, "health checks passed over observation window" if healthy else "health checks did not confirm recovery")
        return result

    # ----------------------------------------------------------------- accessors

    def _record(self, operation_id: str) -> ActionRecord:
        try:
            return self._records[operation_id]
        except KeyError:
            raise KeyError(f"unknown operation {operation_id}") from None

    def get(self, operation_id: str) -> ActionRecord:
        """The live record. Mutating it outside ledger commands breaks its integrity digest and
        every later command (authorize, dispatch, verify) refuses it."""
        return self._record(operation_id)

    def view(self, operation_id: str) -> ActionRecord:
        """A detached, read-only copy for API responses and reports."""
        return self._record(operation_id).model_copy(deep=True)

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
