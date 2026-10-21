# Deployment-owner decisions (recorded 2026-10-19)

Answers from the project owner to the questions the two reviews said only the owner could decide.
These drive the Phase 1+ work; anything not listed here stays as `docs/review/FINDINGS.md` says.

| Area | Decision | Consequence |
|---|---|---|
| Access / identity | Single user, local use. No authentication for now. | R01 stays **planned**; keep the loopback bind default and the "no auth" labels. |
| Durable control state (R14) | **PostgreSQL, multi-worker.** | Repository interfaces around incidents, observations, requests, operations, reservations; SQLite for local tests, PostgreSQL service container in CI with two-process race and crash-recovery tests. |
| Event sources | **All connectors needed**: Kafka, monitoring webhooks, OpenTelemetry. | One adapter contract; **Kafka first** with real end-to-end tests; others ship with the contract and fakes until their turn. |
| Model provider | **Both Anthropic and OpenAI, selected per deployment.** | Keep the provider switch; add a model-route allowlist (R18 route part) and per-route accounting. |
| Actions / remediation destinations | **Open connector model**: adapters for any stack. | A destination adapter contract (prepare/dispatch/reconcile/receipt). **Ticketing (Jira / ServiceNow) first**; Kubernetes and webhook-executor adapters follow the same contract. |
| Paging / notifications | **All**: dashboard, PagerDuty, Slack, email. | Notification connector contract with delivery receipts, dedup and retry; fakes first. |
| Budget (R10) | **Token counts only.** | Reserve tokens before a call, settle on reported usage, caps per investigation and per day; no price table. |
| Deferred | **Only** frontend tests and the accessibility pass (R26/R30). | Multi-tenant / classification (R18), processing cells and the external agent protocol remain in scope for later phases; nothing else is dropped. |
| Test infrastructure | CI may start PostgreSQL and Kafka as service containers; local tests use SQLite and an in-memory broker fake. | `tests/` stays fast locally; integration jobs run in CI. |
| Credentials | **None provided.** Every connector ships with a recorded fake and a config template. | No real destination is called by any test; credentials are wired by the operator later, never committed. |

## Build order (one reviewable PR each)

1. Durable control store: repository interfaces, SQLite + PostgreSQL implementations,
   two-process and crash tests (R14, R16). **Done** in `stag/techops/store.py`; the outbox moves to
   step 3 with the first notification adapter, where it has a consumer.
2. Kafka ingestion: decode boundary, manual commit after durable registration, dead-letter,
   lag metrics; in-memory broker fake locally, real broker in CI (R17). **Done** in `stag/streams/kafka_stream.py`
   (`StreamSource.ack/nack` contract, `apache/kafka` service container in CI).
3. Connector contracts and first adapters: ticketing action adapter, PagerDuty / Slack / email
   notification adapters, all with fakes and config templates (R05, R23 live path). **Done** in
   `stag/connectors/` (`docs/connectors.md`): contracts, dispatcher, outbox, Jira / ServiceNow,
   dashboard / PagerDuty / Slack / email, recording fakes, `.env.example` templates.
4. Model routes and token budgets: allowlisted routes for both providers, reserve/settle on real
   token usage, per-investigation and per-day caps (R10, R18 route part).
5. Remaining connectors: monitoring webhooks, OpenTelemetry, Kubernetes actions.
