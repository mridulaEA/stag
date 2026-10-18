---
name: stag-problem-management
description: Run ITIL-style problem management on STAG incident history — detect recurring incidents and repeated workarounds, open a problem record that links (not merges) incident episodes, drive evidence-based root-cause analysis, draft a known-error record with workaround, and take a permanent fix through change approval with recurrence verification. Use when "this keeps happening", when workarounds are re-applied, or when asked for a known-error database. Not for live incident response (use stag-techops-incident-triage).
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG: Problem Management

Incidents restore service. Problems remove causes. STAG feeds the problem process from recorded
incident history instead of manual ticket review, and keeps the same discipline: identity before
meaning, evidence before narrative, approval before change.

## Detect a problem candidate

| Signal | Source | Rule |
|---|---|---|
| Same service + symptom class, ≥ N resolved episodes in a window | coordinator history / `recurrence_after_resolved` | `frequency` rule grouped by service |
| Ticket/incident tagged recurring / known error / workaround | ITSM tool | `pattern` on `category` |
| Same runbook step executed repeatedly | action ledger (`plan_step` counts) | review query |

A shared symptom is a *clue*. Check the signal keys and the evidence packages of the linked episodes
before declaring one problem; two causes can produce the same symptom.

## Procedure

1. **Open the problem record** (`transition → TRIAGED` on the problem episode) and link incident ids
   in `external_refs` (`incident:INC-…`). Each incident keeps its own impact, owner and timeline.
2. **Root-cause analysis** under a bounded assignment with deep-analysis limits and read-only
   capabilities: `read_incident_history`, `read_dependency_graph`, `inspect_deployment_metadata`,
   `read_metrics`, `retrieve_approved_runbooks`. Output: facts with references, ranked hypotheses
   with what would confirm/refute each, contradictions, next safe check.
3. **Known-error record (draft)**: symptom, scope (services, versions, conditions), detection,
   workaround with its cost/risk, owner, status `draft — pending review`. Not promoted to runbooks
   or the index by the crew.
4. **Permanent fix as one ledger operation**: `schedule_permanent_fix` with change type, target,
   preconditions (`root_cause_evidenced`, `workaround_documented`) and the change policy version.
   CAB / change owner (human) authorizes; `raise_change` dispatches (creates the change request).
5. **Recurrence verification**: the signal key stays quiet for the agreed window *and* the fix is
   observed in production (version, config). Only then `RESOLVED`.
6. **Problem review (draft)**: linked incidents, cost of recurrence, fix, follow-ups.

## KPIs

recurrence rate after fix (target 0 in window) · incidents linked per problem · time from pattern to
problem record · workaround applications avoided.

## Guardrails

- Never merge incident records into the problem; link them.
- Never mark a problem resolved because the change was deployed; verify no recurrence.
- Drafts remain drafts until a human promotes them.

Crew: `crews/problem-management`. Agent: `agents/stag-problem-analyst.md`.
