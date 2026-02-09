"""FastAPI REST and WebSocket routes."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException

from stag.models import StreamEvent

router = APIRouter()

# These will be set by main.py during app startup
_pipeline = None
_ws_manager = None


def set_dependencies(pipeline, ws_manager):
    global _pipeline, _ws_manager
    _pipeline = pipeline
    _ws_manager = ws_manager


# --- Health & Stats ---

@router.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}


@router.get("/stats")
async def stats():
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    return _pipeline.stats


# --- Events ---

@router.get("/events")
async def get_events(limit: int = 50):
    """Get recent stream events."""
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    return {"events": _pipeline.recent_events[-limit:]}


@router.post("/events")
async def ingest_event(event: StreamEvent):
    """Inject an event into the pipeline via REST API."""
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    await _pipeline.inject_event(event)
    return {"status": "ingested", "event_id": event.id}


# --- Insights ---

@router.get("/insights")
async def get_insights(limit: int = 20):
    """Get recent pipeline outputs (triggered insights)."""
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    return {"insights": _pipeline.recent_outputs[-limit:]}


# --- Knowledge ---

@router.get("/knowledge/search")
async def search_knowledge(q: str, top_k: int = 10):
    """Search the hybrid knowledge base."""
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    result = await _pipeline.retriever.search(q, top_k=top_k)
    return result.model_dump(mode="json")


@router.get("/knowledge/graph/stats")
async def graph_stats():
    """Get knowledge graph statistics."""
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    return _pipeline.retriever.graph_store.stats


# --- Pipeline Control ---

@router.post("/pipeline/start")
async def start_pipeline():
    """Start the pipeline (if stopped)."""
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    if _pipeline._running:
        return {"status": "already_running"}
    await _pipeline.start()
    return {"status": "started"}


@router.post("/pipeline/stop")
async def stop_pipeline():
    """Stop the pipeline."""
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    await _pipeline.stop()
    return {"status": "stopped"}


# --- WebSocket ---

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time pipeline events and insights."""
    await _ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection alive; client can also send messages
            data = await websocket.receive_text()
            # Echo back or handle commands
            if data == "ping":
                await websocket.send_text('{"type": "pong"}')
    except WebSocketDisconnect:
        _ws_manager.disconnect(websocket)
