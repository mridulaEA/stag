"""Durable notification outbox on the control store.

* ``enqueue`` is a unique insert on the notification id: the same message (channel + dedup key)
  is accepted once and reported as ``duplicate`` afterwards, across restarts and workers.
* ``drain`` claims queued or failed entries with a lease (one worker delivers each), calls the
  channel's connector and records a receipt. Retryable failures are retried up to ``max_attempts``
  with the attempt count on the receipt; a permanent refusal is ``dead`` at once.
"""

from __future__ import annotations

import json
import os
import socket
from typing import Any

import structlog

from stag.connectors.base import ConnectorRejected
from stag.connectors.notifications import (
    DeliveryReceipt,
    FakeNotificationConnector,
    Notification,
    NotificationConnector,
)
from stag.techops.store import ControlStore, InMemoryControlStore

logger = structlog.get_logger()


class NotificationOutbox:
    COLLECTION = "outbox"

    def __init__(
        self,
        store: ControlStore | None = None,
        connectors: list[NotificationConnector] | None = None,
        *,
        max_attempts: int = 5,
        lease_seconds: int = 60,
        worker: str = "",
        auto_fake_channels: bool = False,
    ) -> None:
        self.store = store or InMemoryControlStore()
        self.connectors: dict[str, NotificationConnector] = {c.channel: c for c in (connectors or [])}
        self.max_attempts = max_attempts
        self.lease_seconds = lease_seconds
        self.worker = worker or f"{socket.gethostname()}:{os.getpid()}"
        # simulation only: a crew may name any channel; each gets a recording fake
        self.auto_fake_channels = auto_fake_channels

    # ------------------------------------------------------------------ channels
    def connector_for(self, channel: str) -> NotificationConnector | None:
        connector = self.connectors.get(channel)
        if connector is None and self.auto_fake_channels:
            connector = self.connectors[channel] = FakeNotificationConnector(channel)
        return connector

    @property
    def channels(self) -> list[str]:
        return sorted(self.connectors)

    def describe(self) -> list[dict[str, Any]]:
        return [c.describe() for _, c in sorted(self.connectors.items())]

    # ------------------------------------------------------------------ enqueue / drain
    def enqueue(self, notification: Notification) -> DeliveryReceipt:
        nid = notification.id
        if self.connector_for(notification.channel) is None:
            return DeliveryReceipt(notification_id=nid, channel=notification.channel, status="dead", error=f"no connector for channel '{notification.channel}'")
        doc = {"notification": notification.model_dump(mode="json"), "attempts": 0, "status": "queued", "receipt": None}
        if not self.store.insert_unique(self.COLLECTION, nid, doc, status="queued", scope=notification.incident_id):
            existing = self._doc(self.store.get(self.COLLECTION, nid)[0])
            return DeliveryReceipt(notification_id=nid, channel=notification.channel, status="duplicate", attempts=int(existing.get("attempts", 0)))
        return DeliveryReceipt(notification_id=nid, channel=notification.channel, status="queued")

    def drain(self, limit: int = 50) -> list[DeliveryReceipt]:
        rows = self.store.list(self.COLLECTION, status="queued", limit=limit) + self.store.list(self.COLLECTION, status="failed", limit=limit)
        receipts: list[DeliveryReceipt] = []
        for key, _, _ in rows:
            claimed = self.store.claim(self.COLLECTION, key, self.worker, self.lease_seconds, from_statuses=("queued", "failed"))
            if claimed is None:
                continue  # another worker holds it
            doc = self._doc(claimed[0])
            notification = Notification.model_validate(doc["notification"])
            attempts = int(doc.get("attempts", 0)) + 1
            connector = self.connector_for(notification.channel)
            try:
                if connector is None:
                    raise ConnectorRejected(f"no connector for channel '{notification.channel}'")
                receipt = connector.deliver(notification)
                receipt.attempts = attempts
            except ConnectorRejected as e:
                receipt = DeliveryReceipt(notification_id=key, channel=notification.channel, status="dead", attempts=attempts, error=str(e)[:300])
            except Exception as e:
                status = "failed" if attempts < self.max_attempts else "dead"
                receipt = DeliveryReceipt(notification_id=key, channel=notification.channel, status=status, attempts=attempts, error=f"{type(e).__name__}: {str(e)[:300]}")
            doc.update({"attempts": attempts, "status": receipt.status, "receipt": receipt.model_dump(mode="json")})
            self.store.put(self.COLLECTION, key, doc, claimed[1], status=receipt.status, scope=notification.incident_id)
            if receipt.status != "delivered":
                logger.warning("outbox.delivery_not_completed", notification_id=key, channel=notification.channel, status=receipt.status, attempts=attempts, error=receipt.error)
            receipts.append(receipt)
        return receipts

    def send(self, notification: Notification) -> DeliveryReceipt:
        """Enqueue then drain once; returns the receipt for *this* notification."""
        queued = self.enqueue(notification)
        if queued.status != "queued":
            return queued
        for receipt in self.drain():
            if receipt.notification_id == notification.id:
                return receipt
        return queued

    # ------------------------------------------------------------------ inspection
    def receipts(self, incident_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        rows = self.store.list(self.COLLECTION, scope=incident_id, limit=limit)
        out = []
        for key, doc, _ in rows:
            d = self._doc(doc)
            out.append({"id": key, "status": d.get("status"), "attempts": d.get("attempts", 0), "notification": d.get("notification"), "receipt": d.get("receipt")})
        return out

    @property
    def stats(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for _, doc, _ in self.store.list(self.COLLECTION):
            s = self._doc(doc).get("status", "?")
            by_status[s] = by_status.get(s, 0) + 1
        return {"total": self.store.count(self.COLLECTION), "by_status": by_status, "channels": self.channels, "worker": self.worker}

    @staticmethod
    def _doc(doc: Any) -> dict[str, Any]:
        return json.loads(doc) if isinstance(doc, str) else dict(doc)
