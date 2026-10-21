"""Connector contracts (R05 live path, R23): action dispatch bound to the ledger, idempotent
destinations, recorded adapters, and the durable deduplicated notification outbox."""

from __future__ import annotations

import json

import pytest

from stag.config import Settings
from stag.connectors import (
    ActionDispatcher,
    ConnectorTimeout,
    DashboardConnector,
    EmailConnector,
    FakeNotificationConnector,
    FakeTicketingConnector,
    HttpResponse,
    JiraConnector,
    NoConnectorError,
    Notification,
    NotificationOutbox,
    PagerDutyConnector,
    RecordedSmtp,
    RecordedTransport,
    ServiceNowConnector,
    SlackConnector,
    build_action_connectors,
    build_notification_connectors,
)
from stag.models import StreamEvent
from stag.models.schemas import EventType, Severity, TriggerResult, TriggerType
from stag.techops import ActionLedger, ActionState, IncidentCoordinator, InvalidActionTransition
from stag.techops.store import InMemoryControlStore, SqlControlStore

SECRET_TOKEN = "tok-super-secret-9f8e7d"
SECRET_KEY = "pd-routing-key-aa11bb22"


def _event(**kw) -> StreamEvent:
    base = dict(source="checkout-api", event_type=EventType.ANOMALY, severity=Severity.HIGH,
                payload={"tool": "db-pool", "data_type": "timeout", "description": "pool timeout"},
                metadata={"environment": "production", "service": "checkout-api"})
    base.update(kw)
    return StreamEvent(**base)


def _incident_and_ledger(store=None):
    coord = IncidentCoordinator(store=store)
    ledger = ActionLedger(store=store)
    ev = _event()
    trig = TriggerResult(trigger_type=TriggerType.THRESHOLD, rule_name="pool_timeouts", fired=True, confidence=0.9, event_id=ev.id, reason="t")
    decision = coord.correlate(ev, trig, coord.register(ev))
    incident = coord.get_incident(decision.incident_id)
    return coord, ledger, incident


def _authorized_op(ledger, incident, **params):
    rec = ledger.prepare(incident.id, "restart", "checkout-api", params or {"replicas": 2})
    ledger.authorize(rec.operation_id, "oncall", incident.version)
    return rec.operation_id


# --------------------------------------------------------------------------- dispatcher × ledger


