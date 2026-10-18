"""Identity contracts for STAG TechOps.

Four identities are kept independent on purpose:

    Observation identity = tenant + source + event_id
    Signal grouping key  = tenant + environment + service + failure_domain + symptom_class
    Incident identity    = durable ID assigned to one incident episode
    Action identity      = durable operation ID for one approved plan step + terms

Keys are *canonical*: components are encoded so that a delimiter inside a field cannot collide
with the delimiter between fields (``a|b`` + ``c`` never equals ``a`` + ``b|c``).

A hash of the event envelope is an integrity check, not an identity: independent observations can
carry identical content, and the same identity arriving with different content is a conflict.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field

# Metadata that describes *delivery* rather than the event itself. Excluded from the envelope hash
# so a redelivery with a new offset or attempt counter is still recognised as the same observation.
DELIVERY_METADATA_KEYS = frozenset(
    {
        "offset",
        "partition",
        "source_sequence",
        "delivery_attempt",
        "observed_at",
        "ingested_at",
        "adapter_version",
        "replay",
        "trace_id_delivery",
    }
)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=False)


def canonical_key(*parts: str) -> str:
    """Collision-free composite key: a JSON array of the parts."""
    return _canonical([str(p) for p in parts])


def display_key(*parts: str) -> str:
    """Human-readable composite key with ``|`` escaped inside components."""
    return "|".join(quote(str(p), safe="").replace("%7C", "%7C") if "|" in str(p) else str(p) for p in parts)


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable SHA-256 of a payload."""
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def envelope_hash(
    *,
    source: str,
    event_type: str,
    severity: str,
    payload: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> str:
    """SHA-256 over the *meaningful* envelope: source, type, severity, payload and non-delivery
    metadata. Two deliveries of one observation hash equal; a changed severity or service does not.
    """
    meta = {k: v for k, v in (metadata or {}).items() if k not in DELIVERY_METADATA_KEYS}
    body = {"source": source, "event_type": event_type, "severity": severity, "payload": payload, "metadata": meta}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


class ObservationIdentity(BaseModel):
    """Identity of one observation. Resending the same event keeps the same identity."""

    tenant: str = "default"
    source: str
    event_id: str

    @property
    def key(self) -> str:
        return canonical_key(self.tenant, self.source, self.event_id)

    @property
    def display(self) -> str:
        return display_key(self.tenant, self.source, self.event_id)


class SignalKey(BaseModel):
    """Locates candidate incidents. It is a *lookup* aid, not proof two signals share a cause."""

    tenant: str = "default"
    environment: str = "unknown"
    service: str = "unknown"
    failure_domain: str = "unknown"
    symptom_class: str = "unknown"
    extra: dict[str, str] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        parts = [self.tenant, self.environment, self.service, self.failure_domain, self.symptom_class]
        if self.extra:
            parts.append(",".join(f"{k}={self.extra[k]}" for k in sorted(self.extra)))
        return display_key(*parts)
