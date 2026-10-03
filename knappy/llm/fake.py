"""Scripted model for tests. Records every request it receives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from knappy.llm.types import Message, ModelTurn, SchemaT, Tier, ToolSpec


@dataclass(frozen=True)
class GenerateRequest:
    tier: Tier
    system: str
    contents: list[Message]
    tools: list[ToolSpec]


Respond = Callable[[GenerateRequest], Awaitable[ModelTurn]]
RespondStructured = Callable[[type[BaseModel], str, str], Awaitable[BaseModel]]


class FakeModel:
    def __init__(
        self,
        respond: Respond | list[ModelTurn] | None = None,
        structured: RespondStructured | None = None,
    ) -> None:
        self._respond = respond
        self._structured = structured
        self.requests: list[GenerateRequest] = []
        self.structured_requests: list[tuple[type[BaseModel], str, str]] = []

    async def generate(
        self,
        *,
        tier: Tier,
        system: str,
        contents: list[Message],
        tools: list[ToolSpec] | None = None,
    ) -> ModelTurn:
        request = GenerateRequest(tier, system, list(contents), list(tools or []))
        self.requests.append(request)
        if self._respond is None:
            return ModelTurn(text="")
        if isinstance(self._respond, list):
            if not self._respond:
                raise AssertionError("FakeModel script exhausted")
            return self._respond.pop(0)
        return await self._respond(request)

    async def generate_structured(
        self,
        *,
        tier: Tier,
        system: str,
        text: str,
        schema: type[SchemaT],
    ) -> SchemaT:
        self.structured_requests.append((schema, system, text))
        if self._structured is None:
            raise AssertionError(f"FakeModel has no structured response for {schema.__name__}")
        result: Any = await self._structured(schema, system, text)
        return schema.model_validate(result.model_dump() if isinstance(result, BaseModel) else result)
