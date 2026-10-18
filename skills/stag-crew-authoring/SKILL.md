---
name: stag-crew-authoring
description: Create, validate and run a STAG crew — a declarative composition of roles (agents + skills + permissions) and a workflow of backbone steps (coordinate, transition, investigate, propose_action, authorize, dispatch, verify, notify, record) in crews/<name>/crew.yaml with a scenario for end-to-end checks. Use when asked for a crew, a team of agents for a use case (incident triage, problem management, release, customer care, log management, data ops, security), or when a crew run halts. Do not use for single-skill edits (stag-skill-authoring).
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: community
---

# STAG: Crew Authoring

A crew is **roles × skills × a workflow that only speaks backbone operations**. It cannot express
"the agent decides to restart the service"; it can only express "propose a restart that a human or
a severity-bounded policy authorizes, then dispatch, then verify". That is why every crew is
checkable end-to-end offline with `python -m stag.crews run <crew> --auto-approve`.

## Layout

```text
crews/<name>/
├── crew.yaml        # the definition (schema: stag/crews/schema.py)
├── scenario.yaml    # events + verification hints + expectations for the e2e run
└── README.md        # one diagram, roles table, how to run
```

## crew.yaml essentials

| Key | What to put there |
|---|---|
| `name`, `display_name`, `domain`, `purpose`, `when_to_use` | folder name == `name`; domain in `techops | customer-care | data | security | …` |
| `stream` | sources, event types, which payload/metadata fields the adapter must set |
| `signal_key` | dotted paths for `service` and `environment` (e.g. `metadata.service`, `payload.automation_id`, `payload.tool`) |
| `triggers` | declarative rules: `severity_threshold`, `pattern`, `sensitive_data`, `frequency`, `data_volume`, `numeric_threshold`; empty list = STAG defaults |
| `coordinator` | `IncidentCoordinator` kwargs: `investigation_cooldown_seconds`, `recurrence_window_seconds`, `dedup_retention` |
| `roles` | `id`, `title`, `agent` (file in `agents/`), `skills` (folders in `skills/`), `permissions.capabilities`, `human: true` for approvers |
| `workflow` | ordered steps; first must be `coordinate`; a crew that `dispatch`es must `authorize` |
| `outputs`, `kpis`, `guardrails` | what the crew produces, how it is measured, what it never does |

### Step actions

| action | backbone call | required fields |
|---|---|---|
| `coordinate` | `IncidentCoordinator.ingest` (runs at ingest for every event) | — |
| `transition` | `coordinator.transition` | `to` |
| `investigate` | `InvestigationAssignment` + `BoundedRun` | optional `assignment` limits; capabilities from the role |
| `propose_action` | `ActionLedger.prepare` | `plan_step`, `target_from`, `parameters`, `policy_version`, `preconditions` |
| `authorize` | `ActionLedger.authorize` or HELD | `policy: {mode: human|policy, auto_if_severity_at_most, approver_role}` (human approver must have `human: true`) |
| `dispatch` | `revalidate` + `dispatch` | — |
| `verify` | executor verdict → `ActionLedger.verify`; RESOLVED path or INVESTIGATING again | optional `on_recovered`, `on_not_recovered` step names |
| `notify` | provenance-tagged message | `channel`, `template` |
| `record` | draft artifact (pending review) | `artifact` |

The runner inserts the implicit lifecycle transitions (TRIAGED → INVESTIGATING → ACTION_PROPOSED →
AUTHORIZED/HELD → EXECUTING → VERIFYING) and records them on each step, so the YAML stays short
while the incident history stays complete.

## scenario.yaml

```yaml
name: short-slug
events:
  - id: fixed-id      # optional: same id on every repeat = redelivery test
    repeat: 3
    source: datadog
    event_type: metric          # any EventType value
    severity: high
    payload: { error_rate: 0.03, message: "pool exhausted" }
    metadata: { service: checkout-api, environment: production }
verification:
  default: healthy
  "default|production|payments-api|metric|error_rate_high#1": not_recovered   # per signal key, per verify attempt
expect: { incidents: 2, duplicate_deliveries: 2 }    # used by tests/test_crews.py
```

## Procedure

1. Start from the closest existing crew (`python -m stag.crews list`) and copy its folder.
2. Decide the signal key first: what is "the same episode" in this domain? (service, dataset,
   automation id, tool × department, product area × region).
3. Pick triggers that are deterministic on payload fields; name them as symptom classes.
4. Define roles with the **least** capabilities; mark the approver `human: true`.
5. Write the workflow; keep exactly one `propose_action` per mitigation path and put the human or
   policy gate right after it.
6. Add a scenario with: a redelivery (`id` + `repeat`), repeated distinct observations, a severity
   increase, one unrelated benign event, and (if you have a fallback path) a `not_recovered` hint.
7. Validate and run:
   ```bash
   python -m stag.crews validate
   python -m stag.crews run <name>                 # should HALT at the human gate
   python -m stag.crews run <name> --auto-approve  # should RESOLVE
   python -m pytest tests/test_crews.py -q
   ```
8. Add the crew to `crews/README.md`, create any missing agent/skill it references, and open a PR
   (PR template has a crews section).

## Guardrails

- Never give an automated role `production_mutations: true`.
- Never use `policy` authorization without `auto_if_severity_at_most`; never above `medium`.
- Never put secrets or raw payloads into scenarios; use shapes and identifiers.
- A crew must halt (HELD / awaiting) rather than guess when the human is missing or telemetry is unknown.
