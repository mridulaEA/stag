"""Trigger evaluation engine.

Evaluates all registered rules against incoming stream events
and determines which triggers should fire.
"""

from __future__ import annotations

import structlog

from stag.models import StreamEvent, TriggerResult
from stag.triggers.rules import RuleRegistry

logger = structlog.get_logger()


class TriggerEngine:
    """Evaluates stream events against registered trigger rules.

    The engine runs all rules against each event and returns the
    highest-confidence fired trigger, or None if no rules fire.
    """

    def __init__(self, registry: RuleRegistry | None = None) -> None:
        self.registry = registry or RuleRegistry.default()
        self._evaluation_count = 0
        self._fire_count = 0

    def evaluate(self, event: StreamEvent) -> TriggerResult | None:
        """Evaluate all rules against an event. Returns the best trigger or None."""
        self._evaluation_count += 1
        fired_triggers: list[TriggerResult] = []

        for rule in self.registry.rules:
            try:
                result = rule.evaluate(event)
                if result.fired:
                    fired_triggers.append(result)
                    logger.debug(
                        "trigger.fired",
                        rule=rule.name,
                        event_id=event.id,
                        confidence=result.confidence,
                    )
            except Exception as e:
                logger.error("trigger.evaluation_error", rule=rule.name, error=str(e))

        if not fired_triggers:
            return None

        # Return the highest-confidence trigger
        best = max(fired_triggers, key=lambda t: t.confidence)
        self._fire_count += 1

        logger.info(
            "trigger.best_match",
            rule=best.rule_name,
            event_id=event.id,
            confidence=best.confidence,
            total_fired=len(fired_triggers),
        )
        return best

    @property
    def stats(self) -> dict:
        return {
            "evaluations": self._evaluation_count,
            "fires": self._fire_count,
            "fire_rate": self._fire_count / max(1, self._evaluation_count),
            "rules_count": len(self.registry.rules),
        }