class TestActionDispatcher:
    def test_applied_receipt_moves_ledger_to_applied_with_external_id(self):
        _, ledger, incident = _incident_and_ledger()
        fake = FakeTicketingConnector()
        dispatcher = ActionDispatcher(ledger, [fake])
        op = _authorized_op(ledger, incident)
        record, receipt = dispatcher.execute(op, "api", incident.version)
        assert record.state is ActionState.APPLIED
        assert receipt.status == "applied" and receipt.external_id == "FAKE-1" and receipt.simulated is True
        assert record.verification["application_receipt"]["external_id"] == "FAKE-1"
        assert record.dispatch_attempts == 1
        # applied is not recovery: a second dispatch is refused by the ledger, the destination untouched
        with pytest.raises(InvalidActionTransition):
            dispatcher.execute(op, "api", incident.version)
        assert fake.dispatches == [op]

    def test_no_connector_means_no_ledger_change(self):
        _, ledger, incident = _incident_and_ledger()
        dispatcher = ActionDispatcher(ledger, [FakeTicketingConnector(targets=["other-service"])])
        op = _authorized_op(ledger, incident)
        with pytest.raises(NoConnectorError):
            dispatcher.execute(op, "api", incident.version)
        rec = ledger.get(op)
        assert rec.state is ActionState.AUTHORIZED and rec.dispatch_attempts == 0

    def test_lost_response_becomes_outcome_unknown_and_reconcile_finds_the_effect_without_a_second_ticket(self):
        _, ledger, incident = _incident_and_ledger()
        op = _authorized_op(ledger, incident)
        fake = FakeTicketingConnector(fail={op: "timeout_once"})
        dispatcher = ActionDispatcher(ledger, [fake])
        record, receipt = dispatcher.execute(op, "api", incident.version)
        assert receipt.status == "unknown" and record.state is ActionState.RECONCILING
        assert len(fake.tickets) == 1, "the effect happened even though the response was lost"
        record, observed = dispatcher.reconcile(op)
        assert observed is True and record.state is ActionState.APPLIED
        assert len(fake.tickets) == 1 and fake.dispatches == [op], "reconciliation observes; it never re-dispatches"

    def test_effect_absent_after_unknown_outcome_permits_one_explicit_retry_which_reuses_the_operation(self):
        _, ledger, incident = _incident_and_ledger()
        op = _authorized_op(ledger, incident)
        fake = FakeTicketingConnector()
        dispatcher = ActionDispatcher(ledger, [fake])
        # simulate a transport failure *before* the destination acted
        fake.dispatch = lambda record: (_ for _ in ()).throw(ConnectorTimeout("connection reset"))  # type: ignore[assignment]
        record, receipt = dispatcher.execute(op, "api", incident.version)
        assert record.state is ActionState.RECONCILING and receipt.status == "unknown"
        record, observed = dispatcher.reconcile(op)
        assert observed is False and record.state is ActionState.RETRY_READY
        fake.dispatch = FakeTicketingConnector.dispatch.__get__(fake)  # restore
        record, receipt = dispatcher.execute(op, "api", incident.version)  # explicit retry, same operation id
        assert record.state is ActionState.APPLIED and record.dispatch_attempts == 2
        assert receipt.operation_id == op and list(fake.tickets) == [op]
        with pytest.raises(InvalidActionTransition):  # applied is not dispatchable; nothing runs twice
            ledger.dispatch(op, "api", incident.version)

    def test_unobservable_destination_keeps_the_operation_reconciling(self):
        _, ledger, incident = _incident_and_ledger()
        op = _authorized_op(ledger, incident)
        fake = FakeTicketingConnector(fail={op: "timeout"})
        dispatcher = ActionDispatcher(ledger, [fake])
        dispatcher.execute(op, "api", incident.version)
        fake.fail[op] = "unknown"
        record, observed = dispatcher.reconcile(op)
        assert observed is None and record.state is ActionState.RECONCILING

    def test_destination_rejection_is_terminal_failure_not_retry(self):
        _, ledger, incident = _incident_and_ledger()
        op = _authorized_op(ledger, incident)
        dispatcher = ActionDispatcher(ledger, [FakeTicketingConnector(fail={op: "reject"})])
        record, receipt = dispatcher.execute(op, "api", incident.version)
        assert receipt.status == "rejected" and record.state is ActionState.FAILED
        assert "rejected" in record.verification["failure_reason"]
        with pytest.raises(InvalidActionTransition):
            ledger.dispatch(op, "api", incident.version)

    def test_stale_approval_never_reaches_the_destination(self):
        coord, ledger, incident = _incident_and_ledger()
        op = _authorized_op(ledger, incident)
        fake = FakeTicketingConnector()
        dispatcher = ActionDispatcher(ledger, [fake])
        from stag.techops import IncidentState

        moved = coord.transition(incident.id, IncidentState.TRIAGED, "op", "evidence changed")
        with pytest.raises(InvalidActionTransition):
            dispatcher.execute(op, "api", moved.version)
        assert fake.dispatches == [] and ledger.get(op).state is ActionState.STALE

    def test_durable_store_keeps_receipts_across_restart(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'c.sqlite'}"
        store = SqlControlStore(url)
        _, ledger, incident = _incident_and_ledger(store)
        op = _authorized_op(ledger, incident)
        ActionDispatcher(ledger, [FakeTicketingConnector()]).execute(op, "api", incident.version)
        store.close()
        reloaded = ActionLedger(store=SqlControlStore(url)).get(op)
        assert reloaded.state is ActionState.APPLIED and reloaded.verification["application_receipt"]["external_id"] == "FAKE-1"


