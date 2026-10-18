"""WebSocket manager for real-time pipeline output broadcasting."""

from __future__ import annotations

import asyncio
import json

import structlog
from fastapi import WebSocket

logger = structlog.get_logger()


class ConnectionManager:
    """Manages WebSocket connections and broadcasts pipeline events."""

    def __init__(self, send_timeout: float = 2.0) -> None:
        self._connections: list[WebSocket] = []
        self.send_timeout = send_timeout

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)
        logger.info("ws.connected", total=len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.remove(websocket)
        logger.info("ws.disconnected", total=len(self._connections))

    async def broadcast(self, data: dict) -> None:
        """Broadcast to all clients concurrently; a slow or dead client cannot delay the others."""
        if not self._connections:
            return
        message = json.dumps(data, default=str)

        async def send(ws: WebSocket) -> WebSocket | None:
            try:
                await asyncio.wait_for(ws.send_text(message), timeout=self.send_timeout)
                return None
            except Exception:
                return ws

        results = await asyncio.gather(*(send(ws) for ws in list(self._connections)))
        for ws in results:
            if ws is not None:
                self.disconnect(ws)

    @property
    def connection_count(self) -> int:
        return len(self._connections)
