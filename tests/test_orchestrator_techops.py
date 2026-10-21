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


async def test_investigation_request_is_leased_and_completed_by_the_generating_worker():
    coord = IncidentCoordinator(investigation_cooldown_seconds=3600)
    orch, generator, messages = _orchestrator(coord)
    orch.worker_id = "worker-a"
    await orch.inject_event(_event())
    assert generator.calls == 1
    request = coord.investigation_requests[0]
    assert request.status == "succeeded"
    assert request.attempts == 1
    assert request.lease_owner is None
    assert request.result_summary == "t"
    assert coord.pending_investigations() == []


async def test_second_worker_on_the_same_request_does_not_generate():
    """Two orchestrators share one control store (multi-worker). Both see the same material change
    for the same event; only the lease holder calls the model."""
    from stag.techops.store import InMemoryControlStore

    store = InMemoryControlStore()
    coord_a = IncidentCoordinator(investigation_cooldown_seconds=3600, store=store)
    coord_b = IncidentCoordinator(investigation_cooldown_seconds=3600, store=store)
    orch_a, gen_a, _ = _orchestrator(coord_a)
    orch_b, gen_b, messages_b = _orchestrator(coord_b)
    orch_a.worker_id, orch_b.worker_id = "worker-a", "worker-b"

    event = _event()
    # Pre-lease the request the way worker A would right after correlating, then let B process.
    reg = coord_a.register(event)
    decision = coord_a.correlate(event, orch_a.triggers.evaluate(event), reg)
    assert coord_a.claim_investigation(decision.investigation_request.id, "worker-a", 300) is not None

    # B receives the same event (second delivery → duplicate, no generation) and a fresh observation
    # of the same symptom (non-material → no generation): in neither case does B touch the lease.
    await orch_b.inject_event(event)
    await orch_b.inject_event(_event())
    assert gen_b.calls == 0
    assert gen_a.calls == 0
    request = coord_b.get_request(decision.investigation_request.id)
    assert request.status == "running" and request.lease_owner == "worker-a"
    # B refuses to complete work it does not hold.
    import pytest

    from stag.techops.store import ConflictError

    with pytest.raises(ConflictError):
        coord_b.complete_investigation(request.id, "worker-b", succeeded=True)


async def test_degraded_generation_leaves_the_request_retryable():
    class FailingGenerator(StubGenerator):
        async def generate(self, event, trigger, retrieval):
            self.calls += 1
            raise RuntimeError("provider down")

    coord = IncidentCoordinator(investigation_cooldown_seconds=3600)
    orch, _, messages = _orchestrator(coord)
    orch.generator = FailingGenerator()
    await orch.inject_event(_event())
    insight = next(m for m in messages if m["type"] == "insight")
    assert insight["output"]["insight"]["generation_mode"] == "degraded"
    request = coord.investigation_requests[0]
    assert request.status == "failed"
    assert [r.id for r in coord.pending_investigations()] == [request.id]
    # another worker can pick the failed work up
    assert coord.claim_investigation(request.id, "worker-b", 300).attempts == 2


async def test_request_leased_elsewhere_suppresses_generation_on_this_worker():
    """Direct race: the decision this worker computed carries a request another worker already leased."""
    from stag.techops.store import InMemoryControlStore

    store = InMemoryControlStore()
    coord = IncidentCoordinator(investigation_cooldown_seconds=3600, store=store)
    orch, generator, messages = _orchestrator(coord)
    orch.worker_id = "worker-a"
    event = _event()
    original_correlate = coord.correlate

    def correlate_then_lose_race(ev, trigger, registration):
        decision = original_correlate(ev, trigger, registration)
        if decision.investigation_request is not None:
            store.claim(coord.REQUESTS, decision.investigation_request.id, "worker-b", 300, from_statuses=("queued",))
        return decision

    coord.correlate = correlate_then_lose_race
    await orch.inject_event(event)
    assert generator.calls == 0
    assert messages[-1]["type"] == "incident_update"
    assert "another worker" in messages[-1]["decision"]["reason"]
    assert orch.stats["investigations_suppressed"] == 1
