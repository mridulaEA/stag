"""Kafka stream source for production deployments."""

from __future__ import annotations

import json

import structlog

from stag.models import StreamEvent
from stag.streams.base import StreamSource

logger = structlog.get_logger()


class KafkaStreamSource(StreamSource):
    """Consume events from Apache Kafka topics."""

    def __init__(
        self,
        bootstrap_servers: str = "localhost:9092",
        topic: str = "stag-events",
        group_id: str = "stag-consumer",
    ) -> None:
        super().__init__()
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self.group_id = group_id
        self._consumer = None

    async def connect(self) -> None:
        try:
            from aiokafka import AIOKafkaConsumer

            self._consumer = AIOKafkaConsumer(
                self.topic,
                bootstrap_servers=self.bootstrap_servers,
                group_id=self.group_id,
                value_deserializer=lambda m: json.loads(m.decode("utf-8")),
                auto_offset_reset="latest",
                enable_auto_commit=True,
            )
            await self._consumer.start()
            logger.info(
                "kafka_stream.connected",
                servers=self.bootstrap_servers,
                topic=self.topic,
            )
        except ImportError as e:
            raise RuntimeError(
                "aiokafka is required for Kafka streaming. Install with: pip install aiokafka"
            ) from e

    async def disconnect(self) -> None:
        if self._consumer:
            await self._consumer.stop()
            logger.info("kafka_stream.disconnected")

    async def _read_events(self) -> None:
        """Consume messages from Kafka and convert to StreamEvents."""
        if not self._consumer:
            raise RuntimeError("Not connected. Call connect() first.")

        logger.info("kafka_stream.consuming", topic=self.topic)
        async for msg in self._consumer:
            if not self._running:
                break
            try:
                event = StreamEvent(**msg.value)
                await self._queue.put(event)
                logger.debug(
                    "kafka_stream.event_received",
                    event_id=event.id,
                    partition=msg.partition,
                    offset=msg.offset,
                )
            except Exception as e:
                logger.error("kafka_stream.parse_error", error=str(e), raw=str(msg.value)[:200])
