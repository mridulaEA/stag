---
name: stag-security-governance
description: Run enterprise AI and data governance on STAG's security streams — shadow AI usage, sensitive data to unsanctioned tools, acceptable-use violations, anomalous usage from DLP, proxy, identity and CASB — as deduplicated cases per tool and department with bounded exposure investigation, an approved DLP control, verified exposure stop and an auditable case record. Use for shadow AI governance, DLP response and AI acceptable-use enforcement. Never auto-blocks people.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: security
---

# STAG: Security & AI Governance

STAG's original scenario, run with the backbone: a wave of DLP alerts becomes one governance case,
users become graph entities inside it, and the control is a single approved operation.

## Signals (STAG default rules)

sensitive data in unsanctioned tool · PII pattern · source code / API key leak · high-frequency user
· large data transfer · severity threshold. Signal key: **tool × department × rule**
(`payload.tool`, `payload.department`).

## Procedure

1. **Triage the case**: distinct observations vs redeliveries; users, data types, sizes, sanctioned
   alternative available? Severity from data type and volume, not from alert count.
2. **Exposure investigation** (bounded, read-only): `read_incident_history` (prior cases for this
   tool/department), `read_dependency_graph` (users ↔ tools ↔ data types), `read_metrics`
   (volume trend), `query_scoped_logs` (proxy/DLP records by identifier). Never pull prompt text or
   file contents into context; cite data types, sizes, identifiers.
3. **Control as one operation**: `apply_dlp_block` scoped to the tool and sensitive data types,
   `notify_users: true`, preconditions `exposure_confirmed`, `sanctioned_alternative_available`,
   policy version = the acceptable-use policy. CISO delegate approves.
4. **Verify**: no sensitive transfers to the tool over the window; sanctioned tool adoption observed;
   no collateral block of sanctioned flows.
5. **Governance case record** (draft): scope, users (by id), data types, control, verification,
   training/follow-ups. Classification labels propagate to the case.

## Guardrails

- Controls target tools and data types; blocking a person is a human HR/security decision.
- Evidence never contains the sensitive content itself.
- A case is not closed because alerts stopped; verify the control and the alternative adoption.

Crew: `crews/security-governance`. Agent: `agents/stag-security-analyst.md`.
Live demo: `STREAM_SOURCE=simulator TECHOPS_COORDINATOR_ENABLED=true python -m stag.main`.
