"""Orchestrator + TechOps coordinator integration: repeated evidence must not reach the LLM."""

import pytest

from stag.config import Settings
from stag.models import StreamEvent
from stag.models.schemas import EventType, Severity
from stag.pipeline.orchestrator import PipelineOrchestrator
from stag.techops import IncidentCoordinator
from stag.triggers import TriggerEngine

pytest.importorskip("networkx")


class StubRetriever:
    def __init__(self):
        self.added = 0
        self.searches = 0

    async def initialize(self):
        pass

    async def add_event(self, event):
        self.added += 1

    async def search(self, query, top_k=8):
        from stag.models.schemas import RetrievalResult

        self.searches += 1
        return RetrievalResult(query=query)

    @property
    def stats(self):
        return {}


class StubGenerator:
    def __init__(self):
        self.calls = 0

    async def initialize(self):
        pass

    async def generate(self, event, trigger, retrieval):
        from stag.models.schemas import GeneratedInsight

        self.calls += 1
        return GeneratedInsight(
            trigger_id=trigger.trigger_id, event_id=event.id, title="t", summary="s", detail="d",
            severity=event.severity, confidence=0.5,
        )

    @property
    def stats(self):
        return {"calls": self.calls}


class StubStream:
    async def start(self):
        pass

    async def stop(self):
        pass

    async def events(self):
        if False:
            yield None


def _event(**kw):
    base = dict(
        source="checkout-api",
        event_type=EventType.DATA_TRANSFER,
        severity=Severity.CRITICAL,
        payload={"tool": "ChatGPT", "data_type": "source_code", "sanctioned": False, "data_size_kb": 9000},
        metadata={"environment": "production", "service": "checkout-api"},
    )
    base.update(kw)
    return StreamEvent(**base)


def _orchestrator(coordinator):
    retriever, generator = StubRetriever(), StubGenerator()
    orch = PipelineOrchestrator(
        stream_source=StubStream(),
        trigger_engine=TriggerEngine(),
        retriever=retriever,
        generator=generator,
        settings=Settings(),
        coordinator=coordinator,
    )
    messages = []

    async def capture(msg):
        messages.append(msg)

    orch.on_output(capture)
    return orch, generator, messages


async def test_without_coordinator_every_trigger_generates():
    orch, generator, messages = _orchestrator(None)
    for _ in range(3):
        await orch.inject_event(_event())
    assert generator.calls == 3
    types = [m["type"] for m in messages]
    # deterministic detection is emitted before enrichment, then the insight
    assert types == ["trigger", "insight"] * 3


async def test_with_coordinator_repeated_symptoms_skip_llm():
    orch, generator, messages = _orchestrator(IncidentCoordinator(investigation_cooldown_seconds=3600))
    first = _event()
    await orch.inject_event(first)
    await orch.inject_event(first)          # exact redelivery
    await orch.inject_event(_event())       # distinct observation, same symptom
    await orch.inject_event(_event())

    assert generator.calls == 1
    types = [m["type"] for m in messages]
    # new incident: trigger + insight; exact redelivery: incident_update with no trigger evaluation;
    # two repeated symptoms: trigger + incident_update (no LLM call)
    assert types == ["trigger", "insight", "incident_update", "trigger", "incident_update", "trigger", "incident_update"]
    insight = next(m for m in messages if m["type"] == "insight")
    assert insight["output"]["incident_id"] is not None
    assert insight["output"]["material_change"] == "new_incident"
    assert messages[2]["decision"]["duplicate"] is True and messages[2]["trigger"] is None
    assert messages[4]["decision"]["material_change"] == "none"

    stats = orch.stats
    assert stats["triggers_fired"] == 3  # the redelivery never reached the trigger engine
    assert stats["investigations_suppressed"] == 3
    assert stats["techops"]["observations_accepted"] == 3
    assert stats["techops"]["duplicate_deliveries"] == 1


async def test_duplicates_are_not_indexed_or_counted_by_stateful_rules():
    from stag.triggers import RuleRegistry
    from stag.triggers.rules import FrequencyRule

    registry = RuleRegistry()
    registry.register(FrequencyRule("burst", window_seconds=60, max_events=2, group_by="user"))
    retriever, generator = StubRetriever(), StubGenerator()
    orch = PipelineOrchestrator(
        stream_source=StubStream(), trigger_engine=__import__("stag.triggers", fromlist=["TriggerEngine"]).TriggerEngine(registry),
        retriever=retriever, generator=generator, settings=Settings(), coordinator=IncidentCoordinator(),
    )
    ev = _event(severity=Severity.LOW, payload={"user": "u1"})
    for _ in range(4):
        await orch.inject_event(ev)  # same identity delivered four times
    assert retriever.added == 1, "duplicates must not be indexed"
    assert orch.stats["triggers_fired"] == 0, "duplicates must not inflate frequency rules"
    assert generator.calls == 0


async def test_generation_failure_preserves_detection():
    class FailingGenerator(StubGenerator):
        async def generate(self, event, trigger, retrieval):
            raise RuntimeError("provider down")

    orch, _, messages = _orchestrator(None)
    orch.generator = FailingGenerator()
    await orch.inject_event(_event())
    types = [m["type"] for m in messages]
    assert types == ["trigger", "insight"]
    assert messages[1]["output"]["insight"]["generation_mode"] == "degraded"
    assert "provider down" in messages[1]["output"]["insight"]["degraded_reason"]


async def test_start_stop_are_idempotent_and_own_the_task():
    orch, _, _ = _orchestrator(None)
    await orch.start()
    first = orch._task
    await orch.start()
    assert orch._task is first, "a second start must not spawn a second processing loop"
    await orch.stop()
    assert orch._task is None and not orch._running
    await orch.stop()  # no error on repeated stop
