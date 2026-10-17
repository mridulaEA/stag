---
name: stag-trigger-rule-authoring
description: Add or modify a STAG trigger rule (stag/triggers/rules.py) with tests and registry wiring. Use when asked to detect a new condition in the stream, change thresholds, or when a rule fires too often or too rarely. Keeps rules deterministic, confidence-scored and covered by tests; defers cadence to the TechOps coordinator rather than hard-coding suppression into rules.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: development
---

# STAG: Trigger Rule Authoring

Trigger rules are the deterministic detection layer. They evaluate one `StreamEvent` and return
a `TriggerResult` with `fired`, `confidence` (0..1), `reason` and `context`. The engine returns
the highest-confidence fired rule. Correlation, dedup and cadence belong to
`stag.techops.IncidentCoordinator`, not to the rule.

## Procedure

1. **Name the condition precisely** and decide its `TriggerType` (`THRESHOLD`, `PATTERN`,
   `ANOMALY`, `TEMPORAL`, `COMPOSITE`). The rule name becomes the incident `symptom_class`, so
   keep it stable and descriptive (`pool_timeouts`, not `rule7`).
2. **Implement** in `stag/triggers/rules.py`:

   ```python
   class ErrorRateRule(TriggerRule):
       """Fires when payload.error_rate exceeds a threshold."""

       def __init__(self, name: str = "error_rate_high", threshold: float = 0.01) -> None:
           super().__init__(name, TriggerType.THRESHOLD)
           self.threshold = threshold

       def evaluate(self, event: StreamEvent) -> TriggerResult:
           rate = float(event.payload.get("error_rate", 0.0))
           fired = rate > self.threshold
           confidence = min(1.0, rate / (self.threshold * 10)) if fired else 0.0
           return TriggerResult(
               trigger_type=self.trigger_type, rule_name=self.name, fired=fired,
               confidence=confidence, event_id=event.id,
               reason=f"error_rate {rate:.3%} {'exceeds' if fired else 'within'} {self.threshold:.3%}",
               context={"error_rate": rate, "threshold": self.threshold},
           )
   ```

   Rules must be pure functions of the event plus their own bounded state (as `FrequencyRule`
   keeps a windowed log). No I/O, no LLM calls, no network.
3. **Register** in `RuleRegistry.default()` only if it should be on for every deployment;
   otherwise document how to register it in a custom registry.
4. **Test** in `tests/test_triggers.py`: fires on the positive case, does not fire on the negative,
   confidence is within [0, 1], `reason` is human-readable. Add an engine test if precedence matters.
5. **Think about cadence separately.** If the rule will fire on every event during an outage,
   that is fine: the coordinator turns repeated symptoms into evidence-only updates. Do not add
   "fire only once per hour" logic inside the rule.
6. Run `python -m pytest tests/test_triggers.py -q` and `ruff check stag tests`.

## Guardrails

- Keep `context` small and free of secrets or full payloads (`value[:200]` pattern).
- Preserve the source severity; a rule may raise priority via confidence, not by rewriting
  `event.severity`.
- Rules evaluate untrusted payloads: never `eval`, never build regexes from payload content.
