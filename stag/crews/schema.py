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

from stag.techops.incident import IncidentState


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

    ``auto_if_severity_at_most`` allows policy approval for low-risk incidents only; everything
    above it is HELD until a human approves.
    """

    mode: Literal["human", "policy"] = "human"
    auto_if_severity_at_most: Literal["low", "medium", "high", "critical"] | None = None
    approver_role: str | None = None


class AssignmentSpec(BaseModel):
    capabilities: list[str] = Field(default_factory=list)
    max_model_calls: int = 4
    max_tool_calls: int = 8
    max_runtime_seconds: int = 120
    budget_profile: str = "standard-investigation"


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
        role_ids = {r.id for r in self.roles}
        step_names = [s.step for s in self.workflow]
        if len(step_names) != len(set(step_names)):
            raise ValueError("workflow step names must be unique")
        if not self.workflow or self.workflow[0].action is not StepAction.COORDINATE:
            raise ValueError("workflow must start with a 'coordinate' step")
        for s in self.workflow:
            if s.role and s.role not in role_ids:
                raise ValueError(f"step '{s.step}' references unknown role '{s.role}'")
            for target in (s.on_recovered, s.on_not_recovered):
                if target and target not in step_names:
                    raise ValueError(f"step '{s.step}' jumps to unknown step '{target}'")
            if s.action is StepAction.AUTHORIZE:
                policy = s.policy or AuthorizePolicy()
                if policy.mode == "human":
                    approver = policy.approver_role or s.role
                    if not approver or not next((r for r in self.roles if r.id == approver), None).human:
                        raise ValueError(
                            f"step '{s.step}': human authorization needs a human role as approver"
                        )
        has_dispatch = any(s.action is StepAction.DISPATCH for s in self.workflow)
        has_authorize = any(s.action is StepAction.AUTHORIZE for s in self.workflow)
        if has_dispatch and not has_authorize:
            raise ValueError("a crew that dispatches actions must have an authorize step")
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
    source: str
    event_type: str
    severity: str = "low"
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None  # set to replay the same identity (redelivery test)
    repeat: int = 1


class Scenario(BaseModel):
    name: str
    description: str = ""
    events: list[ScenarioEvent]
    verification: dict[str, str] = Field(default_factory=dict)  # signal_key or "default" → healthy|not_recovered|unknown
    expect: dict[str, Any] = Field(default_factory=dict)  # assertions for e2e checks

    @classmethod
    def from_yaml(cls, source: str | Path) -> Scenario:
        text = source.read_text(encoding="utf-8") if isinstance(source, Path) else source
        return cls.model_validate(yaml.safe_load(text) or {})
