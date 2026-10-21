# Connectors

STAG reaches destinations (ticketing, later Kubernetes and webhooks) and people (dashboard,
PagerDuty, Slack, email) only through connectors in `stag/connectors/`. The backbone never imports
a vendor SDK, and a destination can never change incident state: it only returns receipts.

No credentials live in this repository. Every connector ships with a **recorded fake** used by the
tests and the crews, and a **config template** below. Wire real destinations with environment
variables on the host that runs STAG.

## Action connectors (operations)

Contract (`stag/connectors/actions.py`):

| Method | Meaning |
|---|---|
| `supports(record)` | this connector is the destination for the operation's `target` |
| `dispatch(record)` | perform the effect **idempotently on `operation_id`** and return an `ActionReceipt` (`applied` / `unknown` / `rejected`) |
| `reconcile(record)` | observe the destination: `True` effect present, `False` absent, `None` cannot tell right now |

`ActionDispatcher` binds a connector to the ledger's state machine. The ledger moves to
`DISPATCHED` *before* the destination is called (an attempt is counted even if the process dies),
then:

| Connector outcome | Ledger |
|---|---|
| receipt `applied` | `APPLIED` with the receipt stored on the operation (verification still required) |
| `ConnectorTimeout` or any other error | `OUTCOME_UNKNOWN → RECONCILING`; a later `reconcile` asks the connector to *observe* |
| `ConnectorRejected` | `FAILED` (terminal) with the reason |
| reconcile observes the effect | `APPLIED` |
| reconcile observes no effect | `RETRY_READY`: one explicit `dispatch` of the **same** operation id, under the cumulative retry ceiling |

Operations whose target no configured connector accepts are not dispatched at all
(`NoConnectorError`; the ledger is untouched). Over the API, `POST /api/techops/actions/{id}/dispatch`
returns `executed: true` with the receipt when a connector ran, and `executed: false` when only the
ledger moved (no connector for that target).

### Ticketing: Jira and ServiceNow

Both key the ticket on the operation id and **search before creating**, so a retry after a lost
response finds the earlier ticket instead of opening a second one.

| Setting | Jira | ServiceNow |
|---|---|---|
| enable | `ACTION_CONNECTORS=jira` | `ACTION_CONNECTORS=servicenow` |
| endpoint | `JIRA_BASE_URL=https://<site>.atlassian.net` | `SERVICENOW_INSTANCE_URL=https://<instance>.service-now.com` |
| identity | `JIRA_EMAIL`, `JIRA_API_TOKEN` | `SERVICENOW_USER`, `SERVICENOW_PASSWORD` |
| where | `JIRA_PROJECT_KEY`, `JIRA_ISSUE_TYPE` (default `Task`) | `SERVICENOW_TABLE` (default `incident`) |
| operation id in | label `stag-op-<operation id>` | `correlation_id` |

A connector whose settings are incomplete is **not built**; startup logs `connectors.not_built`
with the names it needs. `GET /api/techops/connectors` shows what is wired, with secrets redacted.

The default `ACTION_CONNECTORS=fake-ticketing` is a recording in-memory destination whose receipts
carry `simulated: true`. Crews always use it.

## Notification connectors (people)

Contract (`stag/connectors/notifications.py`): `deliver(notification) -> DeliveryReceipt`; raise
`ConnectorRejected` for a permanent refusal and any other `ConnectorError` for a retryable one.

A `Notification` has a deterministic id from `(channel, dedup_key)`. The **outbox**
(`stag/connectors/outbox.py`) lives in the control store and guarantees:

- the same message is accepted once and reported `duplicate` afterwards, across restarts and workers;
- each queued message is leased to one worker, delivered, and its receipt (attempts, external id,
  error) recorded; retryable failures are retried up to `NOTIFY_MAX_ATTEMPTS`, permanent refusals
  are `dead` immediately;
- receipts are queryable: `GET /api/techops/notifications?incident_id=…`.

The app drains the outbox every `NOTIFY_DRAIN_INTERVAL_SECONDS`; `POST /api/techops/incidents/{id}/notify`
enqueues and drains immediately so the caller gets receipts.

| Channel | Enable | Settings |
|---|---|---|
| dashboard | `NOTIFY_CHANNELS=dashboard` | none (WebSocket `notification` messages) |
| PagerDuty | `…,pagerduty` | `PAGERDUTY_ROUTING_KEY` (Events API v2; `dedup_key` = notification dedup key) |
| Slack | `…,slack` | `SLACK_WEBHOOK_URL` (incoming webhook) |
| email | `…,email` | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_STARTTLS`, `EMAIL_FROM`, `EMAIL_TO` (comma-separated) |
| fake | `…,fake` | recording connector for tests |

### Detector-driven paging

With `PAGE_ON_SEVERITY` (default `critical`), a **new incident**, a **severity increase** or a
**recurrence after resolution** at or above that severity is paged to every configured channel
right after the deterministic trigger is emitted, before retrieval or generation. The dedup key is
`<incident id>:<severity>`, so redeliveries and repeated symptoms never page twice, while a
severity increase pages again. Paging does not depend on the model path being up.

## Writing a connector

1. Subclass `ActionConnector` or `NotificationConnector`; take an injected transport
   (`HttpTransport` / `SmtpTransport`) so tests can use `RecordedTransport` / `RecordedSmtp`.
2. Make the effect idempotent on `operation_id` (search before create; use the destination's own
   dedup where it has one).
3. Map outcomes honestly: 4xx → `ConnectorRejected`; timeouts, 5xx, missing ids → `ConnectorTimeout`.
4. Never put credentials in receipts, `describe()` or logs; use `redact()`.
5. Register it in `stag/connectors/registry.py` behind its settings, and add a recorded test in
   `tests/test_connectors.py`.
