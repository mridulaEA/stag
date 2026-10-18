"""Pydantic models for the STAG pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class EventType(str, Enum):
    DATA_ACCESS = "data_access"
    TOOL_USAGE = "tool_usage"
    ANOMALY = "anomaly"
    POLICY_VIOLATION = "policy_violation"
    NETWORK_TRAFFIC = "network_traffic"
    AUTHENTICATION = "authentication"
    DATA_TRANSFER = "data_transfer"
    CONFIGURATION_CHANGE = "configuration_change"
    # Operational streams used by crews
    LOG = "log"
    METRIC = "metric"
    ALERT = "alert"
    DEPLOYMENT = "deployment"
    TICKET = "ticket"
    CUSTOMER_FEEDBACK = "customer_feedback"
    PIPELINE_RUN = "pipeline_run"
    DATA_QUALITY = "data_quality"
    AUTOMATION_RUN = "automation_run"


class TriggerType(str, Enum):
    THRESHOLD = "threshold"
    PATTERN = "pattern"
    ANOMALY = "anomaly"
    TEMPORAL = "temporal"
    COMPOSITE = "composite"


class StreamEvent(BaseModel):
    """A single event from a data stream."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str
    event_type: EventType
    severity: Severity = Severity.LOW
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def doc_key(self) -> str:
        """Canonical source-scoped key for knowledge stores (JSON array, collision-free)."""
        import json

        return json.dumps([self.source, self.id], separators=(",", ":"), ensure_ascii=False)

    @property
    def is_replay(self) -> bool:
        """Historical replay is isolated from live incidents, paging and actions."""
        return bool(self.metadata.get("replay"))

    @property
    def event_time(self) -> datetime:
        """Timezone-aware occurrence time (naive timestamps are treated as UTC)."""
        ts = self.timestamp
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)

    @property
    def text(self) -> str:
        """Flat text representation for embedding and retrieval."""
        parts = [
            f"Source: {self.source}",
            f"Type: {self.event_type.value}",
            f"Severity: {self.severity.value}",
        ]
        for k, v in self.payload.items():
            parts.append(f"{k}: {v}")
        return " | ".join(parts)


class TriggerResult(BaseModel):
    """Result of trigger evaluation on a stream event."""

    trigger_id: str = Field(default_factory=lambda: str(uuid4()))
    trigger_type: TriggerType
    rule_name: str
    fired: bool
    confidence: float = Field(ge=0.0, le=1.0)
    event_id: str
    reason: str
    context: dict[str, Any] = Field(default_factory=dict)


class GraphNode(BaseModel):
    """A node in the knowledge graph."""

    id: str
    label: str
    node_type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """An edge in the knowledge graph."""

    source: str
    target: str
    relation: str
    weight: float = 1.0
    properties: dict[str, Any] = Field(default_factory=dict)


class RetrievalResult(BaseModel):
    """Result from hybrid retrieval."""

    query: str
    vector_results: list[dict[str, Any]] = Field(default_factory=list)
    graph_results: list[dict[str, Any]] = Field(default_factory=list)
    sparse_results: list[dict[str, Any]] = Field(default_factory=list)
    fused_results: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_time_ms: float = 0.0


class GeneratedInsight(BaseModel):
    """LLM-generated insight from the pipeline."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trigger_id: str
    event_id: str
    title: str
    summary: str
    detail: str
    severity: Severity
    recommendations: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    generation_mode: str = "fallback"  # llm | fallback | degraded
    degraded_reason: str | None = None


class PipelineOutput(BaseModel):
    """Complete output from a single pipeline execution."""

    event: StreamEvent
    trigger: TriggerResult
    retrieval: RetrievalResult
    insight: GeneratedInsight
    pipeline_time_ms: float = 0.0
    incident_id: str | None = None
    investigation_request_id: str | None = None
    material_change: str | None = None
