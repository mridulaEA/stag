"""Prompt templates for insight generation."""

SYSTEM_PROMPT = """You are STAG, an enterprise AI security analyst. You analyze stream events, \
trigger signals, and retrieved context to generate actionable security insights.

Your role:
- Analyze events from enterprise data streams (AI tool usage, data transfers, policy violations)
- Assess risk severity based on trigger signals and historical context
- Provide clear, actionable recommendations for security and compliance teams
- Reference specific evidence from retrieved context

Output format: Structured JSON with title, summary, detail, severity, recommendations, and confidence."""

INSIGHT_PROMPT_TEMPLATE = """Analyze the following triggered event and generate a security insight.

## Triggered Event
- **Type**: {event_type}
- **Severity**: {event_severity}
- **Source**: {event_source}
- **Payload**: {event_payload}

## Trigger Signal
- **Rule**: {trigger_rule}
- **Confidence**: {trigger_confidence}
- **Reason**: {trigger_reason}

## Retrieved Context (from knowledge base)
{retrieved_context}

## Instructions
Generate a JSON response with exactly these fields:
{{
    "title": "Brief title of the insight (max 100 chars)",
    "summary": "2-3 sentence executive summary of the finding",
    "detail": "Detailed analysis paragraph covering: what happened, why it matters, risk implications, and connections to other events found in context",
    "severity": "critical|high|medium|low",
    "recommendations": ["action 1", "action 2", "action 3"],
    "confidence": 0.0-1.0
}}

Respond with ONLY valid JSON, no markdown or explanation."""

FALLBACK_INSIGHT_TEMPLATE = """{{
    "title": "Triggered Alert: {trigger_rule}",
    "summary": "{trigger_reason}. Event from {event_source} with {event_severity} severity detected by the {trigger_rule} rule.",
    "detail": "An event was detected at {event_source} with {event_severity} severity. The {trigger_rule} trigger fired with {trigger_confidence} confidence. Reason: {trigger_reason}. Payload contained: {event_payload_summary}. This event should be reviewed by the security team to assess potential risk and determine appropriate response.",
    "severity": "{event_severity}",
    "recommendations": ["Review the event details and assess the scope of impact", "Check for similar events from the same source or user", "Apply appropriate governance controls based on the risk classification"],
    "confidence": {trigger_confidence}
}}"""
