"""API tests for the TechOps endpoints using a stub pipeline (no ML dependencies)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stag.api import routes
from stag.models import StreamEvent
from stag.models.schemas import EventType, Severity, TriggerResult, TriggerType
from stag.techops import ActionLedger, IncidentCoordinator


class StubPipeline:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._running = True

        class _Gen:
            stats = {"has_client": False}

        self.generator = _Gen()


def _event():
    return StreamEvent(
        source="checkout-api",
        event_type=EventType.ANOMALY,
        severity=Severity.HIGH,
        payload={"tool": "db-pool", "data_type": "timeout"},
        metadata={"environment": "production", "service": "checkout-api"},
    )


@pytest.fixture()
def client():
    coord = IncidentCoordinator()
    ev = _event()
    coord.ingest(
        ev,
        TriggerResult(
            trigger_type=TriggerType.THRESHOLD, rule_name="pool_timeouts", fired=True,
            confidence=0.9, event_id=ev.id, reason="test",
        ),
    )
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    routes.set_dependencies(StubPipeline(coord), ws_manager=None, action_ledger=ActionLedger())
    return TestClient(app)


def test_list_and_get_incident(client):
    body = client.get("/api/techops/incidents").json()
    assert body["stats"]["total_incidents"] == 1
    inc = body["incidents"][0]
    detail = client.get(f"/api/techops/incidents/{inc['id']}").json()
    assert detail["incident"]["state"] == "detected"
    assert client.get("/api/techops/incidents/INC-NOPE").status_code == 404


def test_transition_is_explicit_and_validated(client):
    inc = client.get("/api/techops/incidents").json()["incidents"][0]
    bad = client.post(
        f"/api/techops/incidents/{inc['id']}/transition",
        json={"to": "resolved", "actor": "agent", "reason": "I think it is fixed"},
    )
    assert bad.status_code == 409
    ok = client.post(
        f"/api/techops/incidents/{inc['id']}/transition",
        json={"to": "triaged", "actor": "oncall@corp", "reason": "impact confirmed"},
    )
    assert ok.status_code == 200 and ok.json()["state"] == "triaged"


def test_action_ledger_flow_over_api(client):
    inc = client.get("/api/techops/incidents").json()["incidents"][0]
    body = {"incident_id": inc["id"], "plan_step": "restart_component", "target": "checkout-api",
            "parameters": {"replicas": 2}, "policy_version": "RB-17@v3"}
    prepared = client.post("/api/techops/actions/prepare", json=body).json()
    again = client.post("/api/techops/actions/prepare", json=body).json()
    assert prepared["operation_id"] == again["operation_id"]
    op = prepared["operation_id"]

    # a different policy version is a different operation
    other = client.post("/api/techops/actions/prepare", json={**body, "policy_version": "RB-17@v4"}).json()
    assert other["operation_id"] != op

    assert client.post(f"/api/techops/actions/{op}/dispatch").status_code == 409  # not authorized

    # approval is bound to the server's current incident version; a wrong expectation is rejected
    assert client.post(f"/api/techops/actions/{op}/authorize",
                       json={"actor": "oncall@corp", "expected_incident_version": inc["version"] + 7}).status_code == 409
    auth = client.post(f"/api/techops/actions/{op}/authorize", json={"actor": "oncall@corp"}).json()
    assert auth["state"] == "authorized" and auth["authorized_at_incident_version"] == inc["version"]

    # the incident moves (new evidence) → dispatch is refused by the ledger without any caller version
    client.post(f"/api/techops/incidents/{inc['id']}/transition",
                json={"to": "triaged", "actor": "oncall@corp", "reason": "impact confirmed"})
    stale = client.post(f"/api/techops/actions/{op}/dispatch")
    assert stale.status_code == 409 and "stale" in stale.json()["detail"].lower()

    client.post(f"/api/techops/actions/{op}/authorize", json={"actor": "oncall@corp"})
    disp = client.post(f"/api/techops/actions/{op}/dispatch")
    assert disp.status_code == 200 and disp.json()["state"] == "dispatched"

    unknown = client.post(f"/api/techops/actions/{op}/outcome-unknown").json()
    assert unknown["state"] == "reconciling"
    assert client.post(f"/api/techops/actions/{op}/dispatch").status_code == 409  # no blind retry
    rec = client.post(f"/api/techops/actions/{op}/reconcile", params={"target_state_observed": True}).json()
    assert rec["state"] == "applied" and rec["dispatch_attempts"] == 1
    ver = client.post(f"/api/techops/actions/{op}/verify",
                      json={"healthy": True, "evidence": {"error_rate": 0.001, "window_s": 600}}).json()
    assert ver["state"] == "verified" and ver["verification"]["healthy"] is True

    actions = client.get("/api/techops/actions").json()
    assert actions["stats"]["operations"] == 2
    assert client.get(f"/api/techops/incidents/{inc['id']}").json()["actions"][0]["operation_id"] in (op, other["operation_id"])


def test_actions_require_an_existing_open_incident(client):
    assert client.post("/api/techops/actions/prepare",
                       json={"incident_id": "INC-NOPE", "plan_step": "restart", "target": "x"}).status_code == 404
    assert client.post("/api/techops/actions/OP-nope/authorize", json={"actor": "a"}).status_code == 404
    assert client.post("/api/techops/actions/OP-nope/dispatch").status_code == 404


def test_health_reports_component_readiness(client):
    body = client.get("/api/health").json()
    assert body["components"]["techops_coordinator"] == "enabled"
    assert body["status"] in ("ok", "degraded") and body["mode"] == "local-demo"


def test_query_limits_are_bounded(client):
    assert client.get("/api/events", params={"limit": 0}).status_code == 422
    assert client.get("/api/knowledge/search", params={"q": "", "top_k": 5}).status_code == 422


def test_coordinator_disabled_returns_404():
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")

    class NoCoord:
        coordinator = None
        _running = True

    routes.set_dependencies(NoCoord(), ws_manager=None)
    assert TestClient(app).get("/api/techops/incidents").status_code == 404


def test_crews_catalog_and_dry_run(client):
    crews = client.get("/api/crews").json()["crews"]
    assert {c["name"] for c in crews} >= {"techops-incident-triage", "customer-care", "data-ops"}
    one = client.get("/api/crews/techops-incident-triage").json()
    assert one["scenario"]["events"]
    assert client.get("/api/crews/nope").status_code == 404

    held = client.post("/api/crews/techops-incident-triage/run", json={}).json()
    assert held["held"] == 2 and held["resolved"] == 0 and held["mode"] == "simulation"
    done = client.post("/api/crews/techops-incident-triage/run", json={"auto_approve": True}).json()
    assert done["resolved"] == 2 and "model_calls_avoided" in done and "[SIMULATION]" in done["markdown"]
    # invalid scenario input is a 422, not a 500
    bad = client.post("/api/crews/techops-incident-triage/run",
                      json={"scenario": {"name": "x", "events": [{"source": "s", "event_type": "nope"}]}})
    assert bad.status_code == 422
    # dry runs never touch the live coordinator
    assert client.get("/api/techops/incidents").json()["stats"]["total_incidents"] == 1
