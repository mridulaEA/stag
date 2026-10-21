"""Kafka ingestion hardening (R17) against an in-memory consumer fake.

The fake implements the slice of the aiokafka consumer surface the adapter uses: ``start``,
``stop``, async iteration over records, ``commit`` and ``highwater``. The real-broker counterpart is
``tests/test_kafka_broker.py`` (runs when ``STAG_TEST_KAFKA_BOOTSTRAP`` is set).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from stag.config import Settings
from stag.pipeline.orchestrator import PipelineOrchestrator
from stag.streams.base import StreamSource
from stag.streams.kafka_stream import DeadLetter, KafkaStreamSource
from stag.triggers import TriggerEngine
from tests.test_orchestrator_techops import StubGenerator, StubRetriever

SECRET = "ssn=123-45-6789"


@dataclass
class FakeRecord:
    topic: str
    partition: int
    offset: int
    value: bytes
    key: bytes | None = None


class FakeConsumer:
    """Yields its records, then waits like a real consumer until stopped (or fails on demand)."""

    def __init__(self, records, *, fail_after: int | None = None, partition_sizes: dict | None = None):
        self.records = list(records)
        self.fail_after = fail_after
        self.partition_sizes = partition_sizes or {}
        self.commits: list[dict] = []
        self.started = False
        self.stopped = False
        self._i = 0

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        while True:
            if self.stopped:
                raise StopAsyncIteration
            if self.fail_after is not None and self._i >= self.fail_after:
                self.fail_after = None
                raise ConnectionError("broker went away")
            if self._i < len(self.records):
                rec = self.records[self._i]
                self._i += 1
                return rec
            await asyncio.sleep(0.005)

    async def commit(self, offsets: dict):
        self.commits.append(dict(offsets))

    def highwater(self, tp):
        topic, partition = tp
        return self.partition_sizes.get((topic, partition), len([r for r in self.records if r.partition == partition]))


def record(offset: int, data: dict | bytes, partition: int = 0, topic: str = "stag-events") -> FakeRecord:
    value = data if isinstance(data, bytes) else json.dumps(data).encode()
    return FakeRecord(topic, partition, offset, value)


def valid(event_id: str, **overrides) -> dict:
    base = {
        "id": event_id,
        "source": "checkout-api",
        "event_type": "alert",
        "severity": "high",
        "payload": {"message": "pool timeout"},
        "metadata": {"service": "checkout-api", "environment": "production"},
    }
    base.update(overrides)
    return base


async def source_with(records, **kwargs) -> tuple[KafkaStreamSource, FakeConsumer]:
    consumer = FakeConsumer(records)
    src = KafkaStreamSource(consumer_factory=lambda: consumer, reconnect_backoff_seconds=0.01, **kwargs)
    await src.start()
    return src, consumer


async def take(src: StreamSource, n: int, timeout: float = 2.0):
    out = []

    async def pull():
        async for ev in src.events():
            out.append(ev)
            if len(out) >= n:
                return

    await asyncio.wait_for(pull(), timeout)
    return out


async def settle():
    await asyncio.sleep(0.02)


# --------------------------------------------------------------------------- delivery and commit


async def test_offset_is_committed_only_after_ack():
    src, consumer = await source_with([record(0, valid("e-1"))])
    [event] = await take(src, 1)
    assert event.id == "e-1"
    assert event.metadata["partition"] == 0 and event.metadata["offset"] == 0
    assert event.metadata["source_sequence"] == "stag-events/0/0"
    await settle()
    assert consumer.commits == [], "nothing may be committed before the pipeline acknowledges"
    assert src.stats["inflight"] == 1

    await src.ack(event)
    assert consumer.commits == [{("stag-events", 0): 1}]
    assert src.stats["acked"] == 1 and src.stats["committed_offsets"] == {"stag-events/0": 1}
    await src.stop()


async def test_stop_without_ack_leaves_offset_uncommitted():
    src, consumer = await source_with([record(0, valid("e-1")), record(1, valid("e-2"))])
    await take(src, 2)
    await src.stop()
    assert consumer.commits == []
    assert src.stats["inflight"] == 2


async def test_commit_position_never_passes_an_unsettled_delivery():
    """Kafka commits are a high-water mark: acking offset 1 while offset 0 is in flight must not
    commit 2, or a crash would lose the unsettled first delivery."""
    src, consumer = await source_with([record(0, valid("e-1")), record(1, valid("e-2")), record(2, valid("e-3"))])
    first, second, third = await take(src, 3)
    await src.ack(second)
    assert consumer.commits == [], "offset 0 still in flight"
    await src.ack(third)
    assert consumer.commits == []
    await src.ack(first)
    assert consumer.commits == [{("stag-events", 0): 3}], "one contiguous commit once the gap closes"
    await src.stop()


async def test_dead_letter_commit_does_not_pass_an_inflight_delivery():
    src, consumer = await source_with([record(0, valid("e-1")), record(1, b"not json")])
    [first] = await take(src, 1)
    await settle()
    assert src.stats["dead_lettered"] == 1
    assert consumer.commits == [], "the poison record at offset 1 cannot be committed over the in-flight offset 0"
    await src.ack(first)
    assert consumer.commits == [{("stag-events", 0): 2}]
    await src.stop()


async def test_ack_of_unknown_event_is_ignored():
    from stag.models import StreamEvent
    from stag.models.schemas import EventType

    src, consumer = await source_with([])
    await src.ack(StreamEvent(source="api", event_type=EventType.ALERT))  # injected via API, not a delivery
    assert consumer.commits == [] and src.stats["acked"] == 0
    await src.stop()


# --------------------------------------------------------------------------- decode boundary


async def test_malformed_records_are_dead_lettered_without_payload_and_consumption_continues():
    records = [
        record(0, b"\xff\xfe" + SECRET.encode()),  # invalid utf-8
        record(1, ("{not json " + SECRET).encode()),  # invalid json
        record(2, json.dumps([SECRET]).encode()),  # not an object
        record(3, valid("", payload={"leak": SECRET}) | {"id": ""}),  # empty id
        record(4, {k: v for k, v in valid("x").items() if k != "id"} | {"payload": {"leak": SECRET}}),  # missing id
        record(5, valid("e-bad-type", event_type="nope", payload={"leak": SECRET})),  # schema violation
        record(6, valid("e-ok")),
    ]
    src, consumer = await source_with(records, dead_letter_max=10)
    [ok] = await take(src, 1)
    assert ok.id == "e-ok"
    await settle()
    letters = src.dead_letters
    assert len(letters) == 6 and src.stats["dead_lettered"] == 6
    reasons = [d["reason"] for d in letters]
    assert reasons[0] == "invalid utf-8"
    assert reasons[1] == "invalid json"
    assert reasons[2].startswith("not a json object")
    assert reasons[3].startswith("missing stable id")
    assert reasons[4].startswith("missing stable id")
    assert reasons[5].startswith("schema violation at event_type")
    assert all(SECRET not in json.dumps(d) for d in letters), "dead letters carry coordinates and a digest, never content"
    assert all(d["digest"] and d["size"] > 0 for d in letters)
    # poison records are committed so they never block the partition, but only up to the in-flight offset
    assert consumer.commits[-1] == {("stag-events", 0): 6}
    await src.ack(ok)
    assert consumer.commits[-1] == {("stag-events", 0): 7}
    await src.stop()


async def test_oversized_record_is_dead_lettered():
    src, consumer = await source_with([record(0, valid("big", payload={"blob": "x" * 5000}))], max_record_bytes=1000)
    await settle()
    assert src.stats["dead_lettered"] == 1 and src.dead_letters[0]["reason"].startswith("record too large")
    assert consumer.commits == [{("stag-events", 0): 1}]
    await src.stop()


async def test_identity_is_never_minted():
    src, _ = await source_with([record(0, {k: v for k, v in valid("x").items() if k != "id"})])
    await settle()
    assert src.stats["delivered"] == 0 and src.stats["dead_lettered"] == 1
    await src.stop()


async def test_unknown_top_level_fields_are_counted_as_schema_drift():
    src, _ = await source_with([record(0, valid("e-1") | {"schema_version": 2, "trace": "abc"})])
    [event] = await take(src, 1)
    assert event.id == "e-1"
    assert src.stats["schema_drift_keys"] == ["schema_version", "trace"]
    await src.stop()


async def test_dead_letter_buffer_is_bounded():
    src, _ = await source_with([record(i, b"x") for i in range(25)], dead_letter_max=5)
    await settle()
    assert src.stats["dead_lettered"] == 25 and len(src.dead_letters) == 5
    assert src.dead_letters[0]["offset"] == 20
    await src.stop()


# --------------------------------------------------------------------------- failure policy


async def test_nack_dead_letters_and_commits_by_default():
    src, consumer = await source_with([record(0, valid("e-1"))])
    [event] = await take(src, 1)
    await src.nack(event, "ValueError: boom")
    assert src.stats["nacked"] == 1 and src.dead_letters[0]["reason"] == "processing failed: ValueError: boom"
    assert consumer.commits == [{("stag-events", 0): 1}]
    await src.stop()


async def test_nack_halts_without_commit_when_configured():
    src, consumer = await source_with([record(0, valid("e-1")), record(1, valid("e-2"))], on_failure="halt")
    [event, _] = await take(src, 2)
    await src.nack(event, "boom")
    assert src.halted is True and consumer.commits == []
    assert src.stats["halted"] is True
    await src.stop()


# --------------------------------------------------------------------------- supervision and lag


async def test_consumer_error_rebuilds_consumer_with_backoff_and_redelivers():
    first = FakeConsumer([record(0, valid("e-1"))], fail_after=1)
    second = FakeConsumer([record(0, valid("e-1")), record(1, valid("e-2"))])  # broker redelivers the uncommitted 0
    consumers = iter([first, second])
    src = KafkaStreamSource(consumer_factory=lambda: next(consumers), reconnect_backoff_seconds=0.01)
    await src.start()
    events = await take(src, 3, timeout=5)
    assert [e.id for e in events] == ["e-1", "e-1", "e-2"]
    assert src.stats["reconnects"] == 1 and first.stopped and second.started
    for e in events:
        await src.ack(e)
    assert second.commits[-1] == {("stag-events", 0): 2}
    await src.stop()


async def test_lag_is_sampled_per_partition():
    records = [record(0, valid("a")), record(1, valid("b")), record(0, valid("c"), partition=1)]
    src, _ = await source_with(records, )
    src._consumer.partition_sizes = {("stag-events", 0): 10, ("stag-events", 1): 1}
    await take(src, 3)
    assert src.stats["lag"] == {"stag-events/0": 8, "stag-events/1": 0}
    await src.stop()


# --------------------------------------------------------------------------- orchestrator contract


class RecordingStream(StreamSource):
    def __init__(self):
        super().__init__()
        self.acked: list[str] = []
        self.nacked: list[tuple[str, str]] = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def _read_events(self):
        pass

    async def ack(self, event):
        self.acked.append(event.id)

    async def nack(self, event, reason):
        self.nacked.append((event.id, reason))


async def test_orchestrator_acks_after_a_durable_outcome_and_nacks_on_failure():
    from stag.models import StreamEvent
    from stag.models.schemas import EventType, Severity

    stream = RecordingStream()
    orch = PipelineOrchestrator(stream_source=stream, trigger_engine=TriggerEngine(), retriever=StubRetriever(),
                                generator=StubGenerator(), settings=Settings())
    good = StreamEvent(source="checkout-api", event_type=EventType.ALERT, severity=Severity.LOW, payload={"message": "fine"})
    bad = StreamEvent(source="checkout-api", event_type=EventType.ALERT, severity=Severity.LOW, payload={"message": "explode"})
    original = orch._process_event

    async def process(event):
        if event.payload.get("message") == "explode":
            raise RuntimeError("store unavailable")
        await original(event)

    orch._process_event = process
    await orch.start()
    await stream._queue.put(good)
    await stream._queue.put(bad)
    for _ in range(100):
        if stream.acked and stream.nacked:
            break
        await asyncio.sleep(0.01)
    await orch.stop()
    assert stream.acked == [good.id]
    assert stream.nacked == [(bad.id, "RuntimeError: store unavailable")]
    assert orch.stats["stream"]["running"] is False


def test_dead_letter_is_a_plain_record():
    from datetime import UTC, datetime

    d = DeadLetter("t", 0, 1, "r", 3, "abc", datetime.now(UTC))
    assert d.topic == "t" and d.offset == 1
