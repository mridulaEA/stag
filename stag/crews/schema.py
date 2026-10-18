"""Crew definition schema.

A *crew* is a declarative composition of roles (agents + skills + permissions) and a workflow
of steps that run against the STAG TechOps backbone. Crews are stored as ``crews/<name>/crew.yaml``
and executed by :mod:`stag.crews.runner`.

The schema deliberately mirrors the backbone: steps map onto incident transitions, bounded
investigations, ledger operations and verifications, so a crew cannot express "the agent decides
to restart the service" — only "propose a restart that a human (or a policy) authorizes".
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from stag.models.schemas import EventType, Severity
from stag.techops.assignment import KNOWN_CAPABILITIES
from stag.techops.incident import IncidentState

MAX_SCENARIO_EVENTS = 500
MAX_EVENT_REPEAT = 1000
MAX_WORKFLOW_STEPS = 64
POLICY_SEVERITY_CEILING = ("low", "medium")  # policy approval is never allowed above medium


class Verdict(str, Enum):
    """Typed recovery verdict. Anything else is a validation error, never 'healthy' by accident."""

    HEALTHY = "healthy"
    NOT_RECOVERED = "not_recovered"
    UNKNOWN = "unknown"


class StepAction(str, Enum):
    COORDINATE = "coordinate"  # implicit: dedup + correlate + material change (always first)
    TRANSITION = "transition"  # explicit incident lifecycle transition
    INVESTIGATE = "investigate"  # bounded investigation under an assignment contract
    PROPOSE_ACTION = "propose_action"  # ActionLedger.prepare
    AUTHORIZE = "authorize"  # human or policy approval → ActionLedger.authorize
    DISPATCH = "dispatch"  # revalidate + ActionLedger.dispatch
    VERIFY = "verify"  # independent verification → ActionLedger.verify
    NOTIFY = "notify"  # record an outbound communication (ticket, status page, customer)
    RECORD = "record"  # produce a reviewed artifact draft (postmortem, problem record, release note)


class TriggerSpec(BaseModel):
    type: str
    name: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class SignalKeySpec(BaseModel):
    """Where the coordinator finds service / environment in an event (dotted paths)."""

    service: str = "metadata.service"
    environment: str = "metadata.environment"
    symptom_from_rule: bool = True  # False → one episode per service regardless of which rule fired
    failure_domain_from_event_type: bool = True  # False → ignore event type when grouping


class RolePermissions(BaseModel):
    capabilities: list[str] = Field(default_factory=list)
    production_mutations: bool = False

    @field_validator("capabilities")
    @classmethod
    def _known_capabilities(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - KNOWN_CAPABILITIES)
        if unknown:
            raise ValueError(f"capabilities not in the approved catalog: {unknown}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate capabilities")
        return v


class Role(BaseModel):
    id: str
    title: str
    agent: str | None = None  # agents/<agent>.md (Claude Code subagent) if any
    skills: list[str] = Field(default_factory=list)
    permissions: RolePermissions = Field(default_factory=RolePermissions)
    human: bool = False  # a human role (approver, on-call) — never automated
    description: str = ""


class AuthorizePolicy(BaseModel):
    """Who may authorize an action. ``human`` always requires a recorded human approval.

    ``policy`` mode may approve automatically only when the incident severity is at or below
    ``auto_if_severity_at_most``, which is capped at ``medium`` by the schema and re-checked at
    runtime. Everything else is HELD until the named human role approves.
    """

    mode: Literal["human", "policy"] = "human"
    auto_if_severity_at_most: Literal["low", "medium"] | None = None
    approver_role: str | None = None

    @model_validator(mode="after")
    def _policy_needs_ceiling(self) -> AuthorizePolicy:
        if self.mode == "policy" and self.auto_if_severity_at_most is None:
            raise ValueError("policy authorization requires auto_if_severity_at_most (low or medium)")
        if self.mode == "human" and self.auto_if_severity_at_most is not None:
            raise ValueError("human authorization cannot carry an auto-approval ceiling")
        return self


class AssignmentSpec(BaseModel):
    """Per-step narrowing of the role's capabilities and limits. It can only narrow, never widen."""

    capabilities: list[str] = Field(default_factory=list)
    max_model_calls: int = Field(default=4, ge=0, le=64)
    max_tool_calls: int = Field(default=8, ge=0, le=256)
    max_runtime_seconds: int = Field(default=120, ge=1, le=3600)
    budget_profile: str = "standard-investigation"

    @field_validator("capabilities")
    @classmethod
    def _known_capabilities(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - KNOWN_CAPABILITIES)
        if unknown:
            raise ValueError(f"capabilities not in the approved catalog: {unknown}")
        return v


