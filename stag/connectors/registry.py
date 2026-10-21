"""Build connectors from settings. A destination whose credentials are missing is *not* built;
the omission is logged with the setting names so the operator can see what is wired."""

from __future__ import annotations

from typing import Any

import structlog

from stag.connectors.actions import (
    ActionConnector,
    FakeTicketingConnector,
    JiraConnector,
    ServiceNowConnector,
)
from stag.connectors.notifications import (
    DashboardConnector,
    EmailConnector,
    FakeNotificationConnector,
    NotificationConnector,
    PagerDutyConnector,
    SlackConnector,
    SmtplibTransport,
)

logger = structlog.get_logger()


def _names(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def build_action_connectors(settings: Any, *, transport: Any = None) -> list[ActionConnector]:
    built: list[ActionConnector] = []
    for name in _names(getattr(settings, "action_connectors", "")):
        if name == "fake-ticketing":
            built.append(FakeTicketingConnector())
        elif name == "jira":
            if all(getattr(settings, k, "") for k in ("jira_base_url", "jira_email", "jira_api_token", "jira_project_key")):
                built.append(JiraConnector(settings.jira_base_url, settings.jira_email, settings.jira_api_token, settings.jira_project_key, issue_type=getattr(settings, "jira_issue_type", "Task"), transport=transport))
            else:
                logger.warning("connectors.not_built", connector="jira", needs="JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY")
        elif name == "servicenow":
            if all(getattr(settings, k, "") for k in ("servicenow_instance_url", "servicenow_user", "servicenow_password")):
                built.append(ServiceNowConnector(settings.servicenow_instance_url, settings.servicenow_user, settings.servicenow_password, table=getattr(settings, "servicenow_table", "incident"), transport=transport))
            else:
                logger.warning("connectors.not_built", connector="servicenow", needs="SERVICENOW_INSTANCE_URL, SERVICENOW_USER, SERVICENOW_PASSWORD")
        else:
            logger.warning("connectors.unknown", connector=name, known="fake-ticketing, jira, servicenow")
    return built


def build_notification_connectors(settings: Any, *, transport: Any = None, smtp_transport: Any = None, dashboard: DashboardConnector | None = None) -> list[NotificationConnector]:
    built: list[NotificationConnector] = []
    for name in _names(getattr(settings, "notify_channels", "")):
        if name == "dashboard":
            built.append(dashboard or DashboardConnector())
        elif name == "fake":
            built.append(FakeNotificationConnector("fake"))
        elif name == "pagerduty":
            if getattr(settings, "pagerduty_routing_key", ""):
                built.append(PagerDutyConnector(settings.pagerduty_routing_key, transport=transport))
            else:
                logger.warning("connectors.not_built", connector="pagerduty", needs="PAGERDUTY_ROUTING_KEY")
        elif name == "slack":
            if getattr(settings, "slack_webhook_url", ""):
                built.append(SlackConnector(settings.slack_webhook_url, transport=transport))
            else:
                logger.warning("connectors.not_built", connector="slack", needs="SLACK_WEBHOOK_URL")
        elif name == "email":
            recipients = _names(getattr(settings, "email_to", ""))
            if getattr(settings, "smtp_host", "") and getattr(settings, "email_from", "") and recipients:
                transport_ = smtp_transport or SmtplibTransport(settings.smtp_host, int(getattr(settings, "smtp_port", 587)), user=getattr(settings, "smtp_user", ""), password=getattr(settings, "smtp_password", ""), starttls=bool(getattr(settings, "smtp_starttls", True)))
                built.append(EmailConnector(settings.email_from, recipients, transport_))
            else:
                logger.warning("connectors.not_built", connector="email", needs="SMTP_HOST, EMAIL_FROM, EMAIL_TO")
        else:
            logger.warning("connectors.unknown", connector=name, known="dashboard, pagerduty, slack, email, fake")
    return built
