"""FastAPI REST and WebSocket routes."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from stag.crews import CREWS_DIR, Scenario, discover_crews, load_crew, load_scenario, run_crew
from stag.models import StreamEvent
from stag.techops import ActionLedger, IncidentState

router = APIRouter()

# These will be set by main.py during app startup
_pipeline = None
_ws_manager = None
_action_ledger: ActionLedger | None = None


def set_dependencies(pipeline, ws_manager, action_ledger: ActionLedger | None = None):
    global _pipeline, _ws_manager, _action_ledger
    _pipeline = pipeline
    _ws_manager = ws_manager
    _action_ledger = action_ledger or ActionLedger()


# --- Health & Stats ---

@router.get("/health")
async def health():
    """Liveness plus component readiness. ``status`` is ``degraded`` when a required part is down."""
    components = {
        "pipeline": "ready" if _pipeline is not None and getattr(_pipeline, "_running", False) else "not_running",
        "generator": (
            "llm" if _pipeline is not None and getattr(getattr(_pipeline, "generator", None), "stats", {}).get("has_client")
            else "fallback"
        ),
        "techops_coordinator": "enabled" if _pipeline is not None and getattr(_pipeline, "coordinator", None) else "disabled",
        "websocket_clients": _ws_manager.connection_count if _ws_manager is not None else 0,
    }
    status = "ok" if components["pipeline"] == "ready" else "degraded"
    return {"status": status, "version": "0.3.0", "mode": "local-demo", "components": components}


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


class VerifyActionRequest(BaseModel):
    healthy: bool | None
    evidence: dict = Field(default_factory=dict)
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
async def list_investigations():
    coord = _coordinator()
    return {"investigations": [r.model_dump(mode="json") for r in coord.investigation_requests]}


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
    return record.model_dump(mode="json")


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

    There is no caller-supplied version: freshness is decided from server state.
    """
    record = _operation_or_404(operation_id)
    incident = _incident_for_operation(record)
    if not incident.is_open:
        raise HTTPException(409, f"incident {incident.id} is {incident.state.value}")
    try:
        record = _action_ledger.dispatch(operation_id, actor="api", current_incident_version=incident.version)
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/outcome-unknown")
async def action_outcome_unknown(operation_id: str):
    _operation_or_404(operation_id)
    try:
        record = _action_ledger.mark_outcome_unknown(operation_id)
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/reconcile")
async def reconcile_action(operation_id: str, target_state_observed: bool):
    _operation_or_404(operation_id)
    try:
        record = _action_ledger.reconcile(operation_id, target_state_observed)
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


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
    _operation_or_404(operation_id)
    try:
        record = _action_ledger.verify(operation_id, body.healthy, body.evidence, body.actor)
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


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
