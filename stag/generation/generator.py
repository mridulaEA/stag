"""LLM-based insight generator.

Supports OpenAI and Anthropic as LLM providers, with a local fallback
for when no API key is configured (template-based generation).
"""

from __future__ import annotations

import json

import structlog

from stag.config import Settings
from stag.generation.prompts import (
    FALLBACK_INSIGHT_TEMPLATE,
    INSIGHT_PROMPT_TEMPLATE,
    SYSTEM_PROMPT,
)
from stag.models import GeneratedInsight, RetrievalResult, StreamEvent, TriggerResult
from stag.models.schemas import Severity

logger = structlog.get_logger()


class InsightGenerator:
    """Generates insights from triggered events using LLM or fallback templates."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._provider = settings.llm_provider
        self._client = None
        self._generation_count = 0

    async def initialize(self) -> None:
        """Initialize the LLM client."""
        if self._provider == "openai" and self.settings.openai_api_key:
            try:
                from openai import AsyncOpenAI
                self._client = AsyncOpenAI(api_key=self.settings.openai_api_key)
                logger.info("generator.initialized", provider="openai", model=self.settings.openai_model)
            except ImportError:
                logger.warning("generator.openai_not_installed")
                self._client = None
        elif self._provider == "anthropic" and self.settings.anthropic_api_key:
            try:
                from anthropic import AsyncAnthropic
                self._client = AsyncAnthropic(api_key=self.settings.anthropic_api_key)
                logger.info("generator.initialized", provider="anthropic", model=self.settings.anthropic_model)
            except ImportError:
                logger.warning("generator.anthropic_not_installed")
                self._client = None
        else:
            logger.info("generator.using_fallback", reason="no API key configured")
            self._client = None

    async def generate(
        self,
        event: StreamEvent,
        trigger: TriggerResult,
        retrieval: RetrievalResult,
    ) -> GeneratedInsight:
        """Generate an insight from a triggered event with retrieved context."""
        self._generation_count += 1

        # Build context from retrieved results
        context_parts = []
        for i, result in enumerate(retrieval.fused_results[:8], 1):
            sources = ", ".join(result.get("sources", []))
            context_parts.append(f"{i}. [{sources}] (score: {result['score']:.2f}) {result['text']}")
        retrieved_context = "\n".join(context_parts) if context_parts else "No prior context available."

        # Build prompt
        prompt = INSIGHT_PROMPT_TEMPLATE.format(
            event_type=event.event_type.value,
            event_severity=event.severity.value,
            event_source=event.source,
            event_payload=json.dumps(event.payload, default=str)[:500],
            trigger_rule=trigger.rule_name,
            trigger_confidence=trigger.confidence,
            trigger_reason=trigger.reason,
            retrieved_context=retrieved_context,
        )

        # Generate via LLM or fallback, then validate at one boundary.
        mode = "fallback"
        degraded_reason = None
        if self._client:
            insight_data = await self._call_llm(prompt)
            if isinstance(insight_data, dict) and insight_data:
                mode = "llm"
            else:
                mode = "degraded"
                degraded_reason = "provider error or empty response; template output used"
                insight_data = self._fallback_generate(event, trigger)
        else:
            insight_data = self._fallback_generate(event, trigger)

        return self._build_insight(event, trigger, retrieval, insight_data, mode, degraded_reason)

    def _build_insight(
        self,
        event: StreamEvent,
        trigger: TriggerResult,
        retrieval: RetrievalResult,
        data: object,
        mode: str,
        degraded_reason: str | None,
    ) -> GeneratedInsight:
        """Coerce arbitrary model output into a valid insight; malformed shapes degrade explicitly."""
        if not isinstance(data, dict):
            data = {}
            mode, degraded_reason = "degraded", f"model returned {type(data).__name__}, expected object"

        def text(key: str, default: str, limit: int) -> str:
            value = data.get(key)
            return (value if isinstance(value, str) and value.strip() else default)[:limit]

        recs = data.get("recommendations")
        if isinstance(recs, list):
            recommendations = [str(r)[:300] for r in recs if r is not None][:10]
        else:
            recommendations = []
            if recs is not None:
                degraded_reason = degraded_reason or "recommendations was not a list"

        try:
            confidence = float(data.get("confidence", trigger.confidence))
        except (TypeError, ValueError):
            confidence = trigger.confidence
            degraded_reason = degraded_reason or "non-numeric confidence"
        if confidence != confidence or not (0.0 <= confidence <= 1.0):  # NaN or out of range
            confidence = max(0.0, min(1.0, trigger.confidence))
            degraded_reason = degraded_reason or "confidence out of range"
        if degraded_reason and mode == "llm":
            mode = "degraded"

        return GeneratedInsight(
            trigger_id=trigger.trigger_id,
            event_id=event.id,
            title=text("title", f"Alert: {trigger.rule_name}", 200),
            summary=text("summary", trigger.reason, 1000),
            detail=text("detail", "", 5000),
            severity=self._parse_severity(data.get("severity")) or event.severity,
            recommendations=recommendations,
            # Retrieved ids are what was *shown* to the model, not claim-level grounding.
            sources=[str(r.get("id", "")) for r in retrieval.fused_results[:5]],
            confidence=confidence,
            generation_mode=mode,
            degraded_reason=degraded_reason,
        )

    async def _call_llm(self, prompt: str) -> dict:
        """Call the configured LLM provider."""
        try:
            if self._provider == "openai":
                response = await self._client.chat.completions.create(
                    model=self.settings.openai_model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    max_tokens=self.settings.generation_max_tokens,
                    temperature=self.settings.generation_temperature,
                    response_format={"type": "json_object"},
                )
                content = response.choices[0].message.content
                return json.loads(content)

            elif self._provider == "anthropic":
                response = await self._client.messages.create(
                    model=self.settings.anthropic_model,
                    system=SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=self.settings.generation_max_tokens,
                    temperature=self.settings.generation_temperature,
                )
                content = response.content[0].text
                # Strip markdown code fences if present
                if content.startswith("```"):
                    content = content.split("\n", 1)[1].rsplit("```", 1)[0]
                return json.loads(content)

        except Exception as e:
            logger.error("generator.llm_error", provider=self._provider, error=str(e))
            return {}

    def _fallback_generate(self, event: StreamEvent, trigger: TriggerResult) -> dict:
        """Template-based generation when no LLM is available."""
        payload_summary = ", ".join(f"{k}={v}" for k, v in list(event.payload.items())[:5])
        filled = FALLBACK_INSIGHT_TEMPLATE.format(
            trigger_rule=trigger.rule_name,
            trigger_reason=trigger.reason,
            event_source=event.source,
            event_severity=event.severity.value,
            trigger_confidence=trigger.confidence,
            event_payload_summary=payload_summary[:200],
        )
        try:
            return json.loads(filled)
        except json.JSONDecodeError:
            return {
                "title": f"Alert: {trigger.rule_name}",
                "summary": trigger.reason,
                "detail": f"Event from {event.source} triggered {trigger.rule_name}.",
                "severity": event.severity.value,
                "recommendations": ["Review event details", "Check for similar events"],
                "confidence": trigger.confidence,
            }

    @staticmethod
    def _parse_severity(value: object) -> Severity | None:
        if not isinstance(value, str):
            return None
        try:
            return Severity(value.lower())
        except ValueError:
            return None

    @property
    def stats(self) -> dict:
        return {
            "provider": self._provider,
            "has_client": self._client is not None,
            "generations": self._generation_count,
        }
