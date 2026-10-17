# STAG TechOps: design summary

This document condenses the two STAG TechOps articles that `stag/techops/` and the
`stag-techops-*` skills implement.

## The problem

A checkout service times out after a deploy. Several instances log the same error, a collector
retries delivery, overlapping windows detect the problem again. Each trigger launches an
investigation and opens a ticket; two investigations recommend restarting the same component.
**The system did not miss the incident. It multiplied it.**

## The principle

> Continuously update the evidence. Reinvestigate meaningful changes. Authorize actions separately.

Two connected paths:

- **Monitoring path** (deterministic): validate events, update counters, evaluate known
  conditions, maintain incident state, page via established detectors. Never waits for AI.
- **Investigation path** (bounded agentic): retrieval + model when a *material change* needs
  interpretation. Runs inside an assignment contract with capabilities, limits and required output.

## Four identities

```text
Observation identity = tenant + source + event_id               → stag.techops.ObservationIdentity
Signal grouping key  = tenant + environment + service
                       + failure_domain + symptom_class          → stag.techops.SignalKey
Incident identity    = durable ID for one episode               → stag.techops.Incident
Action identity      = durable operation ID for one plan step   → stag.techops.ActionRecord
```

## Lifecycle

```text
DETECTED → CORRELATED → TRIAGED → INVESTIGATING → ACTION_PROPOSED
        → AUTHORIZED / HELD / REJECTED → EXECUTING → VERIFYING → RESOLVED | INVESTIGATING again
```

Transitions are explicit, versioned, and recorded with actor and reason
(`Incident.transition`, `POST /api/techops/incidents/{id}/transition`).

## Material change

Only these create an `InvestigationRequest` (id derived from incident + evidence version, so two
workers produce one request): new incident, severity increase, new failure signature, recurrence
after resolved, mitigation failed, cooldown elapsed. Everything else updates counts.

## Actions

```text
PREPARED → AUTHORIZED → DISPATCHED → VERIFIED
                            ↓
                     OUTCOME_UNKNOWN → RECONCILING
AUTHORIZED → STALE when the incident moved since approval
```

Retries reuse the operation id. Reconciliation reads the target's state. Verification is
independent and windowed; missing telemetry is unknown.

## Enterprise shape

Shared control plane (catalog, policies, model registry, capabilities, budgets, evaluation
standards) distributing versioned configuration to **processing cells** that each run adapters →
normalize + classify → stream processing → incident coordinator → scoped investigation →
authorized execution. Classification decides storage, index, model route and disclosure:

```text
permitted_routes = data ∩ workload ∩ region ∩ task ∩ budget   (empty → non-AI path / hold / escalate)
```

Budgets are admission control: reserve before dispatch, settle after (`BudgetLedger`).

## Where to look

| Concern | Code | Skill |
|---|---|---|
| dedup / correlation | `stag/techops/coordinator.py` | `stag-techops-dedup-correlation` |
| triage | `coordinator.transition` | `stag-techops-incident-triage` |
| investigation contract | `stag/techops/assignment.py` | `stag-techops-bounded-investigation` |
| remediation | `stag/techops/actions.py` | `stag-techops-remediation-ledger` |
| verification | `ActionLedger.verify` | `stag-techops-recovery-verification` |
| budgets | `stag/techops/budget.py` | `stag-techops-enterprise-onboarding` |
| tests | `tests/test_techops.py` | `stag-techops-resilience-tests` |
| pipeline gate | `stag/pipeline/orchestrator.py` | `stag-techops-signal-tuning` |

Enable the coordinator in the pipeline with `TECHOPS_COORDINATOR_ENABLED=true`.
