"""STAG application entry point.

Creates the FastAPI app, initializes all pipeline components,
and starts the stream processing loop.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from stag.api.routes import router, set_dependencies
from stag.api.websocket import ConnectionManager
from stag.config import get_settings
from stag.generation import InsightGenerator
from stag.knowledge import GraphStore, HybridRetriever, SparseStore, VectorStore
from stag.pipeline import PipelineOrchestrator
from stag.streams.kafka_stream import KafkaStreamSource
from stag.streams.simulator import EventSimulator
from stag.streams.webhook import WebhookStreamSource
from stag.techops import ActionLedger, IncidentCoordinator
from stag.techops.store import control_store_from_url
from stag.triggers import TriggerEngine

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

logger = structlog.get_logger()

# Global refs
pipeline: PipelineOrchestrator | None = None
ws_manager = ConnectionManager()


def create_stream_source(settings):
    """Create the appropriate stream source based on configuration."""
    if settings.stream_source == "kafka":
        return KafkaStreamSource(
            bootstrap_servers=settings.kafka_bootstrap_servers,
            topic=settings.kafka_topic,
            group_id=settings.kafka_group_id,
        )
    elif settings.stream_source == "webhook":
        return WebhookStreamSource()
    else:
        return EventSimulator(interval=settings.simulator_interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize and start pipeline on startup, stop on shutdown."""
    global pipeline

    settings = get_settings()
    logger.info("stag.starting", stream_source=settings.stream_source, llm=settings.llm_provider)

    # Build components
    stream = create_stream_source(settings)
    trigger_engine = TriggerEngine()
    vector_store = VectorStore(
        persist_dir=settings.chroma_persist_dir,
        embedding_model=settings.embedding_model,
    )
    graph_store = GraphStore(persist_path=settings.graph_persist_path)
    sparse_store = SparseStore()
    retriever = HybridRetriever(vector_store, graph_store, sparse_store)
    generator = InsightGenerator(settings)

    # One control store is shared by the coordinator and the action ledger so that an incident and
    # the operations against it commit to the same database (and survive a restart together).
    store = control_store_from_url(settings.control_store_url)
    logger.info("techops.control_store", store=type(store).__name__, url=settings.control_store_url or "memory://")

    coordinator = None
    if settings.techops_coordinator_enabled:
        coordinator = IncidentCoordinator(
            tenant=settings.techops_tenant,
            investigation_cooldown_seconds=settings.techops_investigation_cooldown_seconds,
            recurrence_window_seconds=settings.techops_recurrence_window_seconds,
            dedup_retention=settings.techops_dedup_retention,
            processing_mode=settings.techops_processing_mode,
            store=store,
        )
        pending = coordinator.pending_investigations()
        logger.info("techops.coordinator_enabled", tenant=settings.techops_tenant, pending_investigations=len(pending))

    pipeline = PipelineOrchestrator(
        stream_source=stream,
        trigger_engine=trigger_engine,
        retriever=retriever,
        generator=generator,
        settings=settings,
        coordinator=coordinator,
    )

    # Register WebSocket broadcast as pipeline callback
    pipeline.on_output(ws_manager.broadcast)

    # Wire up API routes
    set_dependencies(pipeline, ws_manager, action_ledger=ActionLedger(store=store))

    # Initialize and start
    await pipeline.initialize()
    await pipeline.start()
    logger.info("stag.ready", port=settings.port)

    yield

    # Shutdown
    logger.info("stag.shutting_down")
    await pipeline.stop()
    close = getattr(store, "close", None)
    if close is not None:
        close()


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(
        title="STAG",
        description="Stream-Triggered Augmented Generation — Proactive AI for enterprise data streams",
        version="0.3.0",
        lifespan=lifespan,
    )

    settings = get_settings()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router, prefix="/api")

    # Serve frontend static files if built
    frontend_dist = Path(__file__).parent.parent / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app


app = create_app()


def cli():
    """CLI entry point."""
    settings = get_settings()
    print(f"""
╔══════════════════════════════════════════════════╗
║              STAG v0.3.0                         ║
║  Stream-Triggered Augmented Generation           ║
║                                                  ║
║  Stream:  {settings.stream_source:<20s}             ║
║  LLM:     {settings.llm_provider:<20s}             ║
║  Port:    {settings.port:<20d}             ║
╚══════════════════════════════════════════════════╝
""")
    uvicorn.run(
        "stag.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    cli()
