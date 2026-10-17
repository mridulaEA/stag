---
name: stag-techops-resilience-tests
description: Generate or review the failure-scenario test suite for a STAG deployment — redelivery, similar-but-distinct errors, false merges, concurrent workers, late events, reingested runbooks, lost action responses, LLM outage, prompt injection via telemetry, collector silence and historical replay. Use when adding a source, a trigger rule, an action tool or a coordinator change, or when asked "is this production-ready?". Tests the stream and the lifecycle, not only the answer.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG TechOps: Resilience Test Matrix

A system that produces a useful explanation on a clean log sample has passed a small part of the
test. Every change to ingestion, correlation, investigation or execution should be covered by the
scenarios below. `tests/test_techops.py` implements the core ones against the in-memory
backbone; extend them for your adapters and destinations.

## The matrix

| # | Scenario | Expected behaviour | Where to assert |
|---|---|---|---|
| 1 | Same event delivered repeatedly | one accepted observation effect; duplicates counted separately | `CoordinatorDecision.duplicate`, `Incident.duplicate_delivery_count` |
| 2 | Similar messages from different requests | all genuine occurrences represented | `observation_count` increments |
| 3 | Two independent incidents share an error template | no merge on text similarity alone | distinct `incident_id` per `SignalKey` |
| 4 | Two workers process the same transition | one durable investigation request | `InvestigationRequest.make_id` collision |
| 5 | Old failure arrives after recovery | timeline correction or explicit re-evaluation; no automatic reopening | late event policy; `recurrence_after_resolved` only on current evidence |
| 6 | Runbook ingestion repeats / out of order | no uncontrolled duplicate chunks; no stale version activation | knowledge pipeline manifest |
| 7 | External action succeeds but response is lost | stable operation id; reconciliation; no second side effect | `ActionLedger.mark_outcome_unknown` → `reconcile` |
| 8 | LLM or retrieval unavailable | established detection and paging continue | orchestrator: generator failure must not block trigger/callback path |
| 9 | Malicious instructions in telemetry | no capability expansion, no unauthorized action | `BoundedRun.use_capability` raises `PermissionDenied` |
| 10 | Collector stops reporting | data-loss/freshness alert; no false recovery | verification returns `unknown` |
| 11 | Historical events replayed | no live notifications or mutations without explicit authorization | replay mode flag; ledger refuses dispatch |
| 12 | Approval goes stale | re-authorization required before dispatch | `ActionLedger.revalidate` → `STALE` |
| 13 | Budget exhausted | routine enrichment stops; critical paging continues; partial findings returned | `BudgetExhausted`; `on_limit_or_uncertainty` |
| 14 | SaaS schema changes without notice | adapter flags drift; mapping provenance recorded; no silent remap | adapter contract tests |
| 15 | Access revoked after caching | cached evidence invalidated or blocked | cache key includes authorization scope + policy state |
| 16 | Two agents propose conflicting remediation | both `PREPARED`; one reviewed decision; no concurrent dispatch | ledger + coordinator `ACTION_PROPOSED` state |

## Procedure

1. Identify which rows the change touches (a new adapter → 1, 2, 5, 11, 14; a new action tool →
   7, 9, 11, 12, 16; a coordinator change → 1–5, 13).
2. For each row write a test with a **state assertion**, not a transcript assertion. An agent's
   claim of success is different from the environment's recorded outcome.
3. Repeat nondeterministic trials; assert on recorded state and counts.
4. Add negative tests: the thing that must *not* happen (second dispatch, state jump, merge).
5. Measure, do not assume: false merges/splits against reviewed labels, severe-incident recall,
   duplicate side effects, time to useful triage, verified restoration time.

## Test skeleton

```python
def test_lost_response_does_not_repeat_side_effect(ledger, fake_target):
    rec = ledger.prepare("INC-1", "restart_component", "svc")
    ledger.authorize(rec.operation_id, "oncall", incident_version=3)
    ledger.dispatch(rec.operation_id); fake_target.apply(rec.operation_id)   # applied, reply lost
    ledger.mark_outcome_unknown(rec.operation_id)
    ledger.reconcile(rec.operation_id, target_state_observed=fake_target.has(rec.operation_id))
    assert rec.state.value == "verified"
    assert fake_target.apply_count(rec.operation_id) == 1
```

## Acceptance before expanding autonomy

| Increment | Evidence required |
|---|---|
| Event contract | rows 1, 2, 5, 14 pass for every adapter |
| Stateful coordination | rows 3, 4, 5, 16 pass; no false merges in labelled replay |
| Read-only investigation | rows 8, 9, 13 pass; grounding beats baseline |
| Narrow action class | rows 7, 11, 12, 16 pass under interruption and concurrency |

## Related code

- `tests/test_techops.py` — reference implementations of rows 1–4, 7, 9, 12, 13
