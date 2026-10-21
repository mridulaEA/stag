"""FastAPI REST and WebSocket routes."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from stag.crews import CREWS_DIR, Scenario, discover_crews, load_crew, load_scenario, run_crew
from stag.models import StreamEvent
from stag.techops import ActionLedger, IncidentState, VerificationResult

router = APIRouter()

# These will be set by main.py during app startup
_pipeline = None
_ws_manager = None
_action_ledger: ActionLedger | None = None
_action_dispatcher = None
_outbox = None
_dashboard = None


def set_dependencies(pipeline, ws_manager, action_ledger: ActionLedger | None = None, action_dispatcher=None, outbox=None, dashboard=None):
    global _pipeline, _ws_manager, _action_ledger, _action_dispatcher, _outbox, _dashboard
    _pipeline = pipeline
    _ws_manager = ws_manager
    _action_ledger = action_ledger or ActionLedger()
    _action_dispatcher = action_dispatcher
    _outbox = outbox
    _dashboard = dashboard


async def _deliver_pending() -> list:
    """Drain the outbox now (API callers get receipts) and push dashboard messages to clients."""
    if _outbox is None:
        return []
    receipts = await asyncio.to_thread(_outbox.drain)
    if _dashboard is not None and _ws_manager is not None:
        for message in _dashboard.pop_pending():
            await _ws_manager.broadcast({"type": "notification", "notification": message})
    return receipts


# --- Health & Stats ---

@router.get("/health")
async def health():
    """Liveness plus component readiness. ``status`` is ``degraded`` when a required part is down."""
    components = {
        "pipeline": getattr(_pipeline, "status", "not_initialized") if _pipeline is not None else "not_initialized",
        "pipeline_error": getattr(_pipeline, "last_error", None) if _pipeline is not None else None,
        "generator": (
            "llm" if _pipeline is not None and getattr(getattr(_pipeline, "generator", None), "stats", {}).get("has_client")
            else "fallback"
        ),
        "techops_coordinator": "enabled" if _pipeline is not None and getattr(_pipeline, "coordinator", None) else "disabled",
        "websocket_clients": _ws_manager.connection_count if _ws_manager is not None else 0,
    }
    ready = components["pipeline"] == "ready"
    return {"status": "ok" if ready else "degraded", "ready": ready, "version": "0.3.0", "mode": "local-demo", "components": components}


@router.get("/ready")
async def ready():
    """Readiness probe: 503 unless the stream processing task is alive."""
    if _pipeline is None or not getattr(_pipeline, "healthy", False):
        raise HTTPException(503, f"pipeline not ready: {getattr(_pipeline, 'last_error', None) or getattr(_pipeline, 'status', 'not_initialized')}")
    return {"ready": True}


@router.get("/stats")
async def stats():
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    return _pipeline.stats


# --- Events ---

@router.get("/events")
async def get_events(limit: int = Query(50, ge=1, le=500)):
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
async def get_insights(limit: int = Query(20, ge=1, le=200)):
    """Get recent pipeline outputs (triggered insights)."""
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    return {"insights": _pipeline.recent_outputs[-limit:]}


# --- Knowledge ---

@router.get("/knowledge/search")
async def search_knowledge(q: str = Query(min_length=1, max_length=500), top_k: int = Query(10, ge=1, le=50)):
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
    await _pipeline.start()  # idempotent
    return {"status": "running"}


@router.post("/pipeline/stop")
async def stop_pipeline():
    """Stop the pipeline."""
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    await _pipeline.stop()
    return {"status": "stopped"}


# --- TechOps: incidents, investigations, actions ---


def _coordinator():
    if not _pipeline:
        raise HTTPException(503, "Pipeline not initialized")
    if _pipeline.coordinator is None:
        raise HTTPException(404, "TechOps coordinator disabled (set TECHOPS_COORDINATOR_ENABLED=true)")
    return _pipeline.coordinator


class TransitionRequest(BaseModel):
    to: IncidentState
    actor: str
    reason: str = ""


class ExternalLinkRequest(BaseModel):
    system: str
    external_id: str


class PrepareActionRequest(BaseModel):
    incident_id: str = Field(min_length=1, max_length=64)
    plan_step: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=200)
    parameters: dict = Field(default_factory=dict)
    policy_version: str = Field(default="unversioned", max_length=120)
    preconditions: list[str] = Field(default_factory=list, max_length=32)


class AuthorizeActionRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=1000)
    # Optional consistency check only. The approval is always bound to the server's current
    # incident version; a mismatch is rejected rather than trusted.
    expected_incident_version: int | None = Field(default=None, ge=1)


class VerificationEvidence(BaseModel):
    """Evidence for a verification claim: where it came from, over what window, which signals."""

    source: str = Field(min_length=1, max_length=200)
    window: str = Field(min_length=1, max_length=200)
    signals: list[str] = Field(min_length=1, max_length=64)
    target: str | None = Field(default=None, max_length=200)
    simulated: bool = False
    details: dict = Field(default_factory=dict)


class VerifyActionRequest(BaseModel):
    """A verification claim. Empty evidence does not validate (422)."""

    healthy: bool | None
    evidence: VerificationEvidence
    actor: str = Field(default="verifier", min_length=1, max_length=200)


def _operation_or_404(operation_id: str):
    try:
        return _action_ledger.get(operation_id)
    except KeyError:
        raise HTTPException(404, f"unknown operation {operation_id}") from None


def _incident_for_operation(record):
    """Load the incident behind an operation from server state (never from the request)."""
    coord = _coordinator()
    try:
        return coord.get_incident(record.incident_id)
    except KeyError:
        raise HTTPException(409, f"operation {record.operation_id} refers to unknown incident {record.incident_id}") from None


@router.get("/stream/dead-letters")
async def stream_dead_letters():
    """Records the stream adapter consumed but could not process (coordinates and reason only)."""
    if _pipeline is None:
        raise HTTPException(503, "pipeline not ready")
    stream = _pipeline.stream
    letters = getattr(stream, "dead_letters", None)
    if letters is None:
        raise HTTPException(404, f"{type(stream).__name__} has no dead-letter buffer")
    return {"dead_letters": letters, "stats": getattr(stream, "stats", {})}


@router.get("/techops/incidents")
async def list_incidents(open_only: bool = False):
    coord = _coordinator()
    incidents = coord.open_incidents if open_only else coord.incidents
    return {"incidents": [i.model_dump(mode="json") for i in incidents], "stats": coord.stats}


@router.get("/techops/incidents/{incident_id}")
async def get_incident(incident_id: str):
    coord = _coordinator()
    try:
        incident = coord.get_incident(incident_id)
    except KeyError:
        raise HTTPException(404, f"unknown incident {incident_id}") from None
    return {
        "incident": incident.model_dump(mode="json"),
        "actions": [a.model_dump(mode="json") for a in _action_ledger.for_incident(incident_id)],
    }


@router.post("/techops/incidents/{incident_id}/transition")
async def transition_incident(incident_id: str, body: TransitionRequest):
    """Explicit, recorded lifecycle transition. An agent message never changes state by itself."""
    coord = _coordinator()
    try:
        incident = coord.transition(incident_id, body.to, actor=body.actor, reason=body.reason)
    except KeyError:
        raise HTTPException(404, f"unknown incident {incident_id}") from None
    except Exception as e:  # InvalidTransition
        raise HTTPException(409, str(e)) from e
    return incident.model_dump(mode="json")


@router.post("/techops/incidents/{incident_id}/external")
async def link_external(incident_id: str, body: ExternalLinkRequest):
    coord = _coordinator()
    try:
        coord.link_external(incident_id, body.system, body.external_id)
    except KeyError:
        raise HTTPException(404, f"unknown incident {incident_id}") from None
    return {"status": "linked"}


@router.get("/techops/investigations")
async def list_investigations(pending: bool = False):
    """Investigation requests with their work lifecycle (queued → running → succeeded | failed).

    ``pending=true`` lists only the work still claimable by a worker (queued, or failed and retryable).
    """
    coord = _coordinator()
    requests = coord.pending_investigations() if pending else coord.investigation_requests
    return {"investigations": [r.model_dump(mode="json") for r in requests]}


@router.get("/techops/investigations/{request_id}")
async def get_investigation(request_id: str):
    coord = _coordinator()
    try:
        request = coord.get_request(request_id)
    except KeyError:
        raise HTTPException(404, f"unknown investigation request {request_id}") from None
    return {"investigation": request.model_dump(mode="json"), "stale": coord.is_request_stale(request_id)}


@router.get("/techops/actions")
async def list_actions():
    _coordinator()
    return {"actions": [a.model_dump(mode="json") for a in _action_ledger.records], "stats": _action_ledger.stats}


@router.post("/techops/actions/prepare")
async def prepare_action(body: PrepareActionRequest):
    """Create (or return) the durable operation record for an *existing, open* incident."""
    coord = _coordinator()
    try:
        incident = coord.get_incident(body.incident_id)
    except KeyError:
        raise HTTPException(404, f"unknown incident {body.incident_id}") from None
    if not incident.is_open:
        raise HTTPException(409, f"incident {body.incident_id} is {incident.state.value}; no new actions")
    record = _action_ledger.prepare(**body.model_dump())
    return _action_ledger.view(record.operation_id).model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/authorize")
async def authorize_action(operation_id: str, body: AuthorizeActionRequest):
    """Bind an approval to the operation's exact terms and the incident's *current* version."""
    record = _operation_or_404(operation_id)
    incident = _incident_for_operation(record)
    if not incident.is_open:
        raise HTTPException(409, f"incident {incident.id} is {incident.state.value}")
    if body.expected_incident_version is not None and body.expected_incident_version != incident.version:
        raise HTTPException(
            409,
            f"incident {incident.id} is at version {incident.version}, not {body.expected_incident_version}; review the current evidence",
        )
    try:
        record = _action_ledger.authorize(operation_id, body.actor, incident.version, body.note)
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/dispatch")
async def dispatch_action(operation_id: str):
    """Dispatch after the ledger revalidates the approval against the incident's current version.

    There is no caller-supplied version: freshness is decided from server state. With a connector
    configured for the operation's target, the destination is called and its receipt recorded
    (applied / unknown → reconciling / rejected → failed). Without one, the operation is dispatched
    in the ledger only and the response says so (``executed: false``).
    """
    record = _operation_or_404(operation_id)
    incident = _incident_for_operation(record)
    if not incident.is_open:
        raise HTTPException(409, f"incident {incident.id} is {incident.state.value}")
    connector = _action_dispatcher.connector_for(record) if _action_dispatcher is not None else None
    try:
        if connector is not None:
            record, receipt = await asyncio.to_thread(_action_dispatcher.execute, operation_id, "api", incident.version)
            return {**record.model_dump(mode="json"), "executed": True, "receipt": receipt.model_dump(mode="json")}
        record = _action_ledger.dispatch(operation_id, actor="api", current_incident_version=incident.version)
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return {**record.model_dump(mode="json"), "executed": False, "receipt": None}


@router.post("/techops/actions/{operation_id}/outcome-unknown")
async def action_outcome_unknown(operation_id: str):
    _operation_or_404(operation_id)
    try:
        record = _action_ledger.mark_outcome_unknown(operation_id)
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/reconcile")
async def reconcile_action(operation_id: str, target_state_observed: bool | None = None):
    """Settle an uncertain outcome. Without ``target_state_observed`` the operation's connector
    observes the destination itself; an observation it cannot make leaves the operation reconciling."""
    record = _operation_or_404(operation_id)
    try:
        if target_state_observed is None:
            if _action_dispatcher is None or _action_dispatcher.connector_for(record) is None:
                raise HTTPException(422, "target_state_observed is required: no connector can observe this operation's target")
            record, observed = await asyncio.to_thread(_action_dispatcher.reconcile, operation_id, "api")
            return {**record.model_dump(mode="json"), "observed": observed}
        record = _action_ledger.reconcile(operation_id, target_state_observed)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return {**record.model_dump(mode="json"), "observed": target_state_observed}


@router.get("/techops/connectors")
async def list_connectors():
    return {
        "actions": _action_dispatcher.describe() if _action_dispatcher is not None else [],
        "notifications": _outbox.describe() if _outbox is not None else [],
        "outbox": _outbox.stats if _outbox is not None else None,
    }


class NotifyRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(default="", max_length=20000)
    channels: list[str] | None = None  # default: every configured channel
    severity: Literal["low", "medium", "high", "critical"] | None = None
    dedup_key: str | None = Field(default=None, max_length=300)
    actor: str = Field(min_length=1, max_length=200)
    operation_ids: list[str] = Field(default_factory=list, max_length=50)


@router.post("/techops/incidents/{incident_id}/notify")
async def notify_incident(incident_id: str, body: NotifyRequest):
    """Send a provenance-tagged notification about an incident through the outbox: one delivery per
    (channel, dedup key), receipts returned. A repeated call with the same dedup key is a duplicate."""
    coord = _coordinator()
    if _outbox is None:
        raise HTTPException(503, "notification outbox not configured")
    try:
        incident = coord.get_incident(incident_id)
    except KeyError:
        raise HTTPException(404, f"unknown incident {incident_id}") from None
    from stag.connectors.notifications import Notification

    channels = body.channels or _outbox.channels
    unknown = [c for c in channels if _outbox.connector_for(c) is None]
    if unknown:
        raise HTTPException(422, f"no connector for channel(s) {unknown}; configured: {_outbox.channels}")
    dedup = body.dedup_key or f"{incident.id}:{body.actor}:{body.title}"
    queued = [
        _outbox.enqueue(Notification(channel=c, incident_id=incident.id, title=body.title, body=body.body, severity=body.severity or incident.severity.value,
                                     dedup_key=dedup, operation_ids=body.operation_ids, provenance={"actor": body.actor, "incident_version": incident.version}))
        for c in channels
    ]
    delivered = {r.notification_id: r for r in await _deliver_pending()}
    receipts = [delivered.get(q.notification_id, q) for q in queued]
    return {"receipts": [r.model_dump(mode="json") for r in receipts]}


@router.get("/techops/notifications")
async def list_notifications(incident_id: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    if _outbox is None:
        raise HTTPException(503, "notification outbox not configured")
    return {"notifications": _outbox.receipts(incident_id, limit=limit), "stats": _outbox.stats}


@router.post("/techops/actions/{operation_id}/applied")
async def action_applied(operation_id: str):
    """The destination confirmed the change was applied. Still requires independent verification."""
    _operation_or_404(operation_id)
    try:
        record = _action_ledger.mark_applied(operation_id)
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/verify")
async def verify_action(operation_id: str, body: VerifyActionRequest):
    """Record independent verification on the operation *and* bind it to the incident.

    Simulated evidence is refused by the live coordinator; a healthy verdict is required (and
    must be current) before the incident can be transitioned to RESOLVED.
    """
    record = _operation_or_404(operation_id)
    incident = _incident_for_operation(record)
    if body.evidence.simulated:
        raise HTTPException(409, "simulated verification evidence is not accepted by the live coordinator")
    try:
        result = VerificationResult(healthy=body.healthy, **body.evidence.model_dump())
        _action_ledger.verify(operation_id, body.healthy, result, body.actor)
        _coordinator().record_verification(incident.id, result, operation_id=operation_id, verified_by=body.actor)
    except (ValueError, Exception) as e:
        raise HTTPException(409, str(e)) from e
    return _action_ledger.view(operation_id).model_dump(mode="json")


# --- Crews ---


class CrewRunRequest(BaseModel):
    """Run a crew. Omit ``scenario`` to use the crew's bundled scenario."""

    scenario: Scenario | None = None
    auto_approve: bool = False


