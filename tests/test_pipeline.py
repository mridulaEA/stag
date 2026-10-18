"""Tests for the STAG pipeline components."""

import pytest

from stag.knowledge.graph_store import GraphStore
from stag.knowledge.sparse_store import SparseStore
from stag.models import StreamEvent
from stag.models.schemas import EventType, Severity


def _make_event(**kwargs) -> StreamEvent:
    defaults = {
        "source": "test/source",
        "event_type": EventType.TOOL_USAGE,
        "severity": Severity.HIGH,
        "payload": {
            "tool": "ChatGPT",
            "user": "test@corp.example.com",
            "department": "Engineering",
            "data_type": "source_code",
            "sanctioned": False,
            "description": "Test user used ChatGPT with source code",
        },
    }
    defaults.update(kwargs)
    return StreamEvent(**defaults)


class TestSparseStore:
    @pytest.mark.asyncio
    async def test_add_and_search(self):
        store = SparseStore()
        event = _make_event()
        await store.add_event(event)
        assert store.count == 1

        results = await store.search("ChatGPT source_code")
        assert len(results) > 0
        assert results[0]["source"] == "sparse"

    @pytest.mark.asyncio
    async def test_empty_search(self):
        store = SparseStore()
        results = await store.search("anything")
        assert len(results) == 0


class TestGraphStore:
    @pytest.mark.asyncio
    async def test_add_event_creates_nodes(self):
        store = GraphStore(persist_path="/tmp/test_graph.json")
        await store.initialize()
        event = _make_event()
        await store.add_event(event)

        stats = store.stats
        assert stats["nodes"] > 0
        assert stats["edges"] > 0
        assert "user" in stats["node_types"]
        assert "tool" in stats["node_types"]

    @pytest.mark.asyncio
    async def test_search_finds_entities(self):
        store = GraphStore(persist_path="/tmp/test_graph2.json")
        await store.initialize()
        event = _make_event()
        await store.add_event(event)

        results = await store.search("ChatGPT Engineering")
        assert len(results) > 0


class TestStreamEvent:
    def test_text_representation(self):
        event = _make_event()
        text = event.text
        assert "source_code" in text or "ChatGPT" in text or "TOOL_USAGE" in text.upper()

    def test_default_id_generated(self):
        event = _make_event()
        assert event.id is not None
        assert len(event.id) > 0