class Step(BaseModel):
    step: str
    action: StepAction
    role: str | None = None
    description: str = ""
    # transition
    to: IncidentState | None = None
    # investigate
    assignment: AssignmentSpec | None = None
    # propose_action
    plan_step: str | None = None
    target_from: str = "metadata.service"
    parameters: dict[str, Any] = Field(default_factory=dict)
    policy_version: str = "unversioned"
    preconditions: list[str] = Field(default_factory=list)
    # authorize
    policy: AuthorizePolicy | None = None
    # notify / record
    channel: str | None = None
    template: str | None = None
    artifact: str | None = None
    # control flow (verify steps): where to continue on each verdict; default is the next step
    on_recovered: str | None = None
    on_not_recovered: str | None = None

    @model_validator(mode="after")
    def _check_required_fields(self) -> Step:
        if self.action is StepAction.TRANSITION and self.to is None:
            raise ValueError(f"step '{self.step}': transition requires 'to'")
        if self.action is StepAction.PROPOSE_ACTION and not self.plan_step:
            raise ValueError(f"step '{self.step}': propose_action requires 'plan_step'")
        if self.action is StepAction.RECORD and not self.artifact:
            raise ValueError(f"step '{self.step}': record requires 'artifact'")
        if self.action is StepAction.NOTIFY and not self.channel:
            raise ValueError(f"step '{self.step}': notify requires 'channel'")
        return self


class Kpi(BaseModel):
    name: str
    description: str = ""
    target: str | None = None


