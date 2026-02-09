"""Abstract base class for stream sources."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from stag.models import StreamEvent


class StreamSource(ABC):
    """Base class for all stream sources.

    Subclasses implement `connect`, `disconnect`, and `_read_events`
    to provide an async iterator of StreamEvent objects.
    """

    def __init__(self) -> None:
        self._running = False
        self._queue: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=1000)

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection to the stream source."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Clean up and disconnect from the stream source."""

    @abstractmethod
    async def _read_events(self) -> None:
        """Read events from the source and put them on the internal queue."""

    async def start(self) -> None:
        """Start consuming events."""
        self._running = True
        await self.connect()
        asyncio.create_task(self._read_events())

    async def stop(self) -> None:
        """Stop consuming events."""
        self._running = False
        await self.disconnect()

    async def events(self) -> AsyncIterator[StreamEvent]:
        """Yield events from the stream as they arrive."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue

    @property
    def is_running(self) -> bool:
        return self._running
