---
name: stag-techops-enterprise-onboarding
description: Onboard an application, service or SaaS platform into enterprise STAG — register identity and ownership, define the normalization envelope, classify data (sensitivity, type, residency, trust, importance, purpose), compute eligible processing routes, and assign budgets. Use when connecting a new source, designing processing cells, or answering "which model/route may process this data?". Deterministic governance first; AI only inside permitted boundaries.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG TechOps: Enterprise Onboarding

Shared standards and governance. Distributed processing and ownership. Selective AI.
Enforced execution boundaries. STAG at enterprise scale is **an operations system that grants
agents bounded assignments**, not an enterprise-wide agent with guardrails.

## 1. Registration record (before connecting anything)

```yaml
source_registration:
  application_id: app-checkout            # stable id, never a display name
  service_ids: [checkout-api, checkout-worker]
  source_system: datadog | otel | salesforce | github | ...
  source_instance: acct-7731 / tenant-eu-1
  environment: production
  region: eu-west-1
  ownership:
    technical: team-checkout          # on-call route pd-checkout
    business: payments-domain
    data: dpo-payments
    budget: cc-4410
  dependencies: [postgres-main, identity-adapter]
  data_policies: [internal, pii-fields:[customer_email]]
  visibility_limits: "SaaS vendor exposes audit + health only; no internal logs"
```

Do not force technical, business, data and budget ownership into one `team` field. Unknown
ownership → explicit triage queue, never a model guess.

## 2. Normalization contract (common envelope, typed payloads)

| Field group | Preserve |
|---|---|
| Identity | source system, instance, logical event id, schema version, adapter version |
| Organization | application, service, business domain, owning team, allocation scope |
| Time | event time, observation time, ingestion time, source sequence |
| Semantics | signal type, original severity, normalized severity, units, meaning |
| Security | sensitivity labels, source permissions, residency, permitted purposes |
| Correlation | resource ids, trace ids, deployment ids, source record ids, incident relations |
| Evidence quality | parsing status, mapping provenance, missing fields, raw-evidence reference |

Map into `StreamEvent`: `source` = `source_system/source_instance`, `metadata.environment`,
`metadata.service`, `metadata.schema_version`, `metadata.sensitivity`; keep source severity in
`payload.original_severity`. Metrics keep units; traces keep span relations. Never flatten to a
message string "because an LLM might read it later". Missing information must stay visible.

Adapters are governed products: owner, supported versions, fixtures, rate limits, checkpointing,
backfill rules, schema-drift behaviour (see `stag-stream-adapter`). A vendor cursor is a stream
position, not an event identity.

## 3. Classification drives architecture

| Dimension | Example | Consequence |
|---|---|---|
| Sensitivity | internal / confidential / restricted | storage, handling, disclosure |
| Data type | PII, credentials, financial, source code | field-level minimization |
| Residency | approved regions | storage, inference, replication, fallback |
| Trust / provenance | approved runbook, authenticated telemetry, user text, inferred claim | how evidence may influence decisions |
| Operational importance | critical / ordinary / dev | detection priority, capacity reservation |
| Permitted purpose | diagnosis / audit / reporting | reuse limits |

Classify in stages: approved source/field labels → deterministic checks and secret detection →
AI-assisted classification only inside a boundary already permitted to inspect the data.
Uncertain → conservative handling or review. Labels propagate to derived artifacts (chunks,
embeddings, graph edges, cached answers, summaries). Some restrictions **combine**, not just
"take the higher level".

## 4. Eligible processing routes (compute before any AI call)

```text
permitted_routes = routes_for(data classification)
                 ∩ routes_for(workload)
                 ∩ routes_for(region)
                 ∩ routes_approved_for(task)
                 ∩ routes_within(authorized budget)
if empty: approved non-AI path | hold | escalate  — never improvise a provider substitution
```

Enforce in software (policy engine such as OPA), not in the prompt. Fallback must preserve
eligibility: a cheaper or more available model is not acceptable if it violates handling rules or
failed the task evaluation. Govern four assets separately: model route, agent definition, tool
capability, workflow policy.

## 5. Processing cells and the control plane

Organize by residency, security, volume and failure isolation — not one cell per department.
Shared control plane distributes versioned config (catalog, data policies, model registry,
capability catalog, budgets, evaluation standards). Cells keep enough local state to continue
established monitoring during a control-plane outage; sensitive writes stop when authorization
cannot be established. Name the shared dependencies (gateway, IdP, global DB) and give them
redundancy or degraded modes. The enterprise incident view receives only authorized summaries.

## 6. Budgets as admission control

Hierarchy: enterprise → business domain → service → environment → incident → investigation
attempt. Separate controls for spend per period, token/request rate, concurrent investigations,
per-incident usage, expensive diagnostic queries. Reserve before dispatch, settle after
(`BudgetLedger`). Identity attached by trusted infrastructure, not by an agent-supplied label.
Pre-approved emergency allocation needs an authorized activation path; an agent cannot label its
own task critical.

## 7. Onboarding checklist (exit criteria)

- [ ] Registration record complete; ownership resolved for all four roles
- [ ] Envelope mapping documented; original severity preserved; missing fields visible
- [ ] Adapter fixtures: redelivery, conflicting payload, late event, schema drift
- [ ] Classification labels assigned and propagated to derived data
- [ ] Permitted routes computed and tested with a restricted sample (must be denied)
- [ ] Budget allocation and reservation path tested, including exhaustion
- [ ] Incident destination mapping (PagerDuty/Jira) verified for dedup semantics
- [ ] Resilience rows 1, 2, 5, 11, 14, 15 green (`stag-techops-resilience-tests`)

## Related code

- `stag/models/schemas.py` — `StreamEvent` (`source`, `metadata`)
- `stag/techops/identity.py` — `SignalKey` with tenant / environment / service
- `stag/techops/budget.py` — `BudgetLedger`
