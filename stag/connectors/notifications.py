"""Notification connectors with delivery receipts.

A ``Notification`` has a deterministic id derived from ``(channel, dedup_key)``; the outbox uses it
to guarantee that the same message is never delivered twice (CLAUDE.md: no duplicate deliveries).
Adapters carry their own dedup where the destination supports it (PagerDuty ``dedup_key``, email
``Message-ID``). Credentials live in the adapter and never in receipts.
"""

from __future__ import annotations

import hashlib
import smtplib
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any, Literal, Protocol

import structlog
from pydantic import BaseModel, Field

from stag.connectors.base import (
    ConnectorError,
    ConnectorRejected,
    ConnectorTimeout,
    HttpTransport,
    HttpxTransport,
    redact,
)

logger = structlog.get_logger()

SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


class Notification(BaseModel):
    channel: str
    incident_id: str
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(default="", max_length=20000)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    dedup_key: str = Field(min_length=1, max_length=300)
    operation_ids: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def id(self) -> str:
        digest = hashlib.sha256(f"{self.channel}|{self.dedup_key}".encode()).hexdigest()[:16]
        return f"NTF-{digest}"


class DeliveryReceipt(BaseModel):
    notification_id: str
    channel: str
    status: Literal["queued", "delivered", "duplicate", "failed", "dead"]
    external_id: str | None = None
    attempts: int = 0
    error: str | None = None
    simulated: bool = False
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class NotificationConnector(ABC):
    channel: str = "channel"
    simulated: bool = False

    @abstractmethod
    def deliver(self, notification: Notification) -> DeliveryReceipt:
        """Deliver once. Raise ``ConnectorRejected`` for a permanent refusal, any other
        ``ConnectorError`` for a retryable failure."""

    def describe(self) -> dict[str, Any]:
        return {"channel": self.channel, "simulated": self.simulated}

    def _ok(self, notification: Notification, external_id: str | None = None) -> DeliveryReceipt:
        return DeliveryReceipt(notification_id=notification.id, channel=self.channel, status="delivered", external_id=external_id, simulated=self.simulated)


# --------------------------------------------------------------------------- fake and dashboard


class FakeNotificationConnector(NotificationConnector):
    """Records deliveries. ``fail_times`` makes the first N deliveries raise a retryable error;
    ``reject`` makes every delivery a permanent refusal."""

    simulated = True

    def __init__(self, channel: str = "fake", *, fail_times: int = 0, reject: bool = False) -> None:
        self.channel = channel
        self.fail_times = fail_times
        self.reject = reject
        self.delivered: list[Notification] = []
        self.attempts = 0

    def deliver(self, notification: Notification) -> DeliveryReceipt:
        self.attempts += 1
        if self.reject:
            raise ConnectorRejected(f"{self.channel}: rejected (fixture)")
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectorTimeout(f"{self.channel}: transient failure (fixture)")
        self.delivered.append(notification)
        return self._ok(notification, external_id=f"{self.channel}-{len(self.delivered)}")


class DashboardConnector(NotificationConnector):
    """In-process channel: keeps recent messages and hands new ones to a sink (the WebSocket
    broadcaster) when asked, so delivery works from any thread."""

    channel = "dashboard"

    def __init__(self, *, keep: int = 200, sink: Callable[[dict[str, Any]], Any] | None = None) -> None:
        self.messages: deque[dict[str, Any]] = deque(maxlen=keep)
        self._pending: deque[dict[str, Any]] = deque()
        self.sink = sink

    def deliver(self, notification: Notification) -> DeliveryReceipt:
        message = {"id": notification.id, **notification.model_dump(mode="json")}
        self.messages.append(message)
        if self.sink is not None:
            self.sink(message)
        else:
            self._pending.append(message)
        return self._ok(notification, external_id=notification.id)

    def pop_pending(self) -> list[dict[str, Any]]:
        out = list(self._pending)
        self._pending.clear()
        return out


# --------------------------------------------------------------------------- PagerDuty


