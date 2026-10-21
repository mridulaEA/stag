"""Kafka ingestion against a real broker (R17). Runs when ``STAG_TEST_KAFKA_BOOTSTRAP`` is set.

CI starts an ``apache/kafka`` service container. Locally, any single-node KRaft broker works:
``STAG_TEST_KAFKA_BOOTSTRAP=127.0.0.1:9092 pytest tests/test_kafka_broker.py``.

Checks the two properties the fake cannot prove: the committed offset the *broker* holds for the
consumer group follows acknowledgement, and an unacknowledged delivery is redelivered to the
next consumer of the group.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest

pytest.importorskip("aiokafka")

BOOTSTRAP = os.environ.get("STAG_TEST_KAFKA_BOOTSTRAP")
pytestmark = pytest.mark.skipif(not BOOTSTRAP, reason="STAG_TEST_KAFKA_BOOTSTRAP not set")


def _record(event_id: str) -> bytes:
    return json.dumps(
        {"id": event_id, "source": "checkout-api", "event_type": "alert", "severity": "high",
         "payload": {"message": "pool timeout"}, "metadata": {"service": "checkout-api"}}
    ).encode()


async def _produce(topic: str, values: list[bytes]) -> None:
    from aiokafka import AIOKafkaProducer

    for attempt in range(30):  # the broker may still be electing its coordinator right after start
        producer = AIOKafkaProducer(bootstrap_servers=BOOTSTRAP)
        try:
            await producer.start()
            for v in values:
                await producer.send_and_wait(topic, v, partition=0)
            return
        except Exception:
            if attempt == 29:
                raise
            await asyncio.sleep(1)
        finally:
            await producer.stop()


async def _collect(src, n: int, timeout: float = 60.0):
    out = []

    async def pull():
        async for ev in src.events():
            out.append(ev)
            if len(out) >= n:
                return

    await asyncio.wait_for(pull(), timeout)
    return out


async def _committed_offset(topic: str, group: str) -> int | None:
    from aiokafka import AIOKafkaConsumer, TopicPartition

    consumer = AIOKafkaConsumer(bootstrap_servers=BOOTSTRAP, group_id=group, enable_auto_commit=False)
    await consumer.start()
    try:
        meta = await consumer.committed(TopicPartition(topic, 0))
        return meta
    finally:
        await consumer.stop()


async def test_commit_follows_ack_and_unacked_records_are_redelivered():
    from stag.streams.kafka_stream import KafkaStreamSource

    topic = f"stag-test-{uuid.uuid4().hex[:8]}"
    group = f"stag-group-{uuid.uuid4().hex[:8]}"
    await _produce(topic, [_record("e-1"), b"{not json", _record("e-2")])

    src = KafkaStreamSource(BOOTSTRAP, topic, group, reconnect_backoff_seconds=0.5)
    await src.start()
    try:
        first, second = await _collect(src, 2)
        assert [first.id, second.id] == ["e-1", "e-2"]
        assert src.stats["dead_lettered"] == 1  # the poison record in between
        await src.ack(first)
        await asyncio.sleep(0.5)
        assert src.stats["committed_offsets"] == {f"{topic}/0": 2}, "e-1 acked, poison settled, e-2 still in flight"
    finally:
        await src.stop()

    assert await _committed_offset(topic, group) == 2

    # A new consumer of the same group starts at the committed position: only e-2 comes back.
    again = KafkaStreamSource(BOOTSTRAP, topic, group, reconnect_backoff_seconds=0.5)
    await again.start()
    try:
        [redelivered] = await _collect(again, 1)
        assert redelivered.id == "e-2"
        assert redelivered.metadata["offset"] == 2
        await again.ack(redelivered)
        await asyncio.sleep(0.5)
    finally:
        await again.stop()
    assert await _committed_offset(topic, group) == 3


async def test_poison_only_topic_is_fully_committed():
    from stag.streams.kafka_stream import KafkaStreamSource

    topic = f"stag-test-{uuid.uuid4().hex[:8]}"
    group = f"stag-group-{uuid.uuid4().hex[:8]}"
    await _produce(topic, [b"\xff\xfe", b"[1,2]", json.dumps({"source": "x", "event_type": "alert"}).encode()])
    src = KafkaStreamSource(BOOTSTRAP, topic, group, reconnect_backoff_seconds=0.5)
    await src.start()
    try:
        for _ in range(600):
            if src.stats["dead_lettered"] == 3:
                break
            await asyncio.sleep(0.1)
        assert src.stats["dead_lettered"] == 3
        reasons = [d["reason"] for d in src.dead_letters]
        assert reasons[0] == "invalid utf-8" and reasons[1].startswith("not a json object") and reasons[2].startswith("missing stable id")
        await asyncio.sleep(0.5)
    finally:
        await src.stop()
    assert await _committed_offset(topic, group) == 3
