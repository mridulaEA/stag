"""STAG configuration management."""

from __future__ import annotations

import json
from typing import Literal

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # LLM
    llm_provider: Literal["openai", "anthropic"] = "openai"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"

    # Embedding
    embedding_model: str = "all-MiniLM-L6-v2"

    # Stream
    stream_source: Literal["simulator", "kafka", "webhook"] = "simulator"
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_topic: str = "stag-events"
    kafka_group_id: str = "stag-consumer"
    # Offsets are committed only after the pipeline acknowledges a durable outcome (R17).
    kafka_id_field: str = "id"  # records must carry the source's own stable event id; none is minted
    kafka_max_record_bytes: int = 1_000_000
    kafka_dead_letter_max: int = 1000  # bounded in-memory buffer of poison-record coordinates
    kafka_on_failure: Literal["dead_letter", "halt"] = "dead_letter"  # halt = stop and redeliver after restart

    # Storage
    chroma_persist_dir: str = "./data/chroma"
    graph_persist_path: str = "./data/graph.json"

    # Trigger
    trigger_eval_interval: float = 2.0
    trigger_anomaly_threshold: float = 0.75

    # Generation
    generation_max_tokens: int = 1024
    generation_temperature: float = 0.3

    # Server (local demo: no authentication → loopback by default; set HOST=0.0.0.0 deliberately)
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: str = '["http://localhost:5173","http://localhost:3000"]'

    # TechOps coordinator (deterministic backbone)
    techops_coordinator_enabled: bool = False
    techops_tenant: str = "default"
    techops_investigation_cooldown_seconds: int = 300
    techops_recurrence_window_seconds: int = 3600
    techops_dedup_retention: int = 50000
    techops_processing_mode: Literal["live", "replay"] = "live"
    index_timeout_seconds: float = 5.0
    # Durable control state (observations, incidents, investigation requests, action ledger).
    # "" or "memory://" → process-local (lost on restart); "sqlite:///./data/control.sqlite" → single
    # host, survives restarts; "postgresql://user:pass@host/db" → multi-worker (needs psycopg).
    control_store_url: str = ""
    # Identifies this worker when leasing investigation requests; defaults to "<hostname>:<pid>".
    techops_worker_id: str = ""
    techops_investigation_lease_seconds: int = 300

    # Simulator
    simulator_interval: float = 3.0
    simulator_scenario: str = "enterprise_ai"

    @property
    def cors_origin_list(self) -> list[str]:
        return json.loads(self.cors_origins)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def get_settings() -> Settings:
    return Settings()
