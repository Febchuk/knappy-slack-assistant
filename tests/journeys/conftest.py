"""Journey fixtures: an offline factory, and the live Gemini model with a running cost tally."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from fakes import FakeClock
from journey import START, Journey
from knappy.llm.types import Model

LIVE_COST_USD: list[float] = []


@pytest.fixture
async def journey(tmp_path):
    """Builds and starts journeys; closes every one at teardown, even when the test fails."""
    opened: list[Journey] = []

    async def open_(model: Model, *, at: datetime = START, **kwargs) -> Journey:
        opened.append(await Journey(tmp_path, model=model, clock=FakeClock(at), **kwargs).start())
        return opened[-1]

    yield open_
    for item in opened:
        await item.close()


@pytest.fixture
def live_model():
    from knappy.config import DEFAULT_MODEL_AGENT, DEFAULT_MODEL_LIGHT, load_dotenv
    from knappy.llm.client import GeminiClient, ModelIds

    load_dotenv()
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        pytest.skip("GEMINI_API_KEY not set")

    async def tally(tier, model, usage) -> None:
        LIVE_COST_USD.append(usage.cost_usd)

    return GeminiClient(
        key,
        ModelIds(
            agent=os.environ.get("KNAPPY_MODEL_AGENT") or DEFAULT_MODEL_AGENT,
            light=os.environ.get("KNAPPY_MODEL_LIGHT") or DEFAULT_MODEL_LIGHT,
        ),
        on_usage=tally,
    )


def pytest_terminal_summary(terminalreporter) -> None:
    if LIVE_COST_USD:
        terminalreporter.write_line(
            f"live model: {len(LIVE_COST_USD)} calls, ${sum(LIVE_COST_USD):.4f} total"
        )