def _crew_or_404(name: str):
    try:
        return load_crew(name, CREWS_DIR)
    except FileNotFoundError:
        raise HTTPException(404, f"unknown crew {name}") from None


@router.get("/crews")
async def list_crews(domain: str | None = None):
    """Catalog of crews: roles, skills, workflow steps, KPIs."""
    crews = discover_crews(CREWS_DIR)
    if domain:
        crews = [c for c in crews if c.domain == domain]
    return {
        "crews": [
            {
                "name": c.name,
                "display_name": c.display_name,
                "domain": c.domain,
                "version": c.version,
                "purpose": c.purpose.strip(),
                "when_to_use": c.when_to_use.strip(),
                "roles": [
                    {"id": r.id, "title": r.title, "agent": r.agent, "human": r.human, "skills": r.skills}
                    for r in c.roles
                ],
                "workflow": [
                    {"step": s.step, "action": s.action.value, "role": s.role, "plan_step": s.plan_step,
                     "to": s.to.value if s.to else None,
                     "policy": s.policy.model_dump() if s.policy else None}
                    for s in c.workflow
                ],
                "triggers": [t.model_dump() for t in c.triggers],
                "kpis": [k.model_dump() for k in c.kpis],
                "guardrails": c.guardrails,
                "skills": c.skills,
            }
            for c in crews
        ]
    }


@router.get("/crews/{name}")
async def get_crew(name: str):
    crew = _crew_or_404(name)
    body = crew.model_dump(mode="json")
    try:
        body["scenario"] = load_scenario(crew).model_dump(mode="json")
    except FileNotFoundError:
        body["scenario"] = None
    return body


@router.post("/crews/{name}/run")
async def run_crew_endpoint(name: str, body: CrewRunRequest | None = None):
    """SIMULATION: run a crew against a scenario (bundled by default) with the deterministic executor.

    The run uses an isolated coordinator and ledger; it never touches live pipeline state, never
    calls a model or a remediation destination, and its approvals are simulated flags. Without
    ``auto_approve`` the run halts at the human gate.
    """
    crew = _crew_or_404(name)
    body = body or CrewRunRequest()
    scenario = body.scenario or load_scenario(crew)
    try:
        report = await asyncio.to_thread(run_crew, crew, scenario, body.auto_approve)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    data = report.model_dump(mode="json")
    data.update(
        {
            "mode": "simulation",
            "model_calls_avoided": report.model_calls_avoided,
            "resolved": report.resolved,
            "held": report.held,
            "failed_steps": report.failed_steps,
            "markdown": report.to_markdown(),
        }
    )
    return data


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
