"""PostgreSQL parity for the control store (R14).

Runs only when ``STAG_TEST_POSTGRES_DSN`` is set (CI starts a ``postgres`` service container).
Locally: ``docker run --rm -e POSTGRES_PASSWORD=stag -p 5432:5432 postgres:16`` then
``STAG_TEST_POSTGRES_DSN=postgresql://postgres:stag@localhost:5432/postgres pytest tests/test_control_store_postgres.py``.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytest.importorskip("psycopg")

DSN = os.environ.get("STAG_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="STAG_TEST_POSTGRES_DSN not set")


@pytest.fixture
def pg_store():
    from stag.techops.store import SqlControlStore

    store = SqlControlStore(DSN)
    # isolate each test: fresh tables
    store._exec("DROP TABLE IF EXISTS control_docs")
    store._exec("DROP TABLE IF EXISTS control_log")
    store.close()
    store = SqlControlStore(DSN)
    yield store
    store.close()


def test_postgres_contract_matches_sqlite(pg_store):
    from stag.techops.store import ConflictError

    key = uuid.uuid4().hex
    assert pg_store.insert_unique("c", key, {"name": "a"}, status="queued") is True
    assert pg_store.insert_unique("c", key, {"name": "b"}, status="queued") is False
    doc, v = pg_store.get("c", key)
    assert v == 1
    assert pg_store.put("c", key, {"name": "b"}, v) == 2
    with pytest.raises(ConflictError):
        pg_store.put("c", key, {"name": "lost"}, v)
    assert pg_store.claim("c", key, "w1", 300, from_statuses=("queued",)) is not None
    assert pg_store.claim("c", key, "w2", 300, from_statuses=("queued",)) is None
    assert pg_store.status_of("c", key) == "running"
    with pytest.raises(RuntimeError):
        with pg_store.transaction():
            pg_store.insert_unique("c", "rolled", {"x": 1})
            raise RuntimeError("boom")
    assert pg_store.get("c", "rolled") is None
    assert pg_store.append("log", {"q": 1}) >= 1
    assert len(pg_store.log("log")) == 1


def test_postgres_coordinator_and_ledger_roundtrip(pg_store):
    from stag.models import StreamEvent
    from stag.models.schemas import EventType, Severity, TriggerResult, TriggerType
    from stag.techops import ActionLedger, ActionState, IncidentCoordinator, IncidentState

    coord = IncidentCoordinator(store=pg_store)
    ledger = ActionLedger(store=pg_store)
    ev = StreamEvent(source="checkout-api", event_type=EventType.ANOMALY, severity=Severity.HIGH,
                     payload={"tool": "db-pool", "data_type": "timeout", "description": "pool timeout"},
                     metadata={"environment": "production", "service": "checkout-api"})
    reg = coord.register(ev)
    assert reg.accepted
    assert coord.register(ev).duplicate
    trig = TriggerResult(trigger_type=TriggerType.THRESHOLD, rule_name="r", fired=True, confidence=0.9, event_id=ev.id, reason="t")
    decision = coord.correlate(ev, trig, reg)
    req = decision.investigation_request
    assert coord.claim_investigation(req.id, "w1", 300) is not None
    assert coord.claim_investigation(req.id, "w2", 300) is None
    coord.complete_investigation(req.id, "w1", succeeded=True, summary="done")
    incident = coord.transition(decision.incident_id, IncidentState.TRIAGED, "op", "triaged")
    rec = ledger.prepare(incident.id, "restart", "checkout-api", {"replicas": 2})
    ledger.authorize(rec.operation_id, "oncall", incident.version)
    assert ledger.get(rec.operation_id).state is ActionState.AUTHORIZED
    # a second coordinator on the same database sees committed state
    other = IncidentCoordinator(store=pg_store)
    assert other.get_incident(incident.id).state is IncidentState.TRIAGED
    assert other.get_request(req.id).status == "succeeded"
