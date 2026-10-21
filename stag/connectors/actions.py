"""Action connectors: the only code that touches a destination on behalf of an operation.

Contract (``ActionConnector``):

* ``supports(record)`` — whether this connector is the destination for the operation's target.
* ``dispatch(record)`` — perform the effect **idempotently on ``record.operation_id``**: a retry of
  the same operation must find the earlier effect (ticket, change) instead of creating a second
  one. Returns an ``ActionReceipt``. Raises ``ConnectorRejected`` for a definite refusal and
  ``ConnectorTimeout`` when the outcome is unknown.
* ``reconcile(record)`` — observe the destination: ``True`` if the effect exists, ``False`` if it
  does not, ``None`` if that cannot be determined right now.

Ticketing adapters key the ticket on the operation id (Jira label, ServiceNow ``correlation_id``)
and search before creating. Credentials are held by the adapter and never serialised.
"""

from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from stag.connectors.base import (
    ConnectorRejected,
    ConnectorTimeout,
    HttpTransport,
    HttpxTransport,
    redact,
)
from stag.techops.actions import ActionRecord

logger = structlog.get_logger()


class ActionReceipt(BaseModel):
    operation_id: str
    connector: str
    status: Literal["applied", "unknown", "rejected"]
    external_id: str | None = None
    external_url: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = False
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ActionConnector(ABC):
    name: str = "connector"
    kind: str = "generic"
    simulated: bool = False

    def __init__(self, *, targets: Iterable[str] | None = None) -> None:
        self.targets = set(targets) if targets else None  # None = any target

    def supports(self, record: ActionRecord) -> bool:
        return self.targets is None or record.target in self.targets

    @abstractmethod
    def dispatch(self, record: ActionRecord) -> ActionReceipt: ...

    @abstractmethod
    def reconcile(self, record: ActionRecord) -> bool | None: ...

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "simulated": self.simulated, "targets": sorted(self.targets) if self.targets else "*"}


def ticket_fields(record: ActionRecord) -> tuple[str, str]:
    """Summary and description for a ticket. Terms only; nothing from telemetry payloads."""
    summary = f"[STAG {record.incident_id}] {record.plan_step} on {record.target}"[:250]
    description = json.dumps(
        {
            "operation_id": record.operation_id,
            "incident_id": record.incident_id,
            "plan_step": record.plan_step,
            "target": record.target,
            "parameters": record.parameters,
            "policy_version": record.policy_version,
            "preconditions": record.preconditions,
            "authorized_by": record.authorized_by,
            "authorized_at_incident_version": record.authorized_at_incident_version,
        },
        indent=2,
        default=str,
    )
    return summary, description


# --------------------------------------------------------------------------- fake (recorded)


class FakeTicketingConnector(ActionConnector):
    """In-memory ticketing destination with failure injection.

    ``fail`` maps an operation id to a mode: ``"reject"`` (definite refusal), ``"timeout"`` (the
    ticket is created but the response is lost, every time), ``"timeout_once"`` (lost once, then
    normal) or ``"unknown"`` (reconciliation cannot observe the destination).
    """

    name = "fake-ticketing"
    kind = "ticketing"
    simulated = True

    def __init__(self, *, targets: Iterable[str] | None = None, fail: dict[str, str] | None = None) -> None:
        super().__init__(targets=targets)
        self.fail = dict(fail or {})
        self.tickets: dict[str, dict[str, Any]] = {}
        self.dispatches: list[str] = []
        self._seq = 0

    def dispatch(self, record: ActionRecord) -> ActionReceipt:
        op = record.operation_id
        self.dispatches.append(op)
        mode = self.fail.get(op)
        if mode == "reject":
            raise ConnectorRejected(f"{self.name}: destination rejected {op} (fixture)")
        if op not in self.tickets:
            self._seq += 1
            summary, description = ticket_fields(record)
            self.tickets[op] = {"id": f"FAKE-{self._seq}", "operation_id": op, "summary": summary, "description": description, "created_at": datetime.now(UTC).isoformat()}
        if mode in ("timeout", "timeout_once"):
            if mode == "timeout_once":
                del self.fail[op]
            raise ConnectorTimeout(f"{self.name}: response lost after creating ticket (fixture)")
        ticket = self.tickets[op]
        return ActionReceipt(operation_id=op, connector=self.name, status="applied", external_id=ticket["id"], simulated=True, detail={"summary": ticket["summary"]})

    def reconcile(self, record: ActionRecord) -> bool | None:
        if self.fail.get(record.operation_id) == "unknown":
            return None
        return record.operation_id in self.tickets


# --------------------------------------------------------------------------- Jira


