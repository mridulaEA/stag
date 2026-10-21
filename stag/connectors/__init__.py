"""Connector contracts: how STAG reaches destinations (ticketing, Kubernetes, webhooks) and people
(dashboard, PagerDuty, Slack, email) without those destinations ever reaching into the backbone.

* ``actions``       — ``ActionConnector`` (dispatch / reconcile) and the first adapters: a recorded
                      fake, Jira and ServiceNow. Idempotent on ``operation_id``.
* ``dispatcher``    — ``ActionDispatcher`` binds the ledger's state machine to a connector so the
                      ledger, not the destination, decides what a lost response means.
* ``notifications`` — ``NotificationConnector`` (dashboard, PagerDuty, Slack, email, fake) with
                      delivery receipts.
* ``outbox``        — durable, deduplicated, retried delivery on top of the control store.
* ``registry``      — builds connectors from settings; a channel without credentials is not built.

No test calls a real destination. Every adapter takes an injected transport so request shapes are
checked against recorded responses, and credentials never appear in receipts or logs.
"""

from stag.connectors.actions import (
    ActionConnector,
    ActionReceipt,
    FakeTicketingConnector,
    JiraConnector,
    ServiceNowConnector,
)
from stag.connectors.base import (
    ConnectorError,
    ConnectorRejected,
    ConnectorTimeout,
    HttpResponse,
    HttpxTransport,
    NoConnectorError,
    RecordedTransport,
    redact,
)
from stag.connectors.dispatcher import ActionDispatcher
from stag.connectors.notifications import (
    DashboardConnector,
    DeliveryReceipt,
    EmailConnector,
    FakeNotificationConnector,
    Notification,
    NotificationConnector,
    PagerDutyConnector,
    RecordedSmtp,
    SlackConnector,
)
from stag.connectors.outbox import NotificationOutbox
from stag.connectors.registry import build_action_connectors, build_notification_connectors

__all__ = [
    "ActionConnector",
    "ActionDispatcher",
    "ActionReceipt",
    "ConnectorError",
    "ConnectorRejected",
    "ConnectorTimeout",
    "DashboardConnector",
    "DeliveryReceipt",
    "EmailConnector",
    "FakeNotificationConnector",
    "FakeTicketingConnector",
    "HttpResponse",
    "HttpxTransport",
    "JiraConnector",
    "NoConnectorError",
    "Notification",
    "NotificationConnector",
    "NotificationOutbox",
    "PagerDutyConnector",
    "RecordedSmtp",
    "RecordedTransport",
    "ServiceNowConnector",
    "SlackConnector",
    "build_action_connectors",
    "build_notification_connectors",
    "redact",
]
