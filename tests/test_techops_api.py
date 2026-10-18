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
    prepared = client.post(
        "/api/techops/actions/prepare",
        json={"incident_id": inc["id"], "plan_step": "restart_component", "target": "checkout-api",
              "parameters": {"replicas": 2}, "policy_version": "RB-17@v3"},
    ).json()
    again = client.post(
        "/api/techops/actions/prepare",
        json={"incident_id": inc["id"], "plan_step": "restart_component", "target": "checkout-api",
              "parameters": {"replicas": 2}},
    ).json()
    assert prepared["operation_id"] == again["operation_id"]
    op = prepared["operation_id"]

    assert client.post(f"/api/techops/actions/{op}/dispatch").status_code == 409  # not authorized

    auth = client.post(f"/api/techops/actions/{op}/authorize",
                       json={"actor": "oncall@corp", "incident_version": inc["version"]})
    assert auth.json()["state"] == "authorized"

    stale = client.post(f"/api/techops/actions/{op}/dispatch",
                        params={"current_incident_version": inc["version"] + 3})
    assert stale.status_code == 409

    client.post(f"/api/techops/actions/{op}/authorize",
                json={"actor": "oncall@corp", "incident_version": inc["version"] + 3})
    disp = client.post(f"/api/techops/actions/{op}/dispatch",
                       params={"current_incident_version": inc["version"] + 3})
    assert disp.json()["state"] == "dispatched"

    unknown = client.post(f"/api/techops/actions/{op}/outcome-unknown").json()
    assert unknown["state"] == "reconciling"
    rec = client.post(f"/api/techops/actions/{op}/reconcile", params={"target_state_observed": True}).json()
    assert rec["state"] == "verified" and rec["dispatch_attempts"] == 1

    actions = client.get("/api/techops/actions").json()
    assert actions["stats"]["operations"] == 1
    assert client.get(f"/api/techops/incidents/{inc['id']}").json()["actions"][0]["operation_id"] == op


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
    assert held["held"] == 2 and held["resolved"] == 0
    done = client.post("/api/crews/techops-incident-triage/run", json={"auto_approve": True}).json()
    assert done["resolved"] == 2 and "model_calls_avoided" in done and done["markdown"].startswith("# Crew run")
    # dry runs never touch the live coordinator
    assert client.get("/api/techops/incidents").json()["stats"]["total_incidents"] == 1
