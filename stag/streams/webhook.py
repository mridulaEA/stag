"""Webhook/HTTP stream source for receiving push-based events."""

from __future__ import annotations

import structlog

from stag.models import StreamEvent
from stag.streams.base import StreamSource

logger = structlog.get_logger()


class WebhookStreamSource(StreamSource):
    """Receive events via HTTP webhook pushes.

    Events are pushed into the queue via the `ingest` method,
    which is called by the FastAPI webhook endpoint.
    """

    async def connect(self) -> None:
        logger.info("webhook_stream.connected")

    async def disconnect(self) -> None:
        logger.info("webhook_stream.disconnected")

    async def _read_events(self) -> None:
        """No-op: events are pushed via `ingest`, not polled."""
        pass

    async def ingest(self, event: StreamEvent) -> None:
        """Push an event into the stream from an external webhook."""
        await self._queue.put(event)
        logger.debug("webhook_stream.ingested", event_id=event.id)
