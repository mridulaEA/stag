---
name: stag-techops-recovery-verification
description: Verify that a STAG incident has actually recovered after mitigation, using independent health signals over a defined observation window with hysteresis. Use when an action was dispatched, when someone says "looks fixed, can we close it?", or when deciding RESOLVED vs INVESTIGATING again. Treats missing telemetry as unknown, never as recovery. Do not use to pick the mitigation itself.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG TechOps: Recovery Verification

Verification is independent of the actor who executed the mitigation and independent of the
model that proposed it. It answers one question: **is the externally observable health of the
service, and the customer impact, back within its defined recovery condition for long enough?**

## When this skill applies

- An `ActionRecord` is `DISPATCHED` or `RECONCILING` and needs `verify(...)`.
- The incident is in `EXECUTING` or `VERIFYING`.
- A collector stopped reporting and someone is tempted to call it "quiet".

## Procedure

1. **Define the recovery condition before looking.** Use the service's own criteria
   (SLO, error rate, latency, saturation) and the incident's impact statement from triage.
   Recovery condition ≠ entry condition: use **hysteresis** so the incident does not flap
   around one threshold. Example: entered at error rate > 1%; recover only when < 0.2% for the
   full window.
2. **Choose an observation window and minimum evidence.** Short-traffic services need a
   minimum request count, not just elapsed time. Record both.
3. **Check independent signals**, read-only:
   - service health endpoints / readiness,
   - customer-facing behaviour (synthetic checks, conversion/checkout success),
   - the metric that defined impact (burn rate, p99),
   - downstream dependencies that were implicated,
   - that telemetry is **arriving** (freshness, collector lag, dropped records).
4. **Classify the outcome.**

   | Observation | Verdict | Next |
   |---|---|---|
   | All signals within recovery condition for the full window, telemetry fresh | `healthy=True` | `verify(op, True, evidence)`; incident `VERIFYING → RESOLVED` |
   | Impact persists or worsens | `healthy=False` | `verify(op, False, …)`; incident `VERIFYING → INVESTIGATING`; new material change `mitigation_failed` |
   | Telemetry missing / collector gap | **unknown** | raise data-loss alert; do not resolve; extend window |
   | Late-arriving failure records after recovery | timeline correction, not reopening | attach to history; reopen only if current evidence shows recurrence |

5. **Record evidence with references** in `ActionRecord.verification` and in the incident
   transition reason. Include window, counts, and which signals were checked.
6. **Watch for recurrence.** The coordinator opens a *new episode* flagged
   `recurrence_after_resolved` if the signal returns within `recurrence_window_seconds`. That is
   expected behaviour, not a bug.

## Output

```yaml
operation_id: OP-9c1e…
incident_id: INC-4821
recovery_condition: "checkout error rate < 0.2% AND p99 < 800ms for 15m with ≥ 500 req"
window: 14:20Z..14:35Z (612 requests)
signals:
  - ref: metric:checkout.error_rate   value: 0.08%   ok: true
  - ref: metric:checkout.p99_ms       value: 410     ok: true
  - ref: synthetic:checkout-flow      value: 20/20   ok: true
  - ref: collector:otel-gw-3          freshness: 4s  ok: true
verdict: healthy          # healthy | not_recovered | unknown
notes: "identity-adapter still degraded; tracked separately as INC-4822"
```

## Guardrails

- A successful command, a green deploy, or an agent saying "resolved" is not verification.
- Missing telemetry is an unknown state, not evidence that errors stopped.
- Do not mark the parent incident resolved because a child or dependency closed, or vice versa.
- Verification is read-only. If it needs a fix, go back through the remediation ledger.

## Related code

- `stag/techops/actions.py` — `ActionLedger.verify`
- `stag/techops/coordinator.py` — `transition`, recurrence handling
