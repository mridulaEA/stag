---
name: stag-techops-remediation-ledger
description: Propose, authorize, dispatch, reconcile and verify a remediation action for a STAG incident using durable operation identity so nothing is executed twice. Use when an investigation proposes a mitigation, when an operator asks to "restart/rollback/scale X", when a tool call timed out with unknown outcome, or when designing retry-safe actions. Enforces the three separate permissions (investigate ≠ recommend ≠ commit). Do not use for read-only diagnosis.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG TechOps: Remediation Ledger

A successful command is not verified recovery, and a timeout is not a failure. Every
consequential action gets a **durable operation record before dispatch**, bound to the incident,
exact plan step, target, parameters, policy version and preconditions. Retries reuse that
operation id. A different operation requires a new reviewed decision.

```text
PREPARED → AUTHORIZED → DISPATCHED → VERIFIED
                            ↓
                     OUTCOME_UNKNOWN → RECONCILING → VERIFIED / FAILED / (one retry)
AUTHORIZED → STALE (incident changed since approval) → re-AUTHORIZE or REJECT
```

## Three permissions, three actors

| Permission | Who holds it | STAG mechanism |
|---|---|---|
| Investigate | bounded agent | `InvestigationAssignment` with `production_mutations: false` |
| Recommend a change | agent or engineer | `ActionLedger.prepare(...)` → `PREPARED` (nothing happens yet) |
| Commit the change | accountable human / approved policy | `ActionLedger.authorize(op, actor, incident_version)` |

The model may draft the proposal. The **executor**, not the prompt, checks authorization,
preconditions and staleness.

## Procedure

1. **Prepare** — translate the proposal into one narrowly scoped plan step with validated
   target and parameters. Same step → same `operation_id` (deterministic), so two investigations
   recommending the same restart produce one record.
   ```python
   rec = ledger.prepare("INC-4821", "restart_component", "checkout-api",
                        {"replicas": 2}, policy_version="runbook:RB-17@v3",
                        preconditions=["pool_wait_p99_ms > 1000", "no_other_restart_in_10m"])
   ```
   Or `POST /api/techops/actions/prepare`.
2. **Authorize** — an accountable actor approves the exact target + parameters at a known
   `incident_version`. Show reviewers the evidence and the proposed change, not just an "Approve"
   button. Record who, when, under which policy.
3. **Revalidate, then dispatch** — before execution, `revalidate(op, current_incident_version)`.
   If the incident moved (another operator fixed it, new evidence), the approval is `STALE`;
   stop. Otherwise `dispatch(op)`; attempts are counted on the same id.
   Concurrency control must prevent two workers committing conflicting actions, not merely two
   identical model calls.
4. **Handle uncertainty explicitly** — on timeout / lost response call
   `mark_outcome_unknown(op)` → `RECONCILING`. Query the **target's** state. If the change is
   present → `reconcile(op, True)` (VERIFIED, no retry). If absent → `reconcile(op, False)`
   (one retry, same id). Never mint a new operation id for a retry.
   If the destination offers no reliable idempotency or queryable operation id, you cannot
   manufacture exactly-once with a local flag: stop blind retries and escalate.
5. **Verify** — hand off to `stag-techops-recovery-verification`. `verify(op, healthy, evidence)`
   records the independent outcome. Missing telemetry is unknown, not healthy.
6. **Record incident transitions** — `ACTION_PROPOSED → AUTHORIZED/HELD/REJECTED → EXECUTING →
   VERIFYING → RESOLVED` via the coordinator, each with actor and reason.

## Design rules for action tools (MCP or otherwise)

- Design bounded business operations, not raw endpoint wrappers:
  `prepare_credit_adjustment` vs `commit_approved_credit_adjustment`.
- Each tool: explicit inputs, permissions, preconditions, error behaviour, postconditions,
  duplicate protection for retried writes (caller-supplied operation id).
- Enforce tool permissions **outside the model**; text delimiters are not authorization.
- Tag every execution and notification with incident id + operation id so resulting logs carry
  provenance (avoid automation feedback loops).
- Historical replay: mutations and notifications disabled by default.

## Output

```yaml
operation_id: OP-9c1e…
incident_id: INC-4821
plan_step: restart_component
target: checkout-api
parameters: {replicas: 2}
policy_version: runbook:RB-17@v3
preconditions: [pool_wait_p99_ms > 1000, no_other_restart_in_10m]
state: authorized
authorized_by: oncall@corp.example  (incident_version 17)
dispatch_attempts: 0
next: revalidate against current incident version, then dispatch
```

## Related code

- `stag/techops/actions.py` — `ActionLedger`, `ActionRecord`, `ActionState`
- `stag/api/routes.py` — `/api/techops/actions/*`
- Tests: `tests/test_techops.py::TestActionLedger`
