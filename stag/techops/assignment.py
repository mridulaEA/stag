"""Bounded investigation assignment contract.

An agent can choose *what to investigate next* without choosing *what it is authorized to access
or change*. The assignment names the scope, permitted capabilities, limits and required output;
``BoundedRun`` enforces them outside the model. A schema-valid answer still needs evidence
checks; this module checks the contract, not the truth of the findings.

Example (YAML)::

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
      max_model_calls: 4
      max_tool_calls: 8
      max_runtime_seconds: 120
      budget_profile: standard-investigation
    output:
      required:
        - observed_facts_with_references
        - hypotheses
        - contradictory_or_missing_evidence
        - proposed_next_step
    on_limit_or_uncertainty: return_partial_findings_and_escalate

The numeric limits are examples to test, not universal thresholds.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

READ_ONLY_CAPABILITIES = {
    "query_scoped_logs",
    "read_service_health",
    "retrieve_approved_runbooks",
    "inspect_deployment_metadata",
    "read_metrics",
    "read_traces",
    "read_incident_history",
    "read_dependency_graph",
}

MUTATING_CAPABILITIES = {
    "restart_component",
    "rollback_deployment",
    "scale_service",
    "toggle_feature_flag",
    "apply_config_change",
}


class PermissionDenied(Exception):
    """A capability outside the assignment was requested."""


class LimitExceeded(Exception):
    """A model-call, tool-call or runtime limit was hit."""


class AssignmentScope(BaseModel):
    incident_id: str
    service_ids: list[str] = Field(default_factory=list)
    environment: str = "unknown"
    evidence_snapshot: str


class AssignmentPermissions(BaseModel):
    capabilities: list[str] = Field(default_factory=list)
    production_mutations: bool = False

    def allows(self, capability: str) -> bool:
        if capability not in self.capabilities:
            return False
        if capability in MUTATING_CAPABILITIES and not self.production_mutations:
            return False
        return True


class AssignmentLimits(BaseModel):
    max_model_calls: int = Field(default=4, ge=0)
    max_tool_calls: int = Field(default=8, ge=0)
    max_runtime_seconds: int = Field(default=120, ge=1)
    budget_profile: str = "standard-investigation"


class AssignmentOutput(BaseModel):
    required: list[str] = Field(
        default_factory=lambda: [
            "observed_facts_with_references",
            "hypotheses",
            "contradictory_or_missing_evidence",
            "proposed_next_step",
        ]
    )


class InvestigationAssignment(BaseModel):
    task: Literal["investigate_incident", "triage_incident", "verify_recovery", "draft_postmortem"] = (
        "investigate_incident"
    )
    scope: AssignmentScope
    permissions: AssignmentPermissions = Field(default_factory=AssignmentPermissions)
    limits: AssignmentLimits = Field(default_factory=AssignmentLimits)
    output: AssignmentOutput = Field(default_factory=AssignmentOutput)
    on_limit_or_uncertainty: Literal[
        "return_partial_findings_and_escalate", "return_partial_findings", "fail"
    ] = "return_partial_findings_and_escalate"

    @classmethod
    def from_yaml(cls, source: str | Path) -> InvestigationAssignment:
        text = Path(source).read_text() if isinstance(source, Path) else source
        data = yaml.safe_load(text) or {}
        return cls.model_validate(data)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False)

    def start(self) -> BoundedRun:
        return BoundedRun(self)


class Fact(BaseModel):
    statement: str
    evidence_refs: list[str] = Field(min_length=1)


class InvestigationFindings(BaseModel):
    """Required output shape: facts with references, hypotheses, gaps, and the next safe check."""

    observed_facts_with_references: list[Fact] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)
    contradictory_or_missing_evidence: list[str] = Field(default_factory=list)
    proposed_next_step: str = ""
    partial: bool = False
    escalate: bool = False
    evidence_snapshot: str | None = None

    def missing_required(self, assignment: InvestigationAssignment) -> list[str]:
        missing = []
        for field in assignment.output.required:
            value = getattr(self, field, None)
            if value in (None, "", []):
                missing.append(field)
        return missing

    def is_complete_for(self, assignment: InvestigationAssignment) -> bool:
        return not self.missing_required(assignment)


class BoundedRun:
    """Runtime enforcement of an assignment. The executor, not the prompt, enforces the contract."""

    def __init__(self, assignment: InvestigationAssignment) -> None:
        self.assignment = assignment
        self.model_calls = 0
        self.tool_calls = 0
        self.started_at = time.monotonic()
        self.capability_log: list[dict[str, Any]] = []
        self.denied: list[str] = []

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def exhausted(self) -> bool:
        limits = self.assignment.limits
        return (
            self.model_calls >= limits.max_model_calls
            or self.tool_calls >= limits.max_tool_calls
            or self.elapsed_seconds >= limits.max_runtime_seconds
        )

    def check_runtime(self) -> None:
        if self.elapsed_seconds >= self.assignment.limits.max_runtime_seconds:
            raise LimitExceeded("max_runtime_seconds exceeded")

    def record_model_call(self) -> None:
        self.check_runtime()
        if self.model_calls >= self.assignment.limits.max_model_calls:
            raise LimitExceeded("max_model_calls exceeded")
        self.model_calls += 1

    def use_capability(self, capability: str, **params: Any) -> None:
        """Authorize one tool call. Raises PermissionDenied or LimitExceeded; otherwise records it."""
        self.check_runtime()
        if not self.assignment.permissions.allows(capability):
            self.denied.append(capability)
            raise PermissionDenied(
                f"capability '{capability}' is outside assignment for {self.assignment.scope.incident_id}"
            )
        if self.tool_calls >= self.assignment.limits.max_tool_calls:
            raise LimitExceeded("max_tool_calls exceeded")
        self.tool_calls += 1
        self.capability_log.append({"capability": capability, "params": params})

    def finish(self, findings: InvestigationFindings) -> InvestigationFindings:
        """Validate findings against the contract; mark partial/escalate per policy when incomplete."""
        findings.evidence_snapshot = self.assignment.scope.evidence_snapshot
        missing = findings.missing_required(self.assignment)
        if missing or self.exhausted:
            policy = self.assignment.on_limit_or_uncertainty
            if policy == "fail":
                raise LimitExceeded(f"incomplete findings: missing {missing}")
            findings.partial = True
            findings.escalate = policy == "return_partial_findings_and_escalate"
        return findings

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "incident_id": self.assignment.scope.incident_id,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "denied_capabilities": list(self.denied),
            "exhausted": self.exhausted,
        }
