"""Event simulator for demo and testing.

Generates realistic enterprise AI usage events covering shadow AI activity,
data exposure, policy violations, and normal tool usage patterns.
"""

from __future__ import annotations

import asyncio
import random

import structlog

from stag.models import StreamEvent
from stag.models.schemas import EventType, Severity
from stag.streams.base import StreamSource

logger = structlog.get_logger()

# --- Scenario Data ---

DEPARTMENTS = [
    "Engineering", "Marketing", "Finance", "Legal", "HR",
    "Sales", "Product", "Data Science", "Operations", "Executive",
]

SHADOW_AI_TOOLS = [
    "ChatGPT", "Claude", "Gemini", "Midjourney", "GitHub Copilot",
    "Jasper AI", "Copy.ai", "Perplexity", "Notion AI", "Cursor",
    "DeepSeek", "Hugging Face", "Runway ML", "Synthesia", "Otter.ai",
]

SANCTIONED_TOOLS = [
    "Microsoft Copilot", "Azure OpenAI", "Bedrock (Claude Enterprise)",
]

DATA_TYPES = [
    "source_code", "customer_pii", "financial_data", "legal_documents",
    "employee_records", "strategy_documents", "api_keys", "credentials",
    "client_contracts", "product_roadmap", "meeting_transcripts",
    "marketing_copy", "research_notes", "support_tickets",
]

SENSITIVE_DATA_TYPES = {
    "source_code", "customer_pii", "financial_data", "legal_documents",
    "employee_records", "api_keys", "credentials", "client_contracts",
}

DOMAINS = [
    "chat.openai.com", "claude.ai", "gemini.google.com", "api.openai.com",
    "api.anthropic.com", "copilot.github.com", "app.jasper.ai",
    "perplexity.ai", "notion.so", "cursor.sh", "huggingface.co",
]


def _random_user(department: str) -> str:
    first_names = ["Alex", "Jordan", "Sam", "Morgan", "Casey", "Taylor", "Riley", "Quinn", "Drew", "Avery"]
    last_names = ["Chen", "Patel", "Johnson", "Kim", "Garcia", "Williams", "Davis", "Miller", "Wilson", "Lee"]
    return f"{random.choice(first_names)}.{random.choice(last_names)}@corp.example.com"


