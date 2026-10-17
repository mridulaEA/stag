"""Pydantic models for the STAG pipeline."""

from __future__ import annotations

from datetime import datetime
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


class TriggerType(str, Enum):
    THRESHOLD = "threshold"
    PATTERN = "pattern"
    ANOMALY = "anomaly"
    TEMPORAL = "temporal"
    COMPOSITE = "composite"


class StreamEvent(BaseModel):
    """A single event from a data stream."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    source: str
    event_type: EventType
    severity: Severity = Severity.LOW
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

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
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    trigger_id: str
    event_id: str
    title: str
    summary: str
    detail: str
    severity: Severity
    recommendations: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)


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