# --------------------------------------------------------------------------- recorded ticketing adapters


def _record():
    _, ledger, incident = _incident_and_ledger()
    op = _authorized_op(ledger, incident, replicas=3)
    return ledger.get(op)


class TestJiraAdapter:
    def test_searches_by_operation_label_before_creating_and_returns_the_key(self):
        transport = RecordedTransport([HttpResponse(200, {"issues": []}), HttpResponse(201, {"id": "10001", "key": "OPS-42"})])
        jira = JiraConnector("https://example.atlassian.net/", "ops@example.com", SECRET_TOKEN, "OPS", transport=transport)
        record = _record()
        receipt = jira.dispatch(record)
        assert receipt.status == "applied" and receipt.external_id == "OPS-42" and receipt.external_url.endswith("/browse/OPS-42")
        search, create = transport.calls
        assert search.method == "GET" and search.url.endswith("/rest/api/3/search")
        assert JiraConnector.label(record.operation_id) in search.params["jql"]
        assert create.method == "POST" and create.url.endswith("/rest/api/3/issue")
        fields = create.json["fields"]
        assert fields["project"] == {"key": "OPS"} and fields["labels"] == [JiraConnector.label(record.operation_id)]
        assert record.operation_id in json.dumps(create.json), "the operation id is written into the ticket"
        assert create.headers["Authorization"].startswith("Basic ")
        assert SECRET_TOKEN not in json.dumps(receipt.model_dump(mode="json")) and SECRET_TOKEN not in json.dumps(jira.describe())

    def test_existing_issue_is_reused_on_retry(self):
        transport = RecordedTransport([HttpResponse(200, {"issues": [{"key": "OPS-7", "fields": {"status": {"name": "Open"}}}]})])
        jira = JiraConnector("https://example.atlassian.net", "ops@example.com", SECRET_TOKEN, "OPS", transport=transport)
        receipt = jira.dispatch(_record())
        assert receipt.external_id == "OPS-7" and receipt.detail["created"] is False
        assert len(transport.calls) == 1, "no create call"

    def test_server_error_is_unknown_outcome_and_client_error_is_rejection(self):
        from stag.connectors import ConnectorRejected

        jira = JiraConnector("https://x", "e", SECRET_TOKEN, "OPS", transport=RecordedTransport([HttpResponse(200, {"issues": []}), HttpResponse(503, "unavailable")]))
        with pytest.raises(ConnectorTimeout):
            jira.dispatch(_record())
        jira = JiraConnector("https://x", "e", SECRET_TOKEN, "OPS", transport=RecordedTransport([HttpResponse(200, {"issues": []}), HttpResponse(400, {"errors": {"summary": "too long"}})]))
        with pytest.raises(ConnectorRejected):
            jira.dispatch(_record())

    def test_reconcile_observes_without_creating(self):
        transport = RecordedTransport([HttpResponse(200, {"issues": [{"key": "OPS-1"}]}), HttpResponse(200, {"issues": []}), HttpResponse(502, "bad gateway")])
        jira = JiraConnector("https://x", "e", SECRET_TOKEN, "OPS", transport=transport)
        record = _record()
        assert jira.reconcile(record) is True
        assert jira.reconcile(record) is False
        assert jira.reconcile(record) is None
        assert all(c.method == "GET" for c in transport.calls)


