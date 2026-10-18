"""Model output is validated at one boundary; malformed output degrades explicitly (R22)."""

from stag.config import Settings
from stag.generation.generator import InsightGenerator
from stag.models import StreamEvent
from stag.models.schemas import EventType, RetrievalResult, Severity, TriggerResult, TriggerType


def _ctx():
    event = StreamEvent(source="s", event_type=EventType.ANOMALY, severity=Severity.HIGH, payload={"x": 1})
    trigger = TriggerResult(trigger_type=TriggerType.THRESHOLD, rule_name="r", fired=True, confidence=0.7, event_id=event.id, reason="why")
    return event, trigger, RetrievalResult(query="q")


def test_malformed_shapes_degrade_without_raising():
    gen = InsightGenerator(Settings())
    event, trigger, retrieval = _ctx()
    for bad in ([1, 2], None, "text", {"severity": None, "confidence": "high"}, {"confidence": 2}, {"confidence": float("nan")}, {"recommendations": "not a list"}):
        insight = gen._build_insight(event, trigger, retrieval, bad, "llm", None)
        assert insight.generation_mode == "degraded", bad
        assert 0.0 <= insight.confidence <= 1.0
        assert insight.severity == Severity.HIGH
        assert insight.title.startswith("Alert:")


def test_valid_llm_output_is_marked_llm():
    gen = InsightGenerator(Settings())
    event, trigger, retrieval = _ctx()
    data = {"title": "Pool exhaustion", "summary": "s", "detail": "d", "severity": "critical", "recommendations": ["a", "b"], "confidence": 0.9}
    insight = gen._build_insight(event, trigger, retrieval, data, "llm", None)
    assert insight.generation_mode == "llm" and insight.severity == Severity.CRITICAL and insight.confidence == 0.9
