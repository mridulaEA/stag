"""STAG configuration management."""

from __future__ import annotations

import json
from pathlib import Path
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

    # Storage
    chroma_persist_dir: str = "./data/chroma"
    graph_persist_path: str = "./data/graph.json"

    # Trigger
    trigger_eval_interval: float = 2.0
    trigger_anomaly_threshold: float = 0.75

    # Generation
    generation_max_tokens: int = 1024
    generation_temperature: float = 0.3

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: str = '["http://localhost:5173","http://localhost:3000"]'

    # Simulator
    simulator_interval: float = 3.0
    simulator_scenario: str = "enterprise_ai"

    @property
    def cors_origin_list(self) -> list[str]:
        return json.loads(self.cors_origins)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def get_settings() -> Settings:
    return Settings()
