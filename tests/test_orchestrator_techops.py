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
    assert all(m["type"] == "insight" for m in messages)


async def test_with_coordinator_repeated_symptoms_skip_llm():
    orch, generator, messages = _orchestrator(IncidentCoordinator(investigation_cooldown_seconds=3600))
    first = _event()
    await orch.inject_event(first)
    await orch.inject_event(first)          # exact redelivery
    await orch.inject_event(_event())       # distinct observation, same symptom
    await orch.inject_event(_event())

    assert generator.calls == 1
    types = [m["type"] for m in messages]
    assert types == ["insight", "incident_update", "incident_update", "incident_update"]
    assert messages[0]["output"]["incident_id"] is not None
    assert messages[0]["output"]["material_change"] == "new_incident"
    assert messages[1]["decision"]["duplicate"] is True
    assert messages[2]["decision"]["material_change"] == "none"

    stats = orch.stats
    assert stats["triggers_fired"] == 4
    assert stats["investigations_suppressed"] == 3
    assert stats["techops"]["observations_accepted"] == 3
    assert stats["techops"]["duplicate_deliveries"] == 1