class TestServiceNowAdapter:
    def test_correlation_id_carries_the_operation_and_the_number_is_the_external_id(self):
        transport = RecordedTransport([HttpResponse(200, {"result": []}), HttpResponse(201, {"result": {"sys_id": "abc123", "number": "INC0010042"}})])
        snow = ServiceNowConnector("https://dev.service-now.com", "stag", "pw-secret", transport=transport)
        record = _record()
        receipt = snow.dispatch(record)
        assert receipt.external_id == "INC0010042" and receipt.detail["sys_id"] == "abc123"
        query, create = transport.calls
        assert query.params["sysparm_query"] == f"correlation_id={record.operation_id}"
        assert create.json["correlation_id"] == record.operation_id and create.json["short_description"].startswith("[STAG ")
        assert "pw-secret" not in json.dumps(snow.describe())

    def test_existing_record_is_reused(self):
        transport = RecordedTransport([HttpResponse(200, {"result": [{"sys_id": "s1", "number": "INC1", "state": "2"}]})])
        snow = ServiceNowConnector("https://dev.service-now.com", "stag", "pw", transport=transport)
        assert snow.dispatch(_record()).external_id == "INC1" and len(transport.calls) == 1


# --------------------------------------------------------------------------- registry


class TestRegistry:
    def test_connectors_without_credentials_are_not_built(self):
        s = Settings(action_connectors="fake-ticketing,jira,servicenow,bogus", notify_channels="dashboard,pagerduty,slack,email,fake", _env_file=None)
        actions = build_action_connectors(s)
        assert [c.name for c in actions] == ["fake-ticketing"]
        channels = [c.channel for c in build_notification_connectors(s)]
        assert channels == ["dashboard", "fake"]

    def test_connectors_with_credentials_are_built_from_settings(self):
        s = Settings(action_connectors="jira,servicenow", jira_base_url="https://j", jira_email="e", jira_api_token=SECRET_TOKEN, jira_project_key="OPS",
                     servicenow_instance_url="https://s", servicenow_user="u", servicenow_password="p",
                     notify_channels="pagerduty,slack,email", pagerduty_routing_key=SECRET_KEY, slack_webhook_url="https://hooks.slack.com/services/T/B/x",
                     smtp_host="smtp.example", email_from="stag@example", email_to="a@example, b@example", _env_file=None)
        assert [c.name for c in build_action_connectors(s)] == ["jira", "servicenow"]
        built = build_notification_connectors(s, smtp_transport=RecordedSmtp())
        assert [c.channel for c in built] == ["pagerduty", "slack", "email"]
        described = json.dumps([c.describe() for c in built])
        assert SECRET_KEY not in described and "/services/T/B/x" not in described


# --------------------------------------------------------------------------- notification adapters


def _notification(channel="fake", **kw) -> Notification:
    base = dict(channel=channel, incident_id="INC-1", title="CRITICAL incident INC-1: checkout-api", body="pool timeout", severity="critical", dedup_key="INC-1:critical", operation_ids=["OP-1"], provenance={"actor": "test"})
    base.update(kw)
    return Notification(**base)


