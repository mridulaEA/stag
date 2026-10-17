"""Identity contracts for STAG TechOps.

Four identities are kept independent on purpose:

    Observation identity = tenant + source + event_id
    Signal grouping key  = tenant + environment + service + failure_domain + symptom_class
    Incident identity    = durable ID assigned to one incident episode
    Action identity      = durable operation ID for one approved plan step

A payload hash is an integrity check, not an identity: independent observations can carry
identical payloads, and the same identity arriving with a different payload is a conflict.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field


def payload_hash(payload: dict[str, Any]) -> str:
    """Stable SHA-256 of a payload; used to detect conflicting redelivery, never as identity."""
    canonical = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ObservationIdentity(BaseModel):
    """Identity of one observation. Resending the same event keeps the same identity."""

    tenant: str = "default"
    source: str
    event_id: str

    @property
    def key(self) -> str:
        return f"{self.tenant}|{self.source}|{self.event_id}"


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
        base = f"{self.tenant}|{self.environment}|{self.service}|{self.failure_domain}|{self.symptom_class}"
        if self.extra:
            extras = ",".join(f"{k}={self.extra[k]}" for k in sorted(self.extra))
            return f"{base}|{extras}"
        return base
