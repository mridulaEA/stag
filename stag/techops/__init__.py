"""STAG TechOps: the deterministic operational backbone around bounded agentic investigation.

This package implements the controls described in the STAG TechOps workflow series:

* **Identity** — observation, signal-grouping, incident and action identity are kept separate.
* **Incident lifecycle** — a versioned state machine; an agent message never changes state directly.
* **Coordinator** — duplicate delivery is recognised without inflating counts, repeated symptoms
  update the right incident episode, and only *material* changes create investigation requests.
* **Assignment contract** — a bounded investigation task (capabilities, limits, required output)
  that the executor enforces outside the model.
* **Action ledger** — durable operation identity so a retried remediation is never executed twice,
  with explicit OUTCOME_UNKNOWN → RECONCILING handling.
* **Budget ledger** — reserve-before-dispatch, settle-afterward admission control.

Continuous monitoring does not require continuous LLM calls: the monitoring path updates
evidence; the investigation path runs only when a material change needs interpretation.
"""

from stag.techops.actions import (
    ActionLedger,
    ActionRecord,
    ActionState,
    IntegrityError,
    InvalidActionTransition,
    RetryBudgetExceeded,
    VerificationResult,
)
from stag.techops.assignment import (
    AssignmentLimits,
    AssignmentPermissions,
    AssignmentScope,
    BoundedRun,
    InvestigationAssignment,
    InvestigationFindings,
    LimitExceeded,
    PermissionDenied,
)
from stag.techops.budget import BudgetExhausted, BudgetLedger, Reservation
from stag.techops.coordinator import (
    CoordinatorDecision,
    IncidentCoordinator,
    InvestigationRequest,
    Observation,
    RegistrationResult,
)
from stag.techops.identity import ObservationIdentity, SignalKey, envelope_hash, payload_hash
from stag.techops.incident import (
    Incident,
    IncidentState,
    InvalidTransition,
    MaterialChange,
    Transition,
    VerificationRecord,
)

__all__ = [
    "ObservationIdentity",
    "SignalKey",
    "payload_hash",
    "Incident",
    "IncidentState",
    "InvalidTransition",
    "MaterialChange",
    "Transition",
    "CoordinatorDecision",
    "IncidentCoordinator",
    "InvestigationRequest",
    "Observation",
    "AssignmentLimits",
    "AssignmentPermissions",
    "AssignmentScope",
    "BoundedRun",
    "InvestigationAssignment",
    "InvestigationFindings",
    "LimitExceeded",
    "PermissionDenied",
    "ActionLedger",
    "ActionRecord",
    "ActionState",
    "InvalidActionTransition",
    "IntegrityError",
    "RetryBudgetExceeded",
    "VerificationResult",
    "VerificationRecord",
    "RegistrationResult",
    "envelope_hash",
    "BudgetExhausted",
    "BudgetLedger",
    "Reservation",
]
