---
name: stag-techops-postmortem
description: Draft a blameless postmortem for a STAG incident from its recorded history (observations, transitions, investigations, actions, verification) and compute the inspectable metrics (false merges/splits, duplicate side effects, time to useful triage, verified restoration time, fully loaded cost). Use after RESOLVED or when asked for an incident review, timeline or lessons learned. The output is a DRAFT; it is not promoted to trusted knowledge until reviewed.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG TechOps: Postmortem Draft

The learning stage is where AI helps most and where unreviewed output does the most damage.
Draft the timeline and follow-ups; **do not** promote conclusions into runbooks, incident
knowledge or the retrieval index until a human review approves them.

## Inputs

- `GET /api/techops/incidents/{id}` — `history` (every transition with actor, reason, version),
  `observation_count`, `duplicate_delivery_count`, `signatures`, `external_refs`.
- `GET /api/techops/investigations` — requests and their `evidence_snapshot_version`.
- `GET /api/techops/actions` — operation records, attempts, reconciliation, verification.
- Triage and verification records produced by the sibling skills.
- Fully loaded cost inputs: model calls, tool calls, retrieval, human review time, rework.

## Structure of the draft

1. **Summary** (3 sentences): what users experienced, duration, what mitigated it.
2. **Impact**: measured, with references. Separate customer impact from internal noise.
3. **Timeline** — one line per event, UTC, with evidence ref and actor:
   detection → triage → investigation(s) → proposal → authorization → dispatch →
   (reconciliation) → verification → resolved. Mark late-arriving evidence that *corrected* the
   timeline as such; it did not reopen the incident.
4. **Contributing factors** — candidate causes with the evidence for and against each.
   A recent deployment may be coincidental; say so if causation was not established.
5. **What went well / what did not** — about the *system* (detection, correlation, controls),
   not about individuals.
6. **Pipeline properties check** — did the architecture behave?

   | Property | Evidence |
   |---|---|
   | Correct counting | distinct observations vs duplicate deliveries vs conflicts |
   | Controlled correlation | one episode? false merge or false split against reviewed labels? |
   | Evidence-grounded investigation | every promoted fact has a reference |
   | Bounded execution | one operation id per plan step; attempts; any duplicate side effect? |
   | Verified recovery | independent signals over the window; hysteresis respected |
   | Urgent path independence | did paging depend on AI availability? |

7. **Metrics** (define timestamps explicitly; avoid one ambiguous MTTR):
   - time to detection, time to useful triage, time to verified restoration,
   - investigations requested vs evidence-only updates (model calls avoided),
   - duplicate side effects (must be 0), stale approvals caught, reconciliations,
   - false merges / false splits,
   - cost per correct completed outcome = total cost across all attempts ÷ verified correct
     completions (include failed attempts, review and rework in the numerator).
8. **Follow-ups** — each with owner, type (detector, runbook, test, contract, tooling) and the
   resilience test that would have caught it (`stag-techops-resilience-tests`).

## Guardrails

- Blameless: name roles and controls, not people's mistakes.
- Keep the draft labelled `status: draft — pending review`. Do not call
  `/api/knowledge` indexing or edit runbooks from this skill.
- Do not claim percentage improvements that were not measured.
- Keep restricted data out: cite identifiers, not payloads.

## Related code

- `stag/techops/incident.py` — `Incident.history`
- `stag/techops/actions.py` — `ActionRecord.history`, `verification`