def _generate_event() -> StreamEvent:
    """Generate a single realistic enterprise AI event."""
    scenario = random.choices(
        ["shadow_usage", "data_exposure", "policy_violation", "normal_usage",
         "network_anomaly", "auth_event", "data_transfer"],
        weights=[30, 20, 15, 15, 8, 7, 5],
        k=1,
    )[0]

    department = random.choice(DEPARTMENTS)
    user = _random_user(department)

    if scenario == "shadow_usage":
        tool = random.choice(SHADOW_AI_TOOLS)
        data_type = random.choice(DATA_TYPES)
        is_sensitive = data_type in SENSITIVE_DATA_TYPES
        severity = Severity.HIGH if is_sensitive else Severity.MEDIUM
        return StreamEvent(
            source=f"network/{random.choice(DOMAINS)}",
            event_type=EventType.TOOL_USAGE,
            severity=severity,
            payload={
                "tool": tool,
                "sanctioned": False,
                "department": department,
                "user": user,
                "data_type": data_type,
                "data_size_kb": random.randint(1, 5000),
                "action": random.choice(["prompt", "upload", "paste", "api_call"]),
                "description": f"{user} used {tool} with {data_type} data",
            },
            metadata={"scenario": "shadow_ai", "sensitive": is_sensitive},
        )

    elif scenario == "data_exposure":
        tool = random.choice(SHADOW_AI_TOOLS)
        data_type = random.choice(list(SENSITIVE_DATA_TYPES))
        return StreamEvent(
            source=f"dlp/{tool.lower().replace(' ', '-')}",
            event_type=EventType.DATA_TRANSFER,
            severity=Severity.CRITICAL,
            payload={
                "tool": tool,
                "department": department,
                "user": user,
                "data_type": data_type,
                "data_size_kb": random.randint(50, 10000),
                "destination": random.choice(DOMAINS),
                "dlp_rule": f"DETECT_{data_type.upper()}",
                "match_count": random.randint(1, 50),
                "description": f"Sensitive {data_type} detected in data sent to {tool}",
            },
            metadata={"scenario": "data_exposure", "sensitive": True},
        )

    elif scenario == "policy_violation":
        tool = random.choice(SHADOW_AI_TOOLS)
        policy = random.choice([
            "AI_ACCEPTABLE_USE", "DATA_CLASSIFICATION",
            "VENDOR_SECURITY", "NO_PII_EXTERNAL",
        ])
        return StreamEvent(
            source=f"policy/{policy.lower()}",
            event_type=EventType.POLICY_VIOLATION,
            severity=random.choice([Severity.HIGH, Severity.CRITICAL]),
            payload={
                "tool": tool,
                "department": department,
                "user": user,
                "policy": policy,
                "violation_detail": f"Usage of {tool} violates {policy} policy",
                "recommended_action": random.choice(["block", "warn", "escalate"]),
                "description": f"Policy violation: {user} used {tool} against {policy}",
            },
            metadata={"scenario": "policy_violation"},
        )

    elif scenario == "normal_usage":
        tool = random.choice(SANCTIONED_TOOLS)
        data_type = random.choice(["marketing_copy", "research_notes", "support_tickets", "meeting_transcripts"])
        return StreamEvent(
            source=f"approved/{tool.lower().replace(' ', '-')}",
            event_type=EventType.TOOL_USAGE,
            severity=Severity.LOW,
            payload={
                "tool": tool,
                "sanctioned": True,
                "department": department,
                "user": user,
                "data_type": data_type,
                "data_size_kb": random.randint(1, 500),
                "action": "prompt",
                "description": f"{user} used sanctioned tool {tool}",
            },
            metadata={"scenario": "normal_usage", "sensitive": False},
        )

    elif scenario == "network_anomaly":
        domain = random.choice(DOMAINS)
        return StreamEvent(
            source=f"network/{domain}",
            event_type=EventType.NETWORK_TRAFFIC,
            severity=Severity.HIGH,
            payload={
                "domain": domain,
                "department": department,
                "user": user,
                "bytes_sent": random.randint(100_000, 50_000_000),
                "bytes_received": random.randint(10_000, 5_000_000),
                "request_count": random.randint(50, 500),
                "time_window_minutes": 5,
                "description": f"Unusual traffic volume to {domain} from {department}",
            },
            metadata={"scenario": "network_anomaly"},
        )

    elif scenario == "auth_event":
        tool = random.choice(SHADOW_AI_TOOLS + SANCTIONED_TOOLS)
        return StreamEvent(
            source=f"iam/{tool.lower().replace(' ', '-')}",
            event_type=EventType.AUTHENTICATION,
            severity=Severity.MEDIUM,
            payload={
                "tool": tool,
                "department": department,
                "user": user,
                "auth_method": random.choice(["oauth", "api_key", "email_password", "sso"]),
                "success": random.choice([True, True, True, False]),
                "ip_address": f"10.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}",
                "description": f"Authentication event for {tool} by {user}",
            },
            metadata={"scenario": "auth_event"},
        )

    else:  # data_transfer
        tool = random.choice(SHADOW_AI_TOOLS)
        data_type = random.choice(DATA_TYPES)
        return StreamEvent(
            source=f"proxy/{random.choice(DOMAINS)}",
            event_type=EventType.DATA_TRANSFER,
            severity=Severity.HIGH if data_type in SENSITIVE_DATA_TYPES else Severity.MEDIUM,
            payload={
                "tool": tool,
                "department": department,
                "user": user,
                "data_type": data_type,
                "data_size_kb": random.randint(100, 20000),
                "direction": "outbound",
                "description": f"Data transfer of {data_type} to {tool} by {user}",
            },
            metadata={"scenario": "data_transfer", "sensitive": data_type in SENSITIVE_DATA_TYPES},
        )


class EventSimulator(StreamSource):
    """Generates simulated enterprise AI events for demo purposes."""

    def __init__(self, interval: float = 3.0) -> None:
        super().__init__()
        self.interval = interval

    async def connect(self) -> None:
        logger.info("event_simulator.connected", interval=self.interval)

    async def disconnect(self) -> None:
        logger.info("event_simulator.disconnected")

    async def _read_events(self) -> None:
        """Continuously generate simulated events."""
        logger.info("event_simulator.started")
        while self._running:
            event = _generate_event()
            await self._queue.put(event)
            logger.debug(
                "event_simulator.generated",
                event_id=event.id,
                event_type=event.event_type.value,
                severity=event.severity.value,
            )
            # Add jitter to make timing more realistic
            jitter = random.uniform(0.5, 1.5)
            await asyncio.sleep(self.interval * jitter)
