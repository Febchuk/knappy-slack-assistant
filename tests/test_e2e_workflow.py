"""End-to-end path from a note, through recall and approval."""

from __future__ import annotations

import pytest

from knappy.db.repository import SqliteRepository
from knappy.runtime import KnappyRuntime
from fakes import HeuristicModel


class Recorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute(self, draft: dict) -> None:
        self.calls.append(draft)


@pytest.mark.asyncio
async def test_note_recall_and_approval(repo: SqliteRepository) -> None:
    recorder = Recorder()
    runtime = KnappyRuntime(repo, workspace_id="T_TEST", model=HeuristicModel(), executor=recorder)
    runtime.bind_user("U1")

    logged = await runtime.handle_event(
        {
            "text": "note: Met with Alex from Acme Corp, promised to send the revised budget by Thursday.",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "1.0",
        }
    )
    assert logged is not None
    assert "Alex" in logged.text
    assert "revised budget" in logged.text

    recalled = await runtime.handle_event(
        {
            "text": "What did I promise to send Alex?",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "2.0",
        }
    )
    assert recalled is not None
    assert recalled.text == "You promised to send Alex the revised budget by Thursday."

    staged = await runtime.handle_event(
        {
            "text": "Follow up with Alex",
            "channel": "D1",
            "channel_type": "im",
            "user": "U1",
            "ts": "3.0",
        }
    )
    assert staged is not None and staged.draft_id is not None
    action_ids = [element["action_id"] for element in staged.blocks[2]["elements"]]
    assert "btn_approve_action" in action_ids
    assert "btn_cancel_action" in action_ids

    approved = await runtime.gateway.approve(staged.draft_id, "U1")
    again = await runtime.gateway.approve(staged.draft_id, "U1")
    assert approved.executed is True
    assert ":white_check_mark:" in approved.replacement_blocks[0]["text"]["text"]
    assert again.status == "ignored"
    assert len(recorder.calls) == 1
