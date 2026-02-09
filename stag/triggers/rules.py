"""Trigger rules that evaluate stream events."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timedelta

from stag.models import StreamEvent, TriggerResult
from stag.models.schemas import Severity, TriggerType


class TriggerRule(ABC):
    """Base class for trigger rules."""

    def __init__(self, name: str, trigger_type: TriggerType) -> None:
        self.name = name
        self.trigger_type = trigger_type

    @abstractmethod
    def evaluate(self, event: StreamEvent) -> TriggerResult:
        """Evaluate whether this rule fires for the given event."""


class SeverityThresholdRule(TriggerRule):
    """Fires when event severity meets or exceeds a threshold."""

    SEVERITY_ORDER = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}

    def __init__(self, name: str, min_severity: Severity) -> None:
        super().__init__(name, TriggerType.THRESHOLD)
        self.min_severity = min_severity

    def evaluate(self, event: StreamEvent) -> TriggerResult:
        event_level = self.SEVERITY_ORDER[event.severity]
        threshold_level = self.SEVERITY_ORDER[self.min_severity]
        fired = event_level >= threshold_level
        confidence = min(1.0, event_level / 3.0) if fired else 0.0
        return TriggerResult(
            trigger_type=self.trigger_type,
            rule_name=self.name,
            fired=fired,
            confidence=confidence,
            event_id=event.id,
            reason=f"Severity {event.severity.value} {'meets' if fired else 'below'} threshold {self.min_severity.value}",
        )


class PatternMatchRule(TriggerRule):
    """Fires when event payload matches a regex pattern."""

    def __init__(self, name: str, field: str, pattern: str) -> None:
        super().__init__(name, TriggerType.PATTERN)
        self.field = field
        self.pattern = re.compile(pattern, re.IGNORECASE)

    def evaluate(self, event: StreamEvent) -> TriggerResult:
        value = str(event.payload.get(self.field, ""))
        match = bool(self.pattern.search(value))
        return TriggerResult(
            trigger_type=self.trigger_type,
            rule_name=self.name,
            fired=match,
            confidence=0.9 if match else 0.0,
            event_id=event.id,
            reason=f"Pattern '{self.pattern.pattern}' {'matched' if match else 'not matched'} in {self.field}",
            context={"field": self.field, "value": value[:200]},
        )


class SensitiveDataRule(TriggerRule):
    """Fires when sensitive data types are detected in unsanctioned tools."""

    SENSITIVE_TYPES = {
        "source_code", "customer_pii", "financial_data", "legal_documents",
        "employee_records", "api_keys", "credentials", "client_contracts",
    }

    def __init__(self, name: str = "sensitive_data_exposure") -> None:
        super().__init__(name, TriggerType.PATTERN)

    def evaluate(self, event: StreamEvent) -> TriggerResult:
        data_type = event.payload.get("data_type", "")
        is_sanctioned = event.payload.get("sanctioned", True)
        is_sensitive = data_type in self.SENSITIVE_TYPES
        fired = is_sensitive and not is_sanctioned

        confidence = 0.95 if fired else 0.0
        return TriggerResult(
            trigger_type=self.trigger_type,
            rule_name=self.name,
            fired=fired,
            confidence=confidence,
            event_id=event.id,
            reason=f"{'Sensitive' if is_sensitive else 'Non-sensitive'} data '{data_type}' in {'unsanctioned' if not is_sanctioned else 'sanctioned'} tool",
            context={"data_type": data_type, "sanctioned": is_sanctioned},
        )


class FrequencyRule(TriggerRule):
    """Fires when events from a source exceed a frequency threshold within a time window."""

    def __init__(
        self,
        name: str,
        window_seconds: int = 60,
        max_events: int = 10,
        group_by: str = "user",
    ) -> None:
        super().__init__(name, TriggerType.TEMPORAL)
        self.window_seconds = window_seconds
        self.max_events = max_events
        self.group_by = group_by
        self._event_log: dict[str, list[datetime]] = defaultdict(list)

    def evaluate(self, event: StreamEvent) -> TriggerResult:
        key = str(event.payload.get(self.group_by, event.source))
        now = datetime.utcnow()
        cutoff = now - timedelta(seconds=self.window_seconds)

        # Clean old entries and add new
        self._event_log[key] = [t for t in self._event_log[key] if t > cutoff]
        self._event_log[key].append(now)

        count = len(self._event_log[key])
        fired = count > self.max_events
        confidence = min(1.0, count / (self.max_events * 2)) if fired else 0.0

        return TriggerResult(
            trigger_type=self.trigger_type,
            rule_name=self.name,
            fired=fired,
            confidence=confidence,
            event_id=event.id,
            reason=f"{count} events from {key} in {self.window_seconds}s (threshold: {self.max_events})",
            context={"key": key, "count": count, "window": self.window_seconds},
        )


class DataVolumeRule(TriggerRule):
    """Fires when data transfer volume exceeds a threshold."""

    def __init__(self, name: str = "high_data_volume", threshold_kb: int = 5000) -> None:
        super().__init__(name, TriggerType.THRESHOLD)
        self.threshold_kb = threshold_kb

    def evaluate(self, event: StreamEvent) -> TriggerResult:
        data_size = event.payload.get("data_size_kb", 0)
        fired = data_size > self.threshold_kb
        confidence = min(1.0, data_size / (self.threshold_kb * 3)) if fired else 0.0
        return TriggerResult(
            trigger_type=self.trigger_type,
            rule_name=self.name,
            fired=fired,
            confidence=confidence,
            event_id=event.id,
            reason=f"Data volume {data_size}KB {'exceeds' if fired else 'within'} {self.threshold_kb}KB threshold",
            context={"data_size_kb": data_size},
        )


class RuleRegistry:
    """Registry of all active trigger rules."""

    def __init__(self) -> None:
        self._rules: list[TriggerRule] = []

    def register(self, rule: TriggerRule) -> None:
        self._rules.append(rule)

    @property
    def rules(self) -> list[TriggerRule]:
        return list(self._rules)

    @classmethod
    def default(cls) -> RuleRegistry:
        """Create a registry with the default enterprise AI monitoring rules."""
        registry = cls()
        registry.register(SeverityThresholdRule("critical_severity", Severity.HIGH))
        registry.register(SensitiveDataRule())
        registry.register(PatternMatchRule("pii_in_prompt", "data_type", r"(pii|customer|employee)"))
        registry.register(PatternMatchRule("source_code_leak", "data_type", r"(source_code|api_key|credential)"))
        registry.register(FrequencyRule("high_frequency_user", window_seconds=120, max_events=8))
        registry.register(DataVolumeRule("large_data_transfer", threshold_kb=5000))
        return registry
