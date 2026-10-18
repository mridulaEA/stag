"""FastAPI REST and WebSocket routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
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
    return {"status": "ok", "version": "0.3.0"}


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
    incident_id: str
    plan_step: str
    target: str
    parameters: dict = Field(default_factory=dict)
    policy_version: str = "unversioned"
    preconditions: list[str] = Field(default_factory=list)


class AuthorizeActionRequest(BaseModel):
    actor: str
    incident_version: int
    note: str = ""


class VerifyActionRequest(BaseModel):
    healthy: bool
    evidence: dict = Field(default_factory=dict)
    actor: str = "verifier"


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
    return {"actions": [a.model_dump(mode="json") for a in _action_ledger.records], "stats": _action_ledger.stats}


@router.post("/techops/actions/prepare")
async def prepare_action(body: PrepareActionRequest):
    """Create (or return) the durable operation record. Same plan step → same operation id."""
    record = _action_ledger.prepare(**body.model_dump())
    return record.model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/authorize")
async def authorize_action(operation_id: str, body: AuthorizeActionRequest):
    try:
        record = _action_ledger.authorize(operation_id, body.actor, body.incident_version, body.note)
    except KeyError:
        raise HTTPException(404, f"unknown operation {operation_id}") from None
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/dispatch")
async def dispatch_action(operation_id: str, current_incident_version: int | None = None):
    """Revalidate the approval against the current incident version, then dispatch."""
    try:
        if current_incident_version is not None:
            record = _action_ledger.revalidate(operation_id, current_incident_version)
            if record.state.value == "stale":
                raise HTTPException(409, "approval is stale; re-authorization required")
        record = _action_ledger.dispatch(operation_id)
    except KeyError:
        raise HTTPException(404, f"unknown operation {operation_id}") from None
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/outcome-unknown")
async def action_outcome_unknown(operation_id: str):
    try:
        record = _action_ledger.mark_outcome_unknown(operation_id)
    except KeyError:
        raise HTTPException(404, f"unknown operation {operation_id}") from None
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/reconcile")
async def reconcile_action(operation_id: str, target_state_observed: bool):
    try:
        record = _action_ledger.reconcile(operation_id, target_state_observed)
    except KeyError:
        raise HTTPException(404, f"unknown operation {operation_id}") from None
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return record.model_dump(mode="json")


@router.post("/techops/actions/{operation_id}/verify")
async def verify_action(operation_id: str, body: VerifyActionRequest):
    try:
        record = _action_ledger.verify(operation_id, body.healthy, body.evidence, body.actor)
    except KeyError:
        raise HTTPException(404, f"unknown operation {operation_id}") from None
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
    """Run a crew end-to-end against a scenario (bundled by default) using the deterministic executor.

    This is a dry run on an isolated coordinator/ledger: it never touches the live pipeline state.
    Without ``auto_approve`` the run halts at the human gate, which is the expected production shape.
    """
    crew = _crew_or_404(name)
    body = body or CrewRunRequest()
    scenario = body.scenario or load_scenario(crew)
    report = run_crew(crew, scenario, auto_approve=body.auto_approve)
    data = report.model_dump(mode="json")
    data.update(
        {"model_calls_avoided": report.model_calls_avoided, "resolved": report.resolved, "held": report.held,
         "markdown": report.to_markdown()}
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
