"""Typed conversation and model contract shared by the Gemini client and test fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar, Union

from pydantic import BaseModel

Tier = Literal["agent", "light"]
Recency = Literal["any", "week", "month"]
SchemaT = TypeVar("SchemaT", bound=BaseModel)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0


@dataclass
class ModelTurn:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    # Provider content echoed back verbatim on the next turn (Gemini thought signatures).
    raw: Any = None


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class ToolResult:
    call: ToolCall
    result: Any


Message = Union[UserMessage, ModelTurn, ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]


class Source(BaseModel):
    title: str
    url: str


class WebSearchResult(BaseModel):
    """A grounded answer and the pages it was grounded on (Spec 14 §2)."""

    answer: str
    sources: list[Source]
    searched_queries: list[str]


class Model(Protocol):
    async def generate(
        self,
        *,
        tier: Tier,
        system: str,
        contents: list[Message],
        tools: list[ToolSpec] | None = None,
        timeout_s: float | None = None,
    ) -> ModelTurn: ...

    async def generate_structured(
        self,
        *,
        tier: Tier,
        system: str,
        text: str,
        schema: type[SchemaT],
        timeout_s: float | None = None,
    ) -> SchemaT: ...

    async def search(self, query: str, recency: Recency = "any") -> WebSearchResult: ...