class TestNotificationAdapters:
    def test_pagerduty_trigger_event_uses_the_dedup_key(self):
        transport = RecordedTransport([HttpResponse(202, {"status": "success", "dedup_key": "INC-1:critical"})])
        pd = PagerDutyConnector(SECRET_KEY, transport=transport)
        receipt = pd.deliver(_notification("pagerduty"))
        assert receipt.status == "delivered" and receipt.external_id == "INC-1:critical"
        [call] = transport.calls
        assert call.url == PagerDutyConnector.URL
        assert call.json["event_action"] == "trigger" and call.json["dedup_key"] == "INC-1:critical"
        assert call.json["payload"]["severity"] == "critical" and call.json["payload"]["custom_details"]["incident_id"] == "INC-1"
        assert SECRET_KEY not in json.dumps(receipt.model_dump(mode="json")) and SECRET_KEY not in json.dumps(pd.describe())

    def test_pagerduty_bad_routing_key_is_permanent_and_5xx_is_retryable(self):
        from stag.connectors import ConnectorRejected

        with pytest.raises(ConnectorRejected):
            PagerDutyConnector(SECRET_KEY, transport=RecordedTransport([HttpResponse(400, {"errors": ["routing key"]})])).deliver(_notification("pagerduty"))
        with pytest.raises(ConnectorTimeout):
            PagerDutyConnector(SECRET_KEY, transport=RecordedTransport([HttpResponse(500, "")])).deliver(_notification("pagerduty"))

    def test_slack_webhook_payload(self):
        transport = RecordedTransport([HttpResponse(200, "ok")])
        slack = SlackConnector("https://hooks.slack.com/services/T/B/secret", transport=transport)
        receipt = slack.deliver(_notification("slack"))
        assert receipt.status == "delivered"
        [call] = transport.calls
        assert call.url.endswith("/secret") and "CRITICAL incident INC-1" in call.json["text"] and call.json["blocks"][0]["type"] == "section"
        assert "secret" not in json.dumps(slack.describe()) and slack.describe()["webhook_host"] == "hooks.slack.com"

    def test_email_message_has_stable_message_id_and_incident_header(self):
        smtp = RecordedSmtp()
        email = EmailConnector("stag@example.com", ["oncall@example.com"], smtp)
        n = _notification("email")
        receipt = email.deliver(n)
        [msg] = smtp.messages
        assert msg["Subject"] == "[STAG critical] CRITICAL incident INC-1: checkout-api"
        assert msg["Message-ID"] == f"<{n.id}@stag.local>" and msg["X-STAG-Incident"] == "INC-1"
        assert "pool timeout" in msg.get_content() and receipt.external_id == msg["Message-ID"]
        with pytest.raises(ConnectorTimeout):
            EmailConnector("stag@example.com", ["x@example.com"], RecordedSmtp(fail_times=1)).deliver(n)

    def test_dashboard_keeps_messages_and_hands_pending_to_the_broadcaster(self):
        dash = DashboardConnector(keep=2)
        for i in range(3):
            dash.deliver(_notification("dashboard", dedup_key=f"INC-1:{i}"))
        assert len(dash.messages) == 2 and len(dash.pop_pending()) == 3 and dash.pop_pending() == []
        seen = []
        DashboardConnector(sink=seen.append).deliver(_notification("dashboard"))
        assert seen[0]["incident_id"] == "INC-1"


# --------------------------------------------------------------------------- outbox


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryControlStore()
    else:
        s = SqlControlStore(f"sqlite:///{tmp_path / 'o.sqlite'}")
        yield s
        s.close()


