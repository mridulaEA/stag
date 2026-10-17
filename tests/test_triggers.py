"""Tests for trigger rules and engine."""

from stag.models import StreamEvent
from stag.models.schemas import EventType, Severity
from stag.triggers.engine import TriggerEngine
from stag.triggers.rules import (
    DataVolumeRule,
    PatternMatchRule,
    RuleRegistry,
    SensitiveDataRule,
    SeverityThresholdRule,
)


def _make_event(**kwargs) -> StreamEvent:
    defaults = {
        "source": "test/source",
        "event_type": EventType.TOOL_USAGE,
        "severity": Severity.MEDIUM,
        "payload": {},
    }
    defaults.update(kwargs)
    return StreamEvent(**defaults)


class TestSeverityThresholdRule:
    def test_fires_on_high_severity(self):
        rule = SeverityThresholdRule("test", Severity.HIGH)
        event = _make_event(severity=Severity.CRITICAL)
        result = rule.evaluate(event)
        assert result.fired is True
        assert result.confidence > 0

    def test_does_not_fire_below_threshold(self):
        rule = SeverityThresholdRule("test", Severity.HIGH)
        event = _make_event(severity=Severity.LOW)
        result = rule.evaluate(event)
        assert result.fired is False


class TestSensitiveDataRule:
    def test_fires_on_sensitive_unsanctioned(self):
        rule = SensitiveDataRule()
        event = _make_event(payload={"data_type": "source_code", "sanctioned": False})
        result = rule.evaluate(event)
        assert result.fired is True

    def test_does_not_fire_on_sanctioned(self):
        rule = SensitiveDataRule()
        event = _make_event(payload={"data_type": "source_code", "sanctioned": True})
        result = rule.evaluate(event)
        assert result.fired is False

    def test_does_not_fire_on_non_sensitive(self):
        rule = SensitiveDataRule()
        event = _make_event(payload={"data_type": "marketing_copy", "sanctioned": False})
        result = rule.evaluate(event)
        assert result.fired is False


class TestPatternMatchRule:
    def test_matches_pattern(self):
        rule = PatternMatchRule("test", "data_type", r"(pii|customer)")
        event = _make_event(payload={"data_type": "customer_pii"})
        result = rule.evaluate(event)
        assert result.fired is True

    def test_no_match(self):
        rule = PatternMatchRule("test", "data_type", r"(pii|customer)")
        event = _make_event(payload={"data_type": "marketing_copy"})
        result = rule.evaluate(event)
        assert result.fired is False


class TestDataVolumeRule:
    def test_fires_on_large_volume(self):
        rule = DataVolumeRule("test", threshold_kb=1000)
        event = _make_event(payload={"data_size_kb": 5000})
        result = rule.evaluate(event)
        assert result.fired is True

    def test_does_not_fire_below_threshold(self):
        rule = DataVolumeRule("test", threshold_kb=1000)
        event = _make_event(payload={"data_size_kb": 500})
        result = rule.evaluate(event)
        assert result.fired is False


class TestTriggerEngine:
    def test_returns_best_trigger(self):
        engine = TriggerEngine(RuleRegistry.default())
        event = _make_event(
            severity=Severity.CRITICAL,
            payload={"data_type": "source_code", "sanctioned": False, "data_size_kb": 10000},
        )
        result = engine.evaluate(event)
        assert result is not None
        assert result.fired is True

    def test_returns_none_for_benign_event(self):
        engine = TriggerEngine(RuleRegistry.default())
        event = _make_event(
            severity=Severity.LOW,
            payload={"data_type": "marketing_copy", "sanctioned": True, "data_size_kb": 10},
        )
        result = engine.evaluate(event)
        assert result is None

    def test_stats_tracking(self):
        engine = TriggerEngine(RuleRegistry.default())
        event = _make_event(severity=Severity.CRITICAL, payload={"data_type": "source_code", "sanctioned": False})
        engine.evaluate(event)
        assert engine.stats["evaluations"] == 1
