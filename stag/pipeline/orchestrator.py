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
from stag.knowledge import HybridRetriever
from stag.models import GeneratedInsight, PipelineOutput, RetrievalResult, StreamEvent
from stag.streams.base import StreamSource
from stag.techops import IncidentCoordinator
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
        coordinator: IncidentCoordinator | None = None,
    ) -> None:
        self.stream = stream_source
        self.triggers = trigger_engine
        self.retriever = retriever
        self.generator = generator
        self.settings = settings
        # Optional TechOps coordinator: dedup + incident correlation + investigation cadence.
        # When present, a fired trigger only reaches the LLM on a *material* incident change.
        self.coordinator = coordinator

        self._running = False
        self._task: asyncio.Task | None = None
        self._lifecycle_lock = asyncio.Lock()
        self.last_error: str | None = None
        self.index_timeout_seconds = getattr(settings, "index_timeout_seconds", 5.0)
        self._callbacks: list = []
        self._recent_events: deque[StreamEvent] = deque(maxlen=100)
        self._recent_outputs: deque[PipelineOutput] = deque(maxlen=50)
        self._event_count = 0
        self._trigger_count = 0
        self._insight_count = 0
        self._suppressed_count = 0

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
        """Start the pipeline (idempotent): connect to the stream and own the processing task."""
        async with self._lifecycle_lock:
            if self._running and self._task is not None and not self._task.done():
                return
            self._running = True
            self.last_error = None
            await self.stream.start()
            self._task = asyncio.create_task(self._process_loop(), name="stag-pipeline")
            self._task.add_done_callback(self._on_task_done)
            logger.info("pipeline.started", stream=type(self.stream).__name__)

    def _on_task_done(self, task: asyncio.Task) -> None:
        """Supervise the processing task: an unexpected end clears readiness and records why."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("pipeline.task_failed", error=self.last_error)
        elif self._running:
            self.last_error = "stream ended unexpectedly"
            logger.error("pipeline.stream_ended")
        self._running = False

    @property
    def healthy(self) -> bool:
        """Readiness: the processing task is alive, not merely requested."""
        return self._running and self._task is not None and not self._task.done()

    @property
    def status(self) -> str:
        if self.healthy:
            return "ready"
        if self.last_error:
            return "failed"
        return "stopped"

    async def stop(self) -> None:
        """Stop the pipeline (idempotent): cancel and await the processing task, then persist."""
        async with self._lifecycle_lock:
            if not self._running and self._task is None:
                return
            self._running = False
            await self.stream.stop()
            task, self._task = self._task, None
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            try:
                await self.retriever.graph_store.persist()
            except Exception as e:  # persistence failure must not mask the stop
                logger.error("pipeline.persist_error", error=str(e))
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

    async def _index(self, event: StreamEvent) -> None:
        """Index an accepted observation with a deadline; a stalled store cannot hold the loop."""
        try:
            await asyncio.wait_for(self.retriever.add_event(event), timeout=self.index_timeout_seconds)
        except TimeoutError:
            logger.error("pipeline.index_timeout", event_id=event.id, timeout=self.index_timeout_seconds)
        except Exception as e:
            logger.error("pipeline.index_error", event_id=event.id, error=str(e))

    async def _emit(self, message: dict) -> None:
        for cb in self._callbacks:
            try:
                await cb(message)
            except Exception as e:
                logger.error("pipeline.callback_error", error=str(e))

    async def _process_event(self, event: StreamEvent) -> None:
        """Process a single event.

        Order matters:
          1. identity (dedup) — before any stateful effect
          2. index + deterministic trigger evaluation
          3. emit the deterministic detection *before* optional enrichment
          4. coordinate (material change?) → retrieval → generation, each failure-isolated
        """
        start = time.time()
        self._event_count += 1
        self._recent_events.append(event)

        # Step 0 (TechOps): register identity first. Duplicates, conflicts and replay-tagged events in
        # live mode are never indexed and never reach stateful trigger rules.
        registration = None
        if self.coordinator is not None:
            registration = self.coordinator.register(event)
            if not registration.accepted:
                self._suppressed_count += 1
                await self._emit(
                    {
                        "type": "incident_update",
                        "event": event.model_dump(mode="json"),
                        "trigger": None,
                        "decision": {
                            "accepted": False,
                            "duplicate": registration.duplicate,
                            "conflicting": registration.conflicting,
                            "replay": registration.replay,
                            "incident_id": registration.prior_incident_id,
                            "reason": registration.reason,
                        },
                    }
                )
                return
        elif event.is_replay:
            # No coordinator: still never let replay-tagged traffic act as live detection.
            await self._emit({"type": "incident_update", "event": event.model_dump(mode="json"), "trigger": None,
                              "decision": {"accepted": False, "replay": True, "reason": "replay-tagged event ignored in live mode"}})
            return

        # Step 1: Evaluate deterministic triggers *before* any optional enrichment
        trigger = self.triggers.evaluate(event)

        if trigger is None:
            logger.debug("pipeline.no_trigger", event_id=event.id)
            if self.coordinator is not None:
                self.coordinator.correlate(event, None, registration)
            await self._emit({"type": "event", "event": event.model_dump(mode="json")})
            await self._index(event)
            return

        self._trigger_count += 1

        # Step 2 (TechOps): correlate before investigating. Repeated symptoms update incident
        # evidence; non-material changes never reach the LLM path.
        decision = None
        if self.coordinator is not None:
            decision = self.coordinator.correlate(event, trigger, registration)

        # Step 3: emit the deterministic detection *now*, before indexing, retrieval or generation.
        await self._emit(
            {
                "type": "trigger",
                "event": event.model_dump(mode="json"),
                "trigger": trigger.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json") if decision else None,
            }
        )

        # Step 3b: index the accepted observation (enrichment, bounded by a deadline)
        await self._index(event)

        if decision is not None and decision.investigation_request is None:
            self._suppressed_count += 1
            logger.info(
                "pipeline.investigation_suppressed",
                event_id=event.id,
                incident_id=decision.incident_id,
                reason=decision.reason,
            )
            await self._emit(
                {
                    "type": "incident_update",
                    "event": event.model_dump(mode="json"),
                    "trigger": trigger.model_dump(mode="json"),
                    "decision": decision.model_dump(mode="json"),
                }
            )
            return

        # Step 4: Hybrid retrieval — enrichment; failure degrades, never suppresses the detection
        query = self._build_retrieval_query(event, trigger)
        try:
            retrieval = await self.retriever.search(query, top_k=8)
        except Exception as e:
            logger.error("pipeline.retrieval_error", event_id=event.id, error=str(e))
            retrieval = RetrievalResult(query=query)

        # Step 5: Generate insight (validated; degraded fallback on provider/schema failure)
        try:
            insight = await self.generator.generate(event, trigger, retrieval)
        except Exception as e:
            logger.error("pipeline.generation_error", event_id=event.id, error=str(e))
            insight = GeneratedInsight(
                trigger_id=trigger.trigger_id,
                event_id=event.id,
                title=f"Alert: {trigger.rule_name}",
                summary=trigger.reason,
                detail="Generation failed; deterministic detection preserved.",
                severity=event.severity,
                confidence=trigger.confidence,
                generation_mode="degraded",
                degraded_reason=str(e)[:200],
            )
        self._insight_count += 1

        elapsed = (time.time() - start) * 1000

        output = PipelineOutput(
            event=event,
            trigger=trigger,
            retrieval=retrieval,
            insight=insight,
            pipeline_time_ms=elapsed,
            incident_id=decision.incident_id if decision else None,
            investigation_request_id=(
                decision.investigation_request.id if decision and decision.investigation_request else None
            ),
            material_change=decision.material_change.value if decision else None,
        )
        self._recent_outputs.append(output)

        logger.info(
            "pipeline.insight_generated",
            event_id=event.id,
            trigger=trigger.rule_name,
            severity=insight.severity.value,
            confidence=insight.confidence,
            generation_mode=insight.generation_mode,
            pipeline_ms=round(elapsed, 1),
        )
        await self._emit({"type": "insight", "output": output.model_dump(mode="json")})

    @staticmethod
    def _build_retrieval_query(event: StreamEvent, trigger) -> str:
        """Build a search query from the event and trigger context.

        Keeps exact operational identifiers (service, message, error code, deployment) alongside the
        enterprise-AI demo fields so sparse retrieval can match them.
        """
        parts = []
        payload = event.payload
        meta = event.metadata or {}

        for key in ("service", "dataset", "automation_id", "product_area"):
            value = meta.get(key) or payload.get(key)
            if value:
                parts.append(str(value))
        for key in ("message", "error_code", "template_id", "version", "status", "summary"):
            if payload.get(key):
                parts.append(str(payload[key])[:200])

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
            "investigations_suppressed": self._suppressed_count,
            "trigger_rate": self._trigger_count / max(1, self._event_count),
            "techops": self.coordinator.stats if self.coordinator else None,
            "status": self.status,
            "last_error": self.last_error,
            "trigger_engine": self.triggers.stats,
            "retriever": self.retriever.stats,
            "generator": self.generator.stats,
        }