class Crew(BaseModel):
    name: str
    display_name: str
    domain: str
    purpose: str
    when_to_use: str = ""
    version: str = "1.0.0"
    stream: dict[str, Any] = Field(default_factory=dict)
    signal_key: SignalKeySpec = Field(default_factory=SignalKeySpec)
    triggers: list[TriggerSpec] = Field(default_factory=list)
    coordinator: dict[str, Any] = Field(default_factory=dict)  # IncidentCoordinator kwargs
    roles: list[Role]
    workflow: list[Step]
    outputs: list[str] = Field(default_factory=list)
    kpis: list[Kpi] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    budget_allocation: float = 100.0
    path: Path | None = Field(default=None, exclude=True)

    @field_validator("name")
    @classmethod
    def _name_format(cls, v: str) -> str:
        import re

        if not re.match(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", v):
            raise ValueError("crew name must be lowercase letters, digits and hyphens")
        return v

    @model_validator(mode="after")
    def _check_roles_and_steps(self) -> Crew:
        role_ids = [r.id for r in self.roles]
        if len(role_ids) != len(set(role_ids)):
            raise ValueError("role ids must be unique")
        roles = {r.id: r for r in self.roles}
        for r in self.roles:
            if not r.human and r.permissions.production_mutations:
                raise ValueError(f"role '{r.id}': automated roles may not hold production_mutations")
            if r.human and r.agent:
                raise ValueError(f"role '{r.id}': a human role cannot also be an agent")
        if not any(r.human for r in self.roles):
            raise ValueError("every crew needs at least one human role")

        step_names = [s.step for s in self.workflow]
        if len(step_names) != len(set(step_names)):
            raise ValueError("workflow step names must be unique")
        if len(self.workflow) > MAX_WORKFLOW_STEPS:
            raise ValueError(f"workflow has more than {MAX_WORKFLOW_STEPS} steps")
        if not self.workflow or self.workflow[0].action is not StepAction.COORDINATE:
            raise ValueError("workflow must start with a 'coordinate' step")
        if any(s.action is StepAction.COORDINATE for s in self.workflow[1:]):
            raise ValueError("'coordinate' may only appear as the first step")

        for s in self.workflow:
            if s.role and s.role not in roles:
                raise ValueError(f"step '{s.step}' references unknown role '{s.role}'")
            if s.action is not StepAction.VERIFY and (s.on_recovered or s.on_not_recovered):
                raise ValueError(f"step '{s.step}': only verify steps may define jumps")
            for target in (s.on_recovered, s.on_not_recovered):
                if target and target not in step_names:
                    raise ValueError(f"step '{s.step}' jumps to unknown step '{target}'")
            if s.action is StepAction.INVESTIGATE and s.assignment and s.assignment.capabilities and s.role:
                allowed = set(roles[s.role].permissions.capabilities)
                widened = sorted(set(s.assignment.capabilities) - allowed)
                if widened:
                    raise ValueError(f"step '{s.step}' widens role '{s.role}' capabilities with {widened}")
            if s.action is StepAction.AUTHORIZE:
                policy = s.policy or AuthorizePolicy()
                approver = policy.approver_role or s.role
                if not approver or approver not in roles or not roles[approver].human:
                    raise ValueError(f"step '{s.step}': authorization needs a human role as approver")
                if s.role and s.role != approver and not roles[s.role].human:
                    raise ValueError(f"step '{s.step}': an automated role cannot own an authorize step")
                if policy.auto_if_severity_at_most not in (None, *POLICY_SEVERITY_CEILING):
                    raise ValueError(f"step '{s.step}': policy ceiling above medium is not permitted")

        has_dispatch = any(s.action is StepAction.DISPATCH for s in self.workflow)
        has_authorize = any(s.action is StepAction.AUTHORIZE for s in self.workflow)
        has_propose = any(s.action is StepAction.PROPOSE_ACTION for s in self.workflow)
        has_verify = any(s.action is StepAction.VERIFY for s in self.workflow)
        if has_dispatch and not has_authorize:
            raise ValueError("a crew that dispatches actions must have an authorize step")
        if has_dispatch and not has_verify:
            raise ValueError("a crew that dispatches actions must have a verify step")
        if has_authorize and not has_propose:
            raise ValueError("authorize requires a propose_action step")
        # every dispatch must be preceded (in list order) by an authorize, and every resolve by a verify
        seen_auth = seen_verify = False
        for s in self.workflow:
            seen_auth |= s.action is StepAction.AUTHORIZE
            seen_verify |= s.action is StepAction.VERIFY
            if s.action is StepAction.DISPATCH and not seen_auth:
                raise ValueError(f"step '{s.step}': dispatch before any authorize step")
            if s.action is StepAction.TRANSITION and s.to is IncidentState.RESOLVED and not seen_verify:
                raise ValueError(f"step '{s.step}': resolve before any verify step")
        return self

    def role(self, role_id: str) -> Role:
        return next(r for r in self.roles if r.id == role_id)

    @property
    def skills(self) -> list[str]:
        seen: list[str] = []
        for r in self.roles:
            for s in r.skills:
                if s not in seen:
                    seen.append(s)
        return seen

    @property
    def agents(self) -> list[str]:
        return [r.agent for r in self.roles if r.agent]

    @classmethod
    def from_yaml(cls, source: str | Path) -> Crew:
        if isinstance(source, Path):
            data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
            crew = cls.model_validate(data)
            crew.path = source.parent
            return crew
        return cls.model_validate(yaml.safe_load(source) or {})


class ScenarioEvent(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    event_type: EventType
    severity: Severity = Severity.LOW
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    id: str | None = Field(default=None, max_length=200)  # set to replay the same identity (redelivery test)
    repeat: int = Field(default=1, ge=1, le=MAX_EVENT_REPEAT)


class Scenario(BaseModel):
    """Simulated input for a crew run. Verification verdicts are *fixtures*, labelled as such."""

    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    events: list[ScenarioEvent] = Field(min_length=1, max_length=MAX_SCENARIO_EVENTS)
    verification: dict[str, Verdict] = Field(default_factory=dict)  # "default" or signal_key[#attempt] → verdict
    expect: dict[str, Any] = Field(default_factory=dict)  # assertions for e2e checks

    @model_validator(mode="after")
    def _bounded_expansion(self) -> Scenario:
        if sum(e.repeat for e in self.events) > MAX_SCENARIO_EVENTS * 10:
            raise ValueError("scenario expands to too many events")
        return self

    @classmethod
    def from_yaml(cls, source: str | Path) -> Scenario:
        text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
        return cls.model_validate(yaml.safe_load(text) or {})
