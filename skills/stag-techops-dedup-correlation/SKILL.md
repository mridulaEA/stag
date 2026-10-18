---
name: stag-techops-dedup-correlation
description: Decide what is actually repeating in a STAG stream and apply the right control — redelivery vs repeated symptom vs continuing signal vs duplicate ticket vs reingested document vs retried tool call. Use when counts look inflated, tickets multiply, "the same alert keeps firing", or when designing identity/dedup for a new source. Do not use to tune alert timing (use stag-techops-signal-tuning).
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG TechOps: Deduplication and Correlation

Duplicate data is not one problem. Before deleting anything, determine **what** is repeating.
Deduplicating two identical timeout messages from different requests would falsify the signal;
counting the same redelivered observation twice would inflate it.

## Six duplication problems, six controls

| What repeats | What it means | Control | Where in STAG |
|---|---|---|---|
| Same source event arrives again | Transport redelivery / replay | Stable observation identity + idempotent processing | `ObservationIdentity` = tenant + source + event_id; `IncidentCoordinator.ingest` returns `duplicate=True` |
| Different requests, similar errors | Repeated observations of a symptom | Aggregate counts, keep examples, **do not discard** | `Incident.attach_observation` (count + bounded examples) |
| Adjacent windows detect the same condition | Continuing signal, not a new incident | Stateful trigger evaluation + controlled update cadence | `SignalKey` lookup → existing open incident; `investigation_cooldown_seconds` |
| Several integrations open cases for one episode | Incident-level duplication | Incident identity + external record mapping | `Incident.external_refs`; `link_external` |
| A document is reingested | Duplicate content or a new knowledge version | Source identity, versioned chunks, controlled activation | knowledge pipeline (see "Knowledge updates" below) |
| A tool call is retried after an uncertain response | Possible repeated side effect | Durable action identity + reconciliation | `ActionLedger` (see `stag-techops-remediation-ledger`) |

## Procedure: classify a suspected duplicate

1. **Compare identities, not text.**
   - Same `tenant|source|event_id`, same payload hash → redelivery. Count under
     `duplicate_delivery_count`; no state change, no model call.
   - Same identity, different payload → **conflict**. Quarantine (`coordinator.quarantine`);
     never silently overwrite.
   - Different identity, same payload → two real observations. Both count.
2. **Resolve the signal grouping key.**
   `tenant + environment + service + failure_domain + symptom_class`. It locates candidate
   incidents; it does **not** prove shared cause. Two causes can share a symptom at the same time.
3. **Check episode boundaries.** Open incident with that key → attach. Recently `RESOLVED` →
   new episode flagged `recurrence_after_resolved`. Never match on generated incident titles.
4. **Decide if the change is material.** Only `severity_increase`, `new_signature`,
   `recurrence_after_resolved`, `mitigation_failed` or `cooldown_elapsed` create an
   `InvestigationRequest`. Everything else is an evidence-only update.
5. **Map external records once.** Keep `internal incident id → PagerDuty/Jira/ServiceNow id`
   in `external_refs`. Do not "search by title and create if missing". Verify each destination's
   actual dedup semantics (e.g. PagerDuty `dedup_key` scope) instead of assuming one key works
   everywhere.

## Identity contract (propose; adapt field names per source)

```text
Observation identity = tenant + source + event_id       (CloudEvents: source + id)
Signal grouping key  = tenant + environment + service + failure_domain + symptom_class
Incident identity    = durable ID for one incident episode (INC-...)
Action identity      = durable operation ID for one approved plan step (OP-...)
```

A payload hash is an integrity check, not an identity. A vendor cursor (e.g. Salesforce replay
id) is a stream position, not a unique event identity.

## Exactly-once stream processing is not exactly-once incident response

A Kafka transaction does not make the ticket API or a restart part of that transaction. For a
database-backed coordinator use an atomic local operation plus an outbox:

```text
BEGIN
  register observation under unique (tenant, source, event_id)
  if identical accepted observation exists: no repeated state change
  else: serialize incident update; apply evidence; increment version
        if material transition: insert uniquely identified investigation request
COMMIT
dispatcher publishes committed requests (may duplicate) → workers claim idempotently
```

A cache-only `seen` flag is not enough for consequential work. Size dedup retention to the
redelivery/replay horizon (`dedup_retention`). Run historical replay in an isolated mode with
mutations and notifications disabled.

## Knowledge updates need their own duplicate controls

Reingesting a runbook must not append new copies of its chunks. Keep: stable document id,
source version, content hash, chunk identity, parser config, index version. Build changed content
as a candidate version, validate, then activate via a version-aware manifest. Handle deletions
and permission changes as explicit updates. Newer is not automatically authoritative.

## Output

A short classification table for the case at hand:

```text
observation 7f3a…  → redelivery of 7f3a… (count only)
observation 91c0…  → new observation, attached to INC-4821 (v18), no material change
observation b22e…  → new signature "redis|timeout" on INC-4821 → INV-… requested
ticket JIRA-5512   → already mapped to INC-4821; do not open another
```

## Related code

- `stag/techops/identity.py`, `stag/techops/coordinator.py`
- Tests to copy from: `tests/test_techops.py::TestCoordinatorDedup`