class JiraConnector(ActionConnector):
    """Jira Cloud REST API v3. The operation id travels as a label; dispatch searches by that label
    before creating an issue, so a retry never duplicates the ticket."""

    name = "jira"
    kind = "ticketing"

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        project_key: str,
        *,
        issue_type: str = "Task",
        transport: HttpTransport | None = None,
        targets: Iterable[str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(targets=targets)
        if not (base_url and email and api_token and project_key):
            raise ValueError("jira connector needs base_url, email, api_token and project_key")
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self.issue_type = issue_type
        self._transport = transport or HttpxTransport(timeout)
        token = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json", "Content-Type": "application/json"}
        self._identity = f"{email} / token {redact(api_token)}"

    @staticmethod
    def label(operation_id: str) -> str:
        return f"stag-op-{operation_id.lower()}"

    def _find(self, operation_id: str) -> dict[str, Any] | None:
        resp = self._transport.request(
            "GET",
            f"{self.base_url}/rest/api/3/search",
            headers=self._headers,
            params={"jql": f'labels = "{self.label(operation_id)}"', "fields": "key,status", "maxResults": 1},
        )
        if resp.status >= 500:
            raise ConnectorTimeout(f"jira search: server error {resp.status}")
        if not resp.ok:
            raise ConnectorRejected(f"jira search: {resp.status} {resp.text()}")
        issues = (resp.body or {}).get("issues") if isinstance(resp.body, dict) else None
        return issues[0] if issues else None

    def dispatch(self, record: ActionRecord) -> ActionReceipt:
        existing = self._find(record.operation_id)
        if existing:
            return self._receipt(record, existing["key"], created=False)
        summary, description = ticket_fields(record)
        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary,
                "issuetype": {"name": self.issue_type},
                "labels": [self.label(record.operation_id)],
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [{"type": "codeBlock", "attrs": {"language": "json"}, "content": [{"type": "text", "text": description}]}],
                },
            }
        }
        resp = self._transport.request("POST", f"{self.base_url}/rest/api/3/issue", headers=self._headers, json=payload)
        if resp.status >= 500:
            raise ConnectorTimeout(f"jira create: server error {resp.status}; outcome unknown")
        if not resp.ok:
            raise ConnectorRejected(f"jira create: {resp.status} {resp.text()}")
        key = resp.body.get("key") if isinstance(resp.body, dict) else None
        if not key:
            raise ConnectorTimeout("jira create: response without issue key; outcome unknown")
        return self._receipt(record, key, created=True)

    def _receipt(self, record: ActionRecord, key: str, *, created: bool) -> ActionReceipt:
        return ActionReceipt(
            operation_id=record.operation_id,
            connector=self.name,
            status="applied",
            external_id=key,
            external_url=f"{self.base_url}/browse/{key}",
            detail={"created": created, "project": self.project_key},
        )

    def reconcile(self, record: ActionRecord) -> bool | None:
        try:
            return self._find(record.operation_id) is not None
        except ConnectorTimeout:
            return None

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "base_url": self.base_url, "project": self.project_key, "identity": self._identity}


# --------------------------------------------------------------------------- ServiceNow


class ServiceNowConnector(ActionConnector):
    """ServiceNow Table API. ``correlation_id`` carries the operation id; dispatch queries it first."""

    name = "servicenow"
    kind = "ticketing"

    def __init__(
        self,
        instance_url: str,
        user: str,
        password: str,
        *,
        table: str = "incident",
        transport: HttpTransport | None = None,
        targets: Iterable[str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        super().__init__(targets=targets)
        if not (instance_url and user and password):
            raise ValueError("servicenow connector needs instance_url, user and password")
        self.instance_url = instance_url.rstrip("/")
        self.table = table
        self._transport = transport or HttpxTransport(timeout)
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Accept": "application/json", "Content-Type": "application/json"}
        self._identity = f"{user} / password {redact(password)}"

    def _find(self, operation_id: str) -> dict[str, Any] | None:
        resp = self._transport.request(
            "GET",
            f"{self.instance_url}/api/now/table/{self.table}",
            headers=self._headers,
            params={"sysparm_query": f"correlation_id={operation_id}", "sysparm_fields": "sys_id,number,state", "sysparm_limit": 1},
        )
        if resp.status >= 500:
            raise ConnectorTimeout(f"servicenow query: server error {resp.status}")
        if not resp.ok:
            raise ConnectorRejected(f"servicenow query: {resp.status} {resp.text()}")
        result = (resp.body or {}).get("result") if isinstance(resp.body, dict) else None
        return result[0] if result else None

    def dispatch(self, record: ActionRecord) -> ActionReceipt:
        existing = self._find(record.operation_id)
        if existing:
            return self._receipt(record, existing, created=False)
        summary, description = ticket_fields(record)
        payload = {"short_description": summary, "description": description, "correlation_id": record.operation_id}
        resp = self._transport.request("POST", f"{self.instance_url}/api/now/table/{self.table}", headers=self._headers, json=payload)
        if resp.status >= 500:
            raise ConnectorTimeout(f"servicenow create: server error {resp.status}; outcome unknown")
        if not resp.ok:
            raise ConnectorRejected(f"servicenow create: {resp.status} {resp.text()}")
        result = resp.body.get("result") if isinstance(resp.body, dict) else None
        if not result or not result.get("sys_id"):
            raise ConnectorTimeout("servicenow create: response without sys_id; outcome unknown")
        return self._receipt(record, result, created=True)

    def _receipt(self, record: ActionRecord, row: dict[str, Any], *, created: bool) -> ActionReceipt:
        return ActionReceipt(
            operation_id=record.operation_id,
            connector=self.name,
            status="applied",
            external_id=row.get("number") or row.get("sys_id"),
            external_url=f"{self.instance_url}/nav_to.do?uri={self.table}.do?sys_id={row.get('sys_id')}",
            detail={"created": created, "table": self.table, "sys_id": row.get("sys_id")},
        )

    def reconcile(self, record: ActionRecord) -> bool | None:
        try:
            return self._find(record.operation_id) is not None
        except ConnectorTimeout:
            return None

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "instance_url": self.instance_url, "table": self.table, "identity": self._identity}