class PagerDutyConnector(NotificationConnector):
    """PagerDuty Events API v2 (``trigger``). ``dedup_key`` is the notification's dedup key, so a
    re-page of the same incident/severity de-duplicates on PagerDuty's side as well."""

    channel = "pagerduty"
    URL = "https://events.pagerduty.com/v2/enqueue"
    SEVERITY = {"low": "info", "medium": "warning", "high": "error", "critical": "critical"}

    def __init__(self, routing_key: str, *, transport: HttpTransport | None = None, source: str = "stag", timeout: float = 10.0) -> None:
        if not routing_key:
            raise ValueError("pagerduty connector needs a routing key")
        self._routing_key = routing_key
        self._transport = transport or HttpxTransport(timeout)
        self.source = source

    def deliver(self, notification: Notification) -> DeliveryReceipt:
        payload = {
            "routing_key": self._routing_key,
            "event_action": "trigger",
            "dedup_key": notification.dedup_key[:255],
            "payload": {
                "summary": notification.title[:1024],
                "source": self.source,
                "severity": self.SEVERITY[notification.severity],
                "custom_details": {"incident_id": notification.incident_id, "operation_ids": notification.operation_ids, "body": notification.body[:4000], "provenance": notification.provenance},
            },
        }
        resp = self._transport.request("POST", self.URL, headers={"Content-Type": "application/json"}, json=payload)
        if resp.status in (400, 401, 403):
            raise ConnectorRejected(f"pagerduty: {resp.status} {resp.text()}")
        if not resp.ok:
            raise ConnectorTimeout(f"pagerduty: {resp.status}")
        external = resp.body.get("dedup_key") if isinstance(resp.body, dict) else None
        return self._ok(notification, external_id=external or notification.dedup_key)

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "routing_key": redact(self._routing_key)}


# --------------------------------------------------------------------------- Slack


class SlackConnector(NotificationConnector):
    """Slack incoming webhook."""

    channel = "slack"

    def __init__(self, webhook_url: str, *, transport: HttpTransport | None = None, timeout: float = 10.0) -> None:
        if not webhook_url:
            raise ValueError("slack connector needs a webhook url")
        self._webhook_url = webhook_url
        self._transport = transport or HttpxTransport(timeout)

    def deliver(self, notification: Notification) -> DeliveryReceipt:
        text = f"*[{notification.severity.upper()}] {notification.title}*\n{notification.body}".strip()
        payload = {"text": text[:39000], "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}, {"type": "context", "elements": [{"type": "mrkdwn", "text": f"incident `{notification.incident_id}` · {notification.id}"}]}]}
        resp = self._transport.request("POST", self._webhook_url, headers={"Content-Type": "application/json"}, json=payload)
        if resp.status in (400, 403, 404, 410):
            raise ConnectorRejected(f"slack: {resp.status} {resp.text()}")
        if not resp.ok:
            raise ConnectorTimeout(f"slack: {resp.status}")
        return self._ok(notification, external_id=notification.id)

    def describe(self) -> dict[str, Any]:
        from urllib.parse import urlparse

        # the whole path of an incoming-webhook URL is the credential: show only the host
        return {**super().describe(), "webhook_host": urlparse(self._webhook_url).netloc}


# --------------------------------------------------------------------------- email


class SmtpTransport(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class SmtplibTransport:
    def __init__(self, host: str, port: int, *, user: str = "", password: str = "", starttls: bool = True, timeout: float = 15.0) -> None:
        self.host, self.port, self.user, self._password, self.starttls, self.timeout = host, port, user, password, starttls, timeout

    def send(self, message: EmailMessage) -> None:
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                if self.starttls:
                    smtp.starttls()
                if self.user:
                    smtp.login(self.user, self._password)
                smtp.send_message(message)
        except smtplib.SMTPRecipientsRefused as e:
            raise ConnectorRejected(f"smtp: recipients refused ({len(e.recipients)})") from e
        except (smtplib.SMTPException, OSError) as e:
            raise ConnectorTimeout(f"smtp: {type(e).__name__}") from e


class RecordedSmtp:
    """Test double: records messages; ``fail_times`` raises a retryable error first."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.messages: list[EmailMessage] = []
        self.fail_times = fail_times

    def send(self, message: EmailMessage) -> None:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectorTimeout("smtp: connection reset (fixture)")
        self.messages.append(message)


class EmailConnector(NotificationConnector):
    channel = "email"

    def __init__(self, sender: str, recipients: list[str], transport: SmtpTransport, *, domain: str = "stag.local") -> None:
        if not (sender and recipients):
            raise ValueError("email connector needs a sender and at least one recipient")
        self.sender = sender
        self.recipients = list(recipients)
        self._transport = transport
        self.domain = domain

    def deliver(self, notification: Notification) -> DeliveryReceipt:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = ", ".join(self.recipients)
        message["Subject"] = f"[STAG {notification.severity}] {notification.title}"[:200]
        message["Message-ID"] = f"<{notification.id}@{self.domain}>"
        message["X-STAG-Incident"] = notification.incident_id
        message.set_content(f"{notification.body}\n\nincident: {notification.incident_id}\noperations: {', '.join(notification.operation_ids) or '-'}\nnotification: {notification.id}\n")
        try:
            self._transport.send(message)
        except ConnectorError:
            raise
        except Exception as e:
            raise ConnectorTimeout(f"smtp: {type(e).__name__}") from e
        return self._ok(notification, external_id=message["Message-ID"])

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "from": self.sender, "to": len(self.recipients)}