class TestOutbox:
    def test_same_notification_is_delivered_once(self, store):
        fake = FakeNotificationConnector("fake")
        outbox = NotificationOutbox(store, [fake])
        first = outbox.send(_notification())
        second = outbox.send(_notification())  # same channel + dedup key, new object
        assert first.status == "delivered" and first.attempts == 1
        assert second.status == "duplicate"
        assert len(fake.delivered) == 1
        assert outbox.stats["by_status"] == {"delivered": 1}

    def test_different_dedup_keys_are_different_messages(self, store):
        fake = FakeNotificationConnector("fake")
        outbox = NotificationOutbox(store, [fake])
        outbox.send(_notification(dedup_key="INC-1:high"))
        outbox.send(_notification(dedup_key="INC-1:critical"))
        assert len(fake.delivered) == 2

    def test_transient_failure_is_retried_with_attempt_count(self, store):
        fake = FakeNotificationConnector("fake", fail_times=2)
        outbox = NotificationOutbox(store, [fake], max_attempts=5)
        assert outbox.send(_notification()).status == "failed"
        assert outbox.drain()[0].status == "failed"
        final = outbox.drain()[0]
        assert final.status == "delivered" and final.attempts == 3 and len(fake.delivered) == 1
        assert outbox.drain() == []

    def test_exhausted_attempts_are_dead_and_permanent_refusal_is_dead_at_once(self, store):
        fake = FakeNotificationConnector("fake", fail_times=10)
        outbox = NotificationOutbox(store, [fake], max_attempts=2)
        outbox.send(_notification())
        dead = outbox.drain()[0]
        assert dead.status == "dead" and dead.attempts == 2 and outbox.drain() == []
        rejecting = NotificationOutbox(store, [FakeNotificationConnector("reject", reject=True)])
        assert rejecting.send(_notification("reject")).status == "dead"

    def test_unknown_channel_is_refused_unless_simulation_auto_fakes_it(self, store):
        assert NotificationOutbox(store, []).enqueue(_notification("status-page")).status == "dead"
        sim = NotificationOutbox(InMemoryControlStore(), [], auto_fake_channels=True)
        assert sim.send(_notification("status-page")).status == "delivered" and sim.channels == ["status-page"]

    def test_two_workers_on_one_store_deliver_each_message_once(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'shared.sqlite'}"
        fake_a, fake_b = FakeNotificationConnector("fake"), FakeNotificationConnector("fake")
        a = NotificationOutbox(SqlControlStore(url), [fake_a], worker="a")
        b = NotificationOutbox(SqlControlStore(url), [fake_b], worker="b")
        for i in range(6):
            a.enqueue(_notification(dedup_key=f"INC-1:{i}"))
        assert len(b.enqueue(_notification(dedup_key="INC-1:0")).status) and b.enqueue(_notification(dedup_key="INC-1:0")).status == "duplicate"
        delivered = a.drain(limit=3) + b.drain(limit=10) + a.drain(limit=10)
        assert sorted(r.status for r in delivered) == ["delivered"] * 6
        assert len(fake_a.delivered) + len(fake_b.delivered) == 6
        ids = [n.dedup_key for n in fake_a.delivered + fake_b.delivered]
        assert len(ids) == len(set(ids))

    def test_receipts_are_durable_across_restart(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'd.sqlite'}"
        store = SqlControlStore(url)
        NotificationOutbox(store, [FakeNotificationConnector("fake")]).send(_notification())
        store.close()
        again = NotificationOutbox(SqlControlStore(url), [FakeNotificationConnector("fake")])
        assert again.send(_notification()).status == "duplicate", "dedup survives a restart"
        [row] = again.receipts("INC-1")
        assert row["status"] == "delivered" and row["receipt"]["attempts"] == 1


# --------------------------------------------------------------------------- detector-driven paging


async def test_orchestrator_pages_once_per_incident_and_severity():
    from stag.pipeline.orchestrator import PipelineOrchestrator
    from stag.triggers import TriggerEngine
    from tests.test_orchestrator_techops import StubGenerator, StubRetriever, StubStream
    from tests.test_orchestrator_techops import _event as critical_event

    pager, slack = FakeNotificationConnector("pagerduty"), FakeNotificationConnector("slack")
    outbox = NotificationOutbox(InMemoryControlStore(), [pager, slack])
    orch = PipelineOrchestrator(stream_source=StubStream(), trigger_engine=TriggerEngine(), retriever=StubRetriever(), generator=StubGenerator(),
                                settings=Settings(page_on_severity="critical", _env_file=None), coordinator=IncidentCoordinator(investigation_cooldown_seconds=3600), outbox=outbox)
    first = critical_event()
    await orch.inject_event(first)
    await orch.inject_event(first)              # redelivery
    await orch.inject_event(critical_event())   # repeated symptom
    assert orch.stats["pages_enqueued"] == 2  # one per channel
    receipts = outbox.drain()
    assert sorted(r.channel for r in receipts) == ["pagerduty", "slack"] and all(r.status == "delivered" for r in receipts)
    assert len(pager.delivered) == 1 and pager.delivered[0].severity == "critical" and pager.delivered[0].provenance["actor"] == "stag-detector"
    assert outbox.drain() == []
    # below the threshold: nothing is paged
    quiet = PipelineOrchestrator(stream_source=StubStream(), trigger_engine=TriggerEngine(), retriever=StubRetriever(), generator=StubGenerator(),
                                 settings=Settings(page_on_severity="critical", _env_file=None), coordinator=IncidentCoordinator(), outbox=NotificationOutbox(InMemoryControlStore(), [FakeNotificationConnector("pagerduty")]))
    await quiet.inject_event(critical_event(severity=Severity.HIGH))
    assert quiet.stats["pages_enqueued"] == 0
