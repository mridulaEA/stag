"""Kafka stream source with acknowledgement aligned to durable processing (R17).

Delivery semantics are *at least once*:

* Auto-commit is off. An offset is committed only when the pipeline acknowledges the event after
  a durable outcome (``StreamSource.ack``). A crash before that point leaves the offset
  uncommitted; the redelivery is settled by durable dedup in the coordinator.
* Raw bytes are decoded inside an error boundary. A record that cannot become a valid
  ``StreamEvent`` (bad UTF-8, invalid JSON, not an object, missing stable id, schema violation,
  oversized) is dead-lettered: its coordinates, reason, size and content digest are kept in a
  bounded buffer, its offset is committed so a poison record cannot block the partition, and
  the payload itself is never logged.
* Stable identity is required: the record must carry the source's own event id. The adapter never
  mints one, so a redelivery can only ever be a duplicate, never a new observation.
* Partition/offset travel in delivery metadata keys that are excluded from the envelope hash.
* The consumer is supervised: a broker error tears the consumer down and rebuilds it with
  bounded backoff. Consumer lag per partition is sampled on every record.

The consumer is injected through ``consumer_factory`` so the adapter is testable against an
in-memory fake; the default factory builds an ``aiokafka.AIOKafkaConsumer``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from pydantic import ValidationError

from stag.models import StreamEvent
from stag.streams.base import StreamSource

logger = structlog.get_logger()

_KNOWN_FIELDS = frozenset(StreamEvent.model_fields)


@dataclass(frozen=True)
class DeadLetter:
    """A record that was consumed but could not be processed. Carries no payload."""

    topic: str
    partition: int
    offset: int
    reason: str
    size: int
    digest: str
    at: datetime


class KafkaStreamSource(StreamSource):
    """Consume ``StreamEvent`` records from a Kafka topic with manual, post-durability commits."""

    def __init__(
        self,
        bootstrap_servers: str = "localhost:9092",
        topic: str = "stag-events",
        group_id: str = "stag-consumer",
        *,
        consumer_factory: Callable[[], Any] | None = None,
        id_field: str = "id",
        max_record_bytes: int = 1_000_000,
        dead_letter_max: int = 1000,
        on_failure: Literal["dead_letter", "halt"] = "dead_letter",
        reconnect_backoff_seconds: float = 1.0,
        reconnect_backoff_max_seconds: float = 30.0,
    ) -> None:
        super().__init__()
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.group_id = group_id
        self.id_field = id_field
        self.max_record_bytes = max_record_bytes
        self.on_failure = on_failure
        self.reconnect_backoff_seconds = reconnect_backoff_seconds
        self.reconnect_backoff_max_seconds = reconnect_backoff_max_seconds
        self._consumer_factory = consumer_factory or self._default_consumer_factory
        self._consumer: Any = None
        self._tp_factory: Callable[[str, int], Any] = lambda topic, partition: (topic, partition)
        # delivery coordinates ("topic/partition/offset") handed to the pipeline and not yet settled
        self._inflight: dict[str, tuple[str, int, int]] = {}
        # per partition: highest offset consumed (delivered or dead-lettered) and the offsets still in flight
        self._partitions: dict[tuple[str, int], dict[str, Any]] = {}
        self._committed: dict[tuple[str, int], int] = {}
        self._lag: dict[str, int] = {}
        self._dead_letters: deque[DeadLetter] = deque(maxlen=dead_letter_max)
        self._drift_keys: set[str] = set()
        self._counts = {
            "consumed": 0,
            "delivered": 0,
            "acked": 0,
            "nacked": 0,
            "dead_lettered": 0,
            "commits": 0,
            "reconnects": 0,
        }
        self._commit_lock = asyncio.Lock()
        self.halted: bool = False

    # ------------------------------------------------------------------ consumer plumbing
    def _default_consumer_factory(self):
        try:
            from aiokafka import AIOKafkaConsumer, TopicPartition
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("aiokafka is required for Kafka streaming. Install with: pip install aiokafka") from e
        self._tp_factory = TopicPartition
        return AIOKafkaConsumer(
            self.topic,
            bootstrap_servers=self.bootstrap_servers,
            group_id=self.group_id,
            enable_auto_commit=False,  # commits follow durable processing, never the clock
            auto_offset_reset="earliest",
            # raw bytes: decoding happens inside the error boundary below
            value_deserializer=None,
            key_deserializer=None,
            max_partition_fetch_bytes=max(self.max_record_bytes, 1_048_576),
        )

    async def connect(self) -> None:
        self._consumer = self._consumer_factory()
        await self._consumer.start()
        logger.info("kafka_stream.connected", servers=self.bootstrap_servers, topic=self.topic, group=self.group_id)

    async def disconnect(self) -> None:
        consumer, self._consumer = self._consumer, None
        if consumer is not None:
            try:
                await consumer.stop()
            except Exception as e:  # pragma: no cover - broker already gone
                logger.warning("kafka_stream.stop_error", error=str(e))
        logger.info("kafka_stream.disconnected", **self._counts)

    async def _reconnect(self, attempt: int) -> None:
        self._counts["reconnects"] += 1
        delay = min(self.reconnect_backoff_seconds * (2 ** min(attempt, 10)), self.reconnect_backoff_max_seconds)
        logger.warning("kafka_stream.reconnecting", attempt=attempt, delay_seconds=delay)
        await self.disconnect()
        await asyncio.sleep(delay)
        if self._running:
            await self.connect()

    # ------------------------------------------------------------------ consume loop (supervised)
    async def _read_events(self) -> None:
        if self._consumer is None:
            raise RuntimeError("Not connected. Call connect() first.")
        attempt = 0
        while self._running and not self.halted:
            try:
                async for record in self._consumer:
                    if not self._running or self.halted:
                        break
                    attempt = 0
                    await self._handle_record(record)
                else:
                    # the consumer iterator ended cleanly (stopped); leave the loop
                    break
                break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._running:
                    break
                logger.error("kafka_stream.consumer_error", error=f"{type(e).__name__}: {str(e)[:200]}")
                attempt += 1
                await self._reconnect(attempt)

    async def _handle_record(self, record) -> None:
        self._counts["consumed"] += 1
        self._sample_lag(record)
        outcome = self._decode(record)
        if isinstance(outcome, DeadLetter):
            await self._dead_letter(outcome)
            return
        coords = (record.topic, record.partition, record.offset)
        state = self._partition_state(record.topic, record.partition, record.offset)
        state["consumed_through"] = max(state["consumed_through"], record.offset)
        state["inflight"].add(record.offset)
        self._inflight[outcome.metadata["source_sequence"]] = coords
        self._counts["delivered"] += 1
        await self._queue.put(outcome)

    def _partition_state(self, topic: str, partition: int, first_offset: int | None = None) -> dict[str, Any]:
        state = self._partitions.get((topic, partition))
        if state is None:
            # ``start`` is the position this consumer began at: committing at or below it is a no-op
            state = {"consumed_through": -1, "inflight": set(), "start": first_offset if first_offset is not None else 0}
            self._partitions[(topic, partition)] = state
        return state

    def _sample_lag(self, record) -> None:
        highwater = getattr(self._consumer, "highwater", None)
        if highwater is None:
            return
        try:
            hw = highwater(self._tp_factory(record.topic, record.partition))
        except Exception:  # pragma: no cover - fake or unassigned partition
            hw = None
        if hw is not None:
            self._lag[f"{record.topic}/{record.partition}"] = max(int(hw) - (record.offset + 1), 0)

    # ------------------------------------------------------------------ decode boundary
    def _decode(self, record) -> StreamEvent | DeadLetter:
        raw: bytes = record.value if isinstance(record.value, bytes | bytearray) else (record.value or b"")
        size = len(raw)
        digest = hashlib.sha256(raw).hexdigest()[:16]

        def dead(reason: str) -> DeadLetter:
            return DeadLetter(record.topic, record.partition, record.offset, reason, size, digest, datetime.now(UTC))

        if size > self.max_record_bytes:
            return dead(f"record too large ({size} > {self.max_record_bytes} bytes)")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return dead("invalid utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return dead("invalid json")
        if not isinstance(data, dict):
            return dead(f"not a json object ({type(data).__name__})")
        event_id = data.get(self.id_field)
        if not isinstance(event_id, str) or not event_id.strip():
            # never mint an id: a redelivery must stay recognisable as the same observation
            return dead(f"missing stable id field '{self.id_field}'")
        unknown = set(data) - _KNOWN_FIELDS - {self.id_field}
        if unknown:
            self._drift_keys.update(sorted(unknown)[:20])
        fields = {k: v for k, v in data.items() if k in _KNOWN_FIELDS}
        fields["id"] = event_id
        try:
            event = StreamEvent.model_validate(fields)
        except ValidationError as e:
            locs = sorted({".".join(str(p) for p in err["loc"]) or "<root>" for err in e.errors()})
            return dead("schema violation at " + ", ".join(locs[:8]))
        # delivery coordinates: excluded from the envelope hash (DELIVERY_METADATA_KEYS)
        event.metadata["partition"] = record.partition
        event.metadata["offset"] = record.offset
        event.metadata["source_sequence"] = f"{record.topic}/{record.partition}/{record.offset}"
        return event

    # ------------------------------------------------------------------ acknowledgement
    def _take(self, event: StreamEvent) -> tuple[str, int, int] | None:
        seq = event.metadata.get("source_sequence") if isinstance(event.metadata, dict) else None
        return self._inflight.pop(seq, None) if isinstance(seq, str) else None

    async def ack(self, event: StreamEvent) -> None:
        coords = self._take(event)
        if coords is None:
            return  # not a delivery of ours (e.g. injected via API) or already settled
        self._counts["acked"] += 1
        await self._settle(*coords)

    async def nack(self, event: StreamEvent, reason: str) -> None:
        coords = self._take(event)
        if coords is None:
            return
        self._counts["nacked"] += 1
        topic, partition, offset = coords
        if self.on_failure == "halt":
            # fail closed: leave the offset uncommitted and stop consuming; the record is
            # redelivered to the next consumer of this group.
            self.halted = True
            logger.error("kafka_stream.halted", topic=topic, partition=partition, offset=offset, reason=reason[:200])
            return
        await self._dead_letter(DeadLetter(topic, partition, offset, f"processing failed: {reason[:160]}", 0, "", datetime.now(UTC)))

    async def _dead_letter(self, letter: DeadLetter) -> None:
        self._dead_letters.append(letter)
        self._counts["dead_lettered"] += 1
        logger.warning(
            "kafka_stream.dead_letter",
            topic=letter.topic,
            partition=letter.partition,
            offset=letter.offset,
            reason=letter.reason,
            size=letter.size,
            digest=letter.digest,
        )
        # a poison record must not block the partition; its coordinates are kept for operators
        state = self._partition_state(letter.topic, letter.partition, letter.offset)
        state["consumed_through"] = max(state["consumed_through"], letter.offset)
        await self._settle(letter.topic, letter.partition, letter.offset)

    async def _settle(self, topic: str, partition: int, offset: int) -> None:
        """Mark one delivery settled and commit the partition up to the first unsettled offset.

        Kafka commits are a high-water mark, so committing a settled offset while an earlier
        delivery is still in flight would acknowledge work that has not reached a durable outcome.
        The commit position is therefore the lowest in-flight offset (or consumed_through + 1).
        """
        async with self._commit_lock:
            key = (topic, partition)
            state = self._partition_state(topic, partition)
            state["inflight"].discard(offset)
            commit_to = min(state["inflight"]) if state["inflight"] else state["consumed_through"] + 1
            if commit_to <= max(self._committed.get(key, -1), state["start"]):
                return  # nothing new to acknowledge to the broker
            consumer = self._consumer
            if consumer is None:
                return  # disconnected: leave uncommitted → redelivered after restart
            try:
                await consumer.commit({self._tp_factory(topic, partition): commit_to})
            except Exception as e:
                logger.error("kafka_stream.commit_error", topic=topic, partition=partition, offset=offset, error=str(e)[:200])
                return
            self._committed[key] = commit_to
            self._counts["commits"] += 1

    # ------------------------------------------------------------------ observability
    @property
    def dead_letters(self) -> list[dict[str, Any]]:
        return [{**asdict(d), "at": d.at.isoformat()} for d in self._dead_letters]

    @property
    def stats(self) -> dict[str, Any]:
        return {
            **super().stats,
            **self._counts,
            "inflight": len(self._inflight),
            "committed_offsets": {f"{t}/{p}": o for (t, p), o in sorted(self._committed.items())},
            "lag": dict(self._lag),
            "schema_drift_keys": sorted(self._drift_keys),
            "dead_letter_buffer": len(self._dead_letters),
            "halted": self.halted,
            "on_failure": self.on_failure,
        }
