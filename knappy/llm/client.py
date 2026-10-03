"""Gemini client: tool-calling turns and schema-validated structured calls."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from knappy.llm.types import Message, ModelTurn, SchemaT, Tier, ToolCall, ToolResult, ToolSpec, Usage, UserMessage

logger = logging.getLogger("knappy")

# USD per 1M tokens (input, output). Output includes thinking tokens.
PRICES: dict[str, tuple[float, float]] = {
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.1-flash-lite-preview": (0.25, 1.50),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
FALLBACK_PRICE = PRICES["gemini-3-flash-preview"]

TIMEOUTS_S: dict[Tier, float] = {"agent": 30.0, "light": 10.0}
RETRY_DELAYS_S = (0.5, 2.0)

OnUsage = Callable[[Tier, str, Usage], Awaitable[None]]


@dataclass(frozen=True)
class ModelIds:
    agent: str
    light: str

    def for_tier(self, tier: Tier) -> str:
        return self.agent if tier == "agent" else self.light


class GeminiClient:
    def __init__(
        self,
        api_key: str,
        models: ModelIds,
        *,
        on_usage: OnUsage | None = None,
        sdk: Any | None = None,
    ) -> None:
        self.models = models
        self.on_usage = on_usage
        self._sdk = sdk or genai.Client(api_key=api_key)

    async def generate(
        self,
        *,
        tier: Tier,
        system: str,
        contents: list[Message],
        tools: list[ToolSpec] | None = None,
        timeout_s: float | None = None,
    ) -> ModelTurn:
        config = types.GenerateContentConfig(system_instruction=system)
        if tools:
            config.tools = [types.Tool(function_declarations=[_declaration(spec) for spec in tools])]
        response, usage = await self._call(tier, to_contents(contents), config, timeout_s)
        return parse_turn(response, usage)

    async def generate_structured(
        self,
        *,
        tier: Tier,
        system: str,
        text: str,
        schema: type[SchemaT],
        timeout_s: float | None = None,
    ) -> SchemaT:
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
        )
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=text)])]
        last_error: ValidationError | None = None
        for _ in range(2):
            response, _usage = await self._call(tier, contents, config, timeout_s)
            try:
                return schema.model_validate_json(response.text or "")
            except ValidationError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def _call(
        self, tier: Tier, contents: list[types.Content], config: types.GenerateContentConfig, timeout_s: float | None
    ) -> tuple[Any, Usage]:
        model = self.models.for_tier(tier)
        started = time.perf_counter()
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            try:
                response = await asyncio.wait_for(
                    self._sdk.aio.models.generate_content(model=model, contents=contents, config=config),
                    timeout=timeout_s or TIMEOUTS_S[tier],
                )
                break
            except errors.APIError as exc:
                if not _retryable(exc) or attempt == len(RETRY_DELAYS_S):
                    raise
                await asyncio.sleep(RETRY_DELAYS_S[attempt])
        usage = usage_from(model, response.usage_metadata, int((time.perf_counter() - started) * 1000))
        logger.info(
            "model tier=%s model=%s ms=%d in=%d out=%d cached=%d cost=%.6f",
            tier, model, usage.latency_ms, usage.input_tokens, usage.output_tokens,
            usage.cached_tokens, usage.cost_usd,
        )
        if self.on_usage is not None:
            await self.on_usage(tier, model, usage)
        return response, usage


def _retryable(exc: errors.APIError) -> bool:
    return exc.code == 429 or (exc.code or 0) >= 500


def _declaration(spec: ToolSpec) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=spec.name,
        description=spec.description,
        parameters_json_schema=spec.args_model.model_json_schema(),
    )


def to_contents(messages: list[Message]) -> list[types.Content]:
    contents: list[types.Content] = []
    for message in messages:
        if isinstance(message, UserMessage):
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=message.text)]))
        elif isinstance(message, ModelTurn):
            contents.append(message.raw if message.raw is not None else _model_content(message))
        else:
            part = _function_response(message)
            previous = contents[-1] if contents else None
            if previous is not None and previous.role == "user" and _all_function_responses(previous):
                previous.parts.append(part)
            else:
                contents.append(types.Content(role="user", parts=[part]))
    return contents


def _model_content(turn: ModelTurn) -> types.Content:
    parts: list[types.Part] = []
    if turn.text:
        parts.append(types.Part.from_text(text=turn.text))
    for call in turn.tool_calls:
        parts.append(types.Part(function_call=types.FunctionCall(id=call.id, name=call.name, args=call.args)))
    return types.Content(role="model", parts=parts)


def _function_response(result: ToolResult) -> types.Part:
    payload = json.loads(json.dumps({"result": result.result}, default=str))
    return types.Part(
        function_response=types.FunctionResponse(id=result.call.id, name=result.call.name, response=payload)
    )


def _all_function_responses(content: types.Content) -> bool:
    return bool(content.parts) and all(part.function_response is not None for part in content.parts)


def parse_turn(response: Any, usage: Usage) -> ModelTurn:
    candidates = response.candidates or []
    content = candidates[0].content if candidates else None
    parts = (content.parts if content is not None else None) or []
    calls: list[ToolCall] = []
    texts: list[str] = []
    for index, part in enumerate(parts):
        if part.function_call is not None:
            call = part.function_call
            calls.append(ToolCall(id=call.id or f"call_{index}", name=call.name or "", args=dict(call.args or {})))
        elif part.text and not part.thought:
            texts.append(part.text)
    return ModelTurn(text="".join(texts) or None, tool_calls=calls, usage=usage, raw=content)


def usage_from(model: str, metadata: Any, latency_ms: int) -> Usage:
    if metadata is None:
        return Usage(latency_ms=latency_ms)
    input_tokens = metadata.prompt_token_count or 0
    output_tokens = (metadata.candidates_token_count or 0) + (metadata.thoughts_token_count or 0)
    cached = metadata.cached_content_token_count or 0
    price_in, price_out = PRICES.get(model, FALLBACK_PRICE)
    cost = (input_tokens * price_in + output_tokens * price_out) / 1_000_000
    return Usage(input_tokens, output_tokens, cached, cost, latency_ms)
