"""Durable control store (R14) and investigation work lifecycle (R16).

Every behavioural test runs against both ``InMemoryControlStore`` and a file-backed
``SqlControlStore`` (SQLite) so the two cannot drift. PostgreSQL parity lives in
``test_control_store_postgres.py`` and runs when ``STAG_TEST_POSTGRES_DSN`` is set (CI service
container). The two-process test uses real OS processes on one SQLite file: exactly one of N racing
workers may commit an identical registration / request / operation.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from stag.models import StreamEvent
from stag.models.schemas import EventType, Severity, TriggerResult, TriggerType
from stag.techops import (
    ActionLedger,
    ActionState,
    IncidentCoordinator,
    IncidentState,
    VerificationResult,
)
from stag.techops.store import (
    ConflictError,
    InMemoryControlStore,
    SqlControlStore,
    control_store_from_url,
)


class Doc(BaseModel):
    name: str
    n: int = 0


def _event(**kwargs) -> StreamEvent:
    defaults = {
        "source": "checkout-api",
        "event_type": EventType.ANOMALY,
        "severity": Severity.HIGH,
        "payload": {"tool": "db-pool", "data_type": "timeout", "description": "connection pool timeout"},
        "metadata": {"environment": "production", "service": "checkout-api"},
    }
    defaults.update(kwargs)
    return StreamEvent(**defaults)


def _trigger(event: StreamEvent) -> TriggerResult:
    return TriggerResult(trigger_type=TriggerType.THRESHOLD, rule_name="pool_timeouts", fired=True, confidence=0.9, event_id=event.id, reason="test")


POSTGRES_DSN = os.environ.get("STAG_TEST_POSTGRES_DSN")


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def store(request, tmp_path):
    """Same contract on every backend. PostgreSQL runs when ``STAG_TEST_POSTGRES_DSN`` is set."""
    if request.param == "memory":
        yield InMemoryControlStore()
    elif request.param == "sqlite":
        s = SqlControlStore(f"sqlite:///{tmp_path / 'control.sqlite'}")
        yield s
        s.close()
    else:
        if not POSTGRES_DSN:
            pytest.skip("STAG_TEST_POSTGRES_DSN not set")
        pytest.importorskip("psycopg")
        s = SqlControlStore(POSTGRES_DSN)
        s._exec("TRUNCATE control_docs, control_log")
        yield s
        s.close()


# --------------------------------------------------------------------------- store contract


class TestStoreContract:
    def test_insert_unique_is_first_writer_wins(self, store):
        assert store.insert_unique("c", "k", Doc(name="a"), status="queued") is True
        assert store.insert_unique("c", "k", Doc(name="b"), status="queued") is False
        doc, version = store.get("c", "k")
        assert store.materialize(doc, Doc).name == "a"
        assert version == 1
        assert store.count("c") == 1

    def test_put_is_compare_and_swap(self, store):
        store.insert_unique("c", "k", Doc(name="a"))
        _, v1 = store.get("c", "k")
        v2 = store.put("c", "k", Doc(name="b"), v1)
        assert v2 == v1 + 1
        with pytest.raises(ConflictError):
            store.put("c", "k", Doc(name="lost update"), v1)
        assert store.materialize(store.get("c", "k")[0], Doc).name == "b"

    def test_put_without_expected_version_creates_or_replaces(self, store):
        assert store.put("c", "new", Doc(name="x"), None) == 1
        assert store.put("c", "new", Doc(name="y"), None) == 2
        assert store.materialize(store.get("c", "new")[0], Doc).name == "y"

    def test_list_filters_by_status_and_scope(self, store):
        store.insert_unique("c", "1", Doc(name="1"), status="open", scope="svc-a")
        store.insert_unique("c", "2", Doc(name="2"), status="open", scope="svc-b")
        store.insert_unique("c", "3", Doc(name="3"), status="resolved", scope="svc-a")
        assert {k for k, _, _ in store.list("c", status="open")} == {"1", "2"}
        assert {k for k, _, _ in store.list("c", scope="svc-a")} == {"1", "3"}
        assert [k for k, _, _ in store.list("c", status="open", scope="svc-a")] == ["1"]
        assert len(store.list("c", limit=2)) == 2
        assert store.delete("c", "2") is True
        assert store.delete("c", "2") is False
        assert store.get("c", "2") is None

    def test_append_only_log_keeps_order(self, store):
        ids = [store.append("quarantine", Doc(name=f"q{i}")) for i in range(3)]
        assert ids == sorted(ids)
        assert [store.materialize(d, Doc).name for d in store.log("quarantine")] == ["q0", "q1", "q2"]

    def test_purge_older_than_bounds_retention(self, store):
        store.insert_unique("obs", "old", Doc(name="old"))
        assert store.purge_older_than("obs", datetime.now(UTC) - timedelta(days=1)) == 0
        assert store.purge_older_than("obs", datetime.now(UTC) + timedelta(seconds=1)) == 1
        assert store.count("obs") == 0

    def test_transaction_rolls_back_every_write(self, store):
        store.insert_unique("c", "kept", Doc(name="kept"))
        _, v = store.get("c", "kept")
        with pytest.raises(RuntimeError):
            with store.transaction():
                store.insert_unique("c", "new", Doc(name="new"))
                store.put("c", "kept", Doc(name="changed"), v)
                store.append("log", Doc(name="entry"))
                raise RuntimeError("boom")
        assert store.get("c", "new") is None
        assert store.materialize(store.get("c", "kept")[0], Doc).name == "kept"
        assert store.get("c", "kept")[1] == v
        assert store.log("log") == []

    def test_nested_transaction_commits_once(self, store):
        with store.transaction():
            store.insert_unique("c", "a", Doc(name="a"))
            with store.transaction():
                store.insert_unique("c", "b", Doc(name="b"))
        assert store.count("c") == 2

    def test_claim_leases_to_exactly_one_owner(self, store):
        store.insert_unique("req", "r1", Doc(name="work"), status="queued")
        first = store.claim("req", "r1", "worker-a", 300, from_statuses=("queued", "failed"))
        assert first is not None
        assert store.status_of("req", "r1") == "running"
        # second worker: lease held and not expired → refused
        assert store.claim("req", "r1", "worker-b", 300, from_statuses=("queued", "failed")) is None
        # unknown key → refused
        assert store.claim("req", "missing", "worker-b", 300, from_statuses=("queued",)) is None

    def test_expired_lease_is_reclaimable(self, store):
        store.insert_unique("req", "r1", Doc(name="work"), status="queued")
        assert store.claim("req", "r1", "worker-a", 0, from_statuses=("queued",)) is not None
        # lease_seconds=0 → lease_until is now; strictly-past check needs a moment
        import time

        time.sleep(0.01)
        reclaimed = store.claim("req", "r1", "worker-b", 300, from_statuses=("queued",))
        assert reclaimed is not None

    def test_finished_status_is_not_claimable(self, store):
        store.insert_unique("req", "r1", Doc(name="work"), status="queued")
        _, v = store.get("req", "r1")
        store.put("req", "r1", Doc(name="work", n=1), v, status="succeeded")
        assert store.claim("req", "r1", "worker-b", 300, from_statuses=("queued", "failed")) is None


# --------------------------------------------------------------------------- restart durability (R14)


class TestDurability:
    def test_coordinator_state_survives_restart(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'c.sqlite'}"
        store = SqlControlStore(url)
        coord = IncidentCoordinator(store=store)
        ev = _event()
        reg = coord.register(ev)
        decision = coord.correlate(ev, _trigger(ev), reg)
        incident_id = decision.incident_id
        request_id = decision.investigation_request.id
        coord.transition(incident_id, IncidentState.TRIAGED, "op", "looked")
        store.close()

        # New process-equivalent: fresh store + coordinator on the same file.
        store2 = SqlControlStore(url)
        coord2 = IncidentCoordinator(store=store2)
        assert coord2.has_incident(incident_id)
        incident = coord2.get_incident(incident_id)
        assert incident.state is IncidentState.TRIAGED
        assert incident.version >= 2
        # The same delivery after restart is still a duplicate, never a second incident.
        again = coord2.register(_event(id=ev.id, timestamp=ev.timestamp))
        assert again.accepted is False and again.duplicate is True
        assert len(coord2.open_incidents) == 1
        # The investigation request is still queued, with its evidence snapshot.
        assert coord2.get_request(request_id).status == "queued"
        assert [r.id for r in coord2.pending_investigations()] == [request_id]
        store2.close()

    def test_ledger_state_survives_restart_and_shares_the_incident_store(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'c.sqlite'}"
        store = SqlControlStore(url)
        coord = IncidentCoordinator(store=store)
        ledger = ActionLedger(store=store)
        ev = _event()
        decision = coord.correlate(ev, _trigger(ev), coord.register(ev))
        incident = coord.get_incident(decision.incident_id)
        rec = ledger.prepare(incident.id, "restart", "checkout-api", {"replicas": 2})
        ledger.authorize(rec.operation_id, "oncall", incident.version)
        store.close()

        store2 = SqlControlStore(url)
        ledger2 = ActionLedger(store=store2)
        loaded = ledger2.get(rec.operation_id)
        assert loaded.state is ActionState.AUTHORIZED
        assert loaded.terms_hash == rec.terms_hash
        # Preparing the same terms again after restart returns the same operation id (no new op for a retry).
        assert ledger2.prepare(incident.id, "restart", "checkout-api", {"replicas": 2}).operation_id == rec.operation_id
        assert [a.operation_id for a in ledger2.for_incident(incident.id)] == [rec.operation_id]
        store2.close()

    def test_quarantine_log_is_durable(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'c.sqlite'}"
        store = SqlControlStore(url)
        coord = IncidentCoordinator(store=store)
        ev = _event()
        coord.register(ev)
        conflicting = _event(id=ev.id, timestamp=ev.timestamp, payload={**ev.payload, "description": "different"})
        res = coord.register(conflicting)
        assert res.conflicting is True
        store.close()
        coord2 = IncidentCoordinator(store=SqlControlStore(url))
        assert len(coord2.quarantine) == 1

    def test_factory_selects_backend(self, tmp_path):
        assert isinstance(control_store_from_url(None), InMemoryControlStore)
        assert isinstance(control_store_from_url("memory://"), InMemoryControlStore)
        sql = control_store_from_url(f"sqlite:///{tmp_path / 'x.sqlite'}")
        assert isinstance(sql, SqlControlStore)
        sql.close()
        with pytest.raises(ValueError):
            control_store_from_url("mysql://nope")


# --------------------------------------------------------------------------- investigation lifecycle (R16)


class TestInvestigationLifecycle:
    def _request(self, store):
        coord = IncidentCoordinator(store=store)
        ev = _event()
        decision = coord.correlate(ev, _trigger(ev), coord.register(ev))
        return coord, decision.investigation_request

    def test_request_is_queued_then_leased_to_one_worker(self, store):
        coord, req = self._request(store)
        assert req.status == "queued" and req.attempts == 0
        claimed = coord.claim_investigation(req.id, "worker-a", 300)
        assert claimed is not None
        assert claimed.status == "running" and claimed.attempts == 1 and claimed.lease_owner == "worker-a"
        assert coord.claim_investigation(req.id, "worker-b", 300) is None
        assert coord.pending_investigations() == []

    def test_completion_requires_the_lease_holder(self, store):
        coord, req = self._request(store)
        coord.claim_investigation(req.id, "worker-a", 300)
        with pytest.raises(ConflictError):
            coord.complete_investigation(req.id, "worker-b", succeeded=True, summary="not mine")
        done = coord.complete_investigation(req.id, "worker-a", succeeded=True, summary="root cause: pool exhausted")
        assert done.status == "succeeded" and done.lease_owner is None and done.completed_at is not None
        # finished work cannot be claimed or completed again
        assert coord.claim_investigation(req.id, "worker-a", 300) is None
        with pytest.raises(ConflictError):
            coord.complete_investigation(req.id, "worker-a", succeeded=True)

    def test_failed_work_is_retryable_and_counts_attempts(self, store):
        coord, req = self._request(store)
        coord.claim_investigation(req.id, "worker-a", 300)
        failed = coord.complete_investigation(req.id, "worker-a", succeeded=False, summary="provider timeout")
        assert failed.status == "failed"
        assert [r.id for r in coord.pending_investigations()] == [req.id]
        retry = coord.claim_investigation(req.id, "worker-b", 300)
        assert retry is not None and retry.attempts == 2

    def test_crashed_worker_lease_expires(self, store):
        coord, req = self._request(store)
        assert coord.claim_investigation(req.id, "worker-crashed", 0) is not None
        import time

        time.sleep(0.01)
        recovered = coord.claim_investigation(req.id, "worker-b", 300)
        assert recovered is not None and recovered.lease_owner == "worker-b" and recovered.attempts == 2

    def test_stale_request_is_visible(self, store):
        coord, req = self._request(store)
        assert coord.is_request_stale(req.id) is False
        coord.transition(req.incident_id, IncidentState.TRIAGED, "op", "moved on")
        assert coord.is_request_stale(req.id) is True
        with pytest.raises(KeyError):
            coord.get_request("INV-missing")

    def test_record_verification_still_needs_lifecycle(self, store):
        # the lifecycle never resolves incidents by itself (CLAUDE.md non-negotiable)
        coord, req = self._request(store)
        coord.claim_investigation(req.id, "w", 300)
        coord.complete_investigation(req.id, "w", succeeded=True, summary="fixed")
        incident = coord.get_incident(req.incident_id)
        assert incident.state is IncidentState.DETECTED
        for st in (IncidentState.TRIAGED, IncidentState.INVESTIGATING, IncidentState.VERIFYING):
            coord.transition(incident.id, st, "op", "path")
        coord.record_verification(incident.id, VerificationResult(healthy=True, source="probe", window="15m", signals=["p99"]))
        assert coord.transition(incident.id, IncidentState.RESOLVED, "op", "verified").state is IncidentState.RESOLVED


# --------------------------------------------------------------------------- two processes, one SQLite file


def _race_register(args):
    url, event_json, start_at = args
    import time

    from stag.models import StreamEvent
    from stag.techops import ActionLedger, IncidentCoordinator
    from stag.techops.store import SqlControlStore

    event = StreamEvent.model_validate_json(event_json)
    store = SqlControlStore(url)
    coord = IncidentCoordinator(store=store)
    ledger = ActionLedger(store=store)
    while time.time() < start_at:
        time.sleep(0.001)
    reg = coord.register(event)
    trig = TriggerResult(trigger_type=TriggerType.THRESHOLD, rule_name="r", fired=True, confidence=0.9, event_id=event.id, reason="t")
    decision = coord.correlate(event, trig, reg) if reg.accepted else None
    incident_id = decision.incident_id if decision else (reg.prior_incident_id or next(iter(coord.open_incidents)).id)
    op = ledger.prepare(incident_id, "restart", "checkout-api", {"replicas": 2})
    claimed = coord.claim_investigation(decision.investigation_request.id, f"w{start_at}", 300) if decision and decision.investigation_request else None
    store.close()
    return {
        "accepted": reg.accepted,
        "duplicate": reg.duplicate,
        "incident_id": incident_id,
        "request_id": decision.investigation_request.id if decision and decision.investigation_request else None,
        "op": op.operation_id,
        "claimed": claimed is not None,
    }


@pytest.mark.skipif(sys.platform == "win32", reason="fork-based multiprocessing")
def test_two_processes_one_registration_one_incident_one_operation(tmp_path):
    import time

    url = f"sqlite:///{tmp_path / 'race.sqlite'}"
    SqlControlStore(url).close()  # create schema once
    ev = _event()
    start_at = time.time() + 0.5
    ctx = mp.get_context("spawn")
    with ctx.Pool(4) as pool:
        results = pool.map(_race_register, [(url, ev.model_dump_json(), start_at)] * 4)

    accepted = [r for r in results if r["accepted"]]
    assert len(accepted) == 1, results
    assert all(r["duplicate"] for r in results if not r["accepted"])
    assert len({r["incident_id"] for r in results}) == 1
    assert len({r["op"] for r in results}) == 1
    assert sum(1 for r in results if r["claimed"]) == 1

    store = SqlControlStore(url)
    coord = IncidentCoordinator(store=store)
    assert len(coord.incidents) == 1
    assert len(coord.investigation_requests) == 1
    assert coord.investigation_requests[0].status == "running"
    assert ActionLedger(store=store).stats["operations"] == 1
    store.close()
