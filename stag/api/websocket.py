"""WebSocket manager for real-time pipeline output broadcasting."""

from __future__ import annotations

import json

import structlog
from fastapi import WebSocket

logger = structlog.get_logger()


class ConnectionManager:
    """Manages WebSocket connections and broadcasts pipeline events."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.append(websocket)
        logger.info("ws.connected", total=len(self._connections))

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self._connections:
            self._connections.remove(websocket)
        logger.info("ws.disconnected", total=len(self._connections))

    async def broadcast(self, data: dict) -> None:
        """Broadcast a message to all connected WebSocket clients."""
        dead = []
        message = json.dumps(data, default=str)
        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def connection_count(self) -> int:
        return len(self._connections)
