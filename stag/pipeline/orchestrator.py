"""Pipeline orchestrator: Stream → Trigger → Retrieve → Generate.

This is the core of STAG. The orchestrator continuously consumes events
from a stream source, evaluates triggers, performs hybrid retrieval when
a trigger fires, and generates actionable insights.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque

import structlog

from stag.config import Settings
from stag.generation import InsightGenerator
from stag.knowledge import HybridRetriever, VectorStore, GraphStore, SparseStore
from stag.models import StreamEvent, PipelineOutput
from stag.streams.base import StreamSource
from stag.triggers import TriggerEngine

logger = structlog.get_logger()


class PipelineOrchestrator:
    """Orchestrates the full STAG pipeline.

    Pipeline flow:
    1. Stream source emits events
    2. Each event is indexed into all knowledge stores
    3. Trigger engine evaluates rules
    4. If a trigger fires → hybrid retrieval → LLM generation
    5. Output is broadcast via callback (WebSocket, logging, etc.)
    """

    def __init__(
        self,
        stream_source: StreamSource,
        trigger_engine: TriggerEngine,
        retriever: HybridRetriever,
        generator: InsightGenerator,
        settings: Settings,
    ) -> None:
        self.stream = stream_source
        self.triggers = trigger_engine
        self.retriever = retriever
        self.generator = generator
        self.settings = settings

        self._running = False
        self._callbacks: list = []
        self._recent_events: deque[StreamEvent] = deque(maxlen=100)
        self._recent_outputs: deque[PipelineOutput] = deque(maxlen=50)
        self._event_count = 0
        self._trigger_count = 0
        self._insight_count = 0

    def on_output(self, callback) -> None:
        """Register a callback for pipeline outputs.

        Callback signature: async def callback(output: PipelineOutput) -> None
        """
        self._callbacks.append(callback)

    async def initialize(self) -> None:
        """Initialize all pipeline components."""
        await self.retriever.initialize()
        await self.generator.initialize()
        logger.info("pipeline.initialized")

    async def start(self) -> None:
        """Start the pipeline: connect to stream and begin processing."""
        self._running = True
        await self.stream.start()
        logger.info("pipeline.started", stream=type(self.stream).__name__)

        asyncio.create_task(self._process_loop())

    async def stop(self) -> None:
        """Stop the pipeline and disconnect from the stream."""
        self._running = False
        await self.stream.stop()
        await self.retriever.graph_store.persist()
        logger.info("pipeline.stopped", events=self._event_count, insights=self._insight_count)

    async def _process_loop(self) -> None:
        """Main processing loop: consume events and run pipeline."""
        async for event in self.stream.events():
            if not self._running:
                break
            try:
                await self._process_event(event)
            except Exception as e:
                logger.error("pipeline.event_error", event_id=event.id, error=str(e))

    async def _process_event(self, event: StreamEvent) -> None:
        """Process a single event through the full pipeline."""
        start = time.time()
        self._event_count += 1
        self._recent_events.append(event)

        # Step 1: Index event into knowledge stores
        await self.retriever.add_event(event)

        # Step 2: Evaluate triggers
        trigger = self.triggers.evaluate(event)

        if trigger is None:
            logger.debug("pipeline.no_trigger", event_id=event.id)
            # Broadcast event-only update (no insight)
            for cb in self._callbacks:
                try:
                    await cb({"type": "event", "event": event.model_dump(mode="json")})
                except Exception:
                    pass
            return

        self._trigger_count += 1

        # Step 3: Hybrid retrieval — build query from event and trigger
        query = self._build_retrieval_query(event, trigger)
        retrieval = await self.retriever.search(query, top_k=8)

        # Step 4: Generate insight
        insight = await self.generator.generate(event, trigger, retrieval)
        self._insight_count += 1

        elapsed = (time.time() - start) * 1000

        output = PipelineOutput(
            event=event,
            trigger=trigger,
            retrieval=retrieval,
            insight=insight,
            pipeline_time_ms=elapsed,
        )
        self._recent_outputs.append(output)

        logger.info(
            "pipeline.insight_generated",
            event_id=event.id,
            trigger=trigger.rule_name,
            severity=insight.severity.value,
            confidence=insight.confidence,
            pipeline_ms=round(elapsed, 1),
        )

        # Broadcast output
        for cb in self._callbacks:
            try:
                await cb({"type": "insight", "output": output.model_dump(mode="json")})
            except Exception as e:
                logger.error("pipeline.callback_error", error=str(e))

    @staticmethod
    def _build_retrieval_query(event: StreamEvent, trigger) -> str:
        """Build a search query from the event and trigger context."""
        parts = []
        payload = event.payload

        if payload.get("tool"):
            parts.append(payload["tool"])
        if payload.get("user"):
            parts.append(payload["user"])
        if payload.get("department"):
            parts.append(payload["department"])
        if payload.get("data_type"):
            parts.append(payload["data_type"])
        if payload.get("description"):
            parts.append(payload["description"])

        parts.append(event.event_type.value)
        parts.append(event.severity.value)

        return " ".join(parts)

    async def inject_event(self, event: StreamEvent) -> None:
        """Manually inject an event into the pipeline (for API/webhook use)."""
        await self._process_event(event)

    @property
    def recent_events(self) -> list[dict]:
        return [e.model_dump(mode="json") for e in self._recent_events]

    @property
    def recent_outputs(self) -> list[dict]:
        return [o.model_dump(mode="json") for o in self._recent_outputs]

    @property
    def stats(self) -> dict:
        return {
            "running": self._running,
            "events_processed": self._event_count,
            "triggers_fired": self._trigger_count,
            "insights_generated": self._insight_count,
            "trigger_rate": self._trigger_count / max(1, self._event_count),
            "trigger_engine": self.triggers.stats,
            "retriever": self.retriever.stats,
            "generator": self.generator.stats,
        }
