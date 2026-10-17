---
name: stag-techops-signal-tuning
description: Tune STAG's detection cadence and alert hygiene — debounce, cooldown, hysteresis, inhibition, burn-rate/multi-window SLO alerting, late-event and replay policy, backpressure. Use when alerts are noisy or flapping, when investigations fire too often or too rarely, when paging depends on AI, or when setting TECHOPS_* and Alertmanager-style parameters. Do not use to decide what a duplicate is (use stag-techops-dedup-correlation).
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: techops
---

# STAG TechOps: Signal and Cadence Tuning

Continuous monitoring does not require continuous LLM calls. The monitoring path updates
counters and incident state; the investigation path runs on **material change**. Tune the
controls below deliberately rather than asking a model how often to page.

## Four controls, four failure modes

| Control | Purpose | Failure to avoid | STAG knob |
|---|---|---|---|
| Debounce | combine a short burst before routine analysis | waiting forever while events keep arriving — set a max wait | trigger window (`FrequencyRule.window_seconds`), coordinator attach-before-investigate |
| Cooldown | limit repetitive analysis/notification | hiding increased severity or expanding impact | `TECHOPS_INVESTIGATION_COOLDOWN_SECONDS` (severity increase and new signature bypass it) |
| Hysteresis | different entry and recovery conditions | incident flapping around one threshold | recovery condition in `stag-techops-recovery-verification`; `TECHOPS_RECURRENCE_WINDOW_SECONDS` |
| Inhibition | suppress dependent alerts under an established parent | concealing an independent fault | parent/child incidents via `external_refs`/`attributes.related_incident`; keep evidence |

Existing alert managers (Prometheus Alertmanager: `group_wait`, `group_interval`,
`repeat_interval`, inhibition, silences) belong **upstream** of AI interpretation. Do not
re-create them inside a prompt.

## Procedure

1. **Measure first.** From `/api/stats`: `triggers_fired`, `investigations_suppressed`,
   `techops.evidence_only_updates`, `techops.investigations_requested`, `techops.duplicate_deliveries`.
   From incidents: episodes per signal key per day, reopen rate, flapping.
2. **Decide what counts as material.** Default: new incident, severity increase, new signature,
   recurrence after resolved, mitigation failed, cooldown elapsed. If a class of change is being
   missed, add it as a `MaterialChange` and a coordinator rule — do not shorten the cooldown globally.
3. **Tie paging to impact where SLOs exist.** Burn-rate with multiple windows balances speed and
   precision: `burn_rate = observed_bad_fraction / allowed_bad_fraction` (1% bad vs 0.1% allowed
   = 10×). Low-traffic services need minimum-evidence thresholds; copy no universal numbers.
4. **Keep urgent paging independent of AI.** An unavailable model must not stop an established
   detector from paging. Verify this with resilience row 8.
5. **Set time policies separately** for live detection, late evidence and historical replay.
   Watermarks are assumptions, not proof no late event can arrive. Use processing-time deadlines
   for critical alerts; revise evidence later. Late failure records correct the timeline; they do
   not reopen incidents unless current evidence shows recurrence.
6. **Backpressure is an operational requirement.** Bounded queues, priority classes,
   per-tenant limits. If enrichment falls behind, keep recording incident updates and paging; mark
   analysis `delayed`. Monitor: ingestion lag, dropped records, queue age, **age of the evidence
   behind an active investigation**.
7. **Avoid feedback loops.** Tag STAG's own tool executions and notifications with incident and
   operation ids; do not treat its own ticket updates as independent evidence of worsening.

## Output: tuning proposal

```yaml
signal_key: default|production|checkout-api|anomaly|pool_timeouts
observed: 412 triggers/day → 9 investigations, 403 evidence-only updates, 0 missed severity increases
changes:
  - TECHOPS_INVESTIGATION_COOLDOWN_SECONDS: 300 → 900   # rationale: analyses repeat with no new evidence
  - add MaterialChange: affected_instances_doubled         # rationale: impact growth was invisible
  - recovery condition: error_rate < 0.2% for 15m (entry was > 1%)   # hysteresis
  - paging: unchanged; verified independent of LLM availability (test row 8)
risks: slower routine re-analysis; mitigated by severity/new-signature bypass
```

## Related code

- `stag/config.py` — `techops_*` settings
- `stag/techops/coordinator.py` — `_update_incident` material-change rules
- `stag/triggers/rules.py` — `FrequencyRule`, `SeverityThresholdRule`
