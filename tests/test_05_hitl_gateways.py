"""Spec 05: staged drafts, authorization, and idempotent execution."""

from __future__ import annotations

import pytest

from knappy.db.repository import SqliteRepository
from knappy.hitl.blocks import approval_blocks
from knappy.hitl.gateway import ApprovalGateway


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def execute(self, draft: dict) -> None:
        self.calls.append(draft)
        if self.fail:
            raise RuntimeError("provider down")


async def _pending(repo: SqliteRepository, user_id: str = "U1") -> str:
    return await repo.create_draft(
        workspace_id="T_TEST",
        user_id=user_id,
        channel_id="D1",
        action_type="SEND_SLACK_DM",
        payload={
            "action_type": "SEND_SLACK_DM",
            "recipient_identifier": "U_ALEX",
            "recipient_name": "Alex",
            "preview_summary": "Follow up",
            "staged_content": "Could you send the deck?",
            "metadata": {},
        },
    )


@pytest.mark.asyncio
async def test_hitl_01_stage_draft(repo: SqliteRepository) -> None:
    draft_id = await _pending(repo)
    draft = await repo.get_draft(draft_id)
    assert draft is not None
    assert draft["status"] == "PENDING"
    blocks = approval_blocks(draft_id, "Alex", "Could you send the deck?")
    action_ids = [element["action_id"] for element in blocks[2]["elements"]]
    assert action_ids == ["btn_approve_action", "btn_edit_draft", "btn_cancel_action"]


@pytest.mark.asyncio
async def test_hitl_02_authorized_approval(repo: SqliteRepository) -> None:
    recorder = Recorder()
    gateway = ApprovalGateway(repo, recorder)
    draft_id = await _pending(repo)
    result = await gateway.approve(draft_id, "U1")
    draft = await repo.get_draft(draft_id)
    assert result.executed is True
    assert draft["status"] == "APPROVED"
    assert draft["executed_at"] is not None
    assert len(recorder.calls) == 1
    assert ":white_check_mark:" in result.replacement_blocks[0]["text"]["text"]


@pytest.mark.asyncio
async def test_hitl_03_unauthorized(repo: SqliteRepository) -> None:
    recorder = Recorder()
    gateway = ApprovalGateway(repo, recorder)
    draft_id = await _pending(repo)
    result = await gateway.approve(draft_id, "U_OTHER")
    draft = await repo.get_draft(draft_id)
    assert result.ephemeral is not None and "Unauthorized" in result.ephemeral
    assert draft["status"] == "PENDING"
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_hitl_04_double_click(repo: SqliteRepository) -> None:
    recorder = Recorder()
    gateway = ApprovalGateway(repo, recorder)
    draft_id = await _pending(repo)
    first = await gateway.approve(draft_id, "U1")
    second = await gateway.approve(draft_id, "U1")
    assert first.executed is True
    assert second.status == "ignored"
    assert len(recorder.calls) == 1


@pytest.mark.asyncio
async def test_hitl_05_cancel(repo: SqliteRepository) -> None:
    recorder = Recorder()
    gateway = ApprovalGateway(repo, recorder)
    draft_id = await _pending(repo)
    result = await gateway.cancel(draft_id, "U1")
    draft = await repo.get_draft(draft_id)
    assert result.status == "CANCELLED"
    assert draft["status"] == "CANCELLED"
    assert recorder.calls == []
    assert ":x:" in result.replacement_blocks[0]["text"]["text"]


@pytest.mark.asyncio
async def test_hitl_failure_sets_failed(repo: SqliteRepository) -> None:
    gateway = ApprovalGateway(repo, Recorder(fail=True))
    draft_id = await _pending(repo)
    result = await gateway.approve(draft_id, "U1")
    draft = await repo.get_draft(draft_id)
    assert result.status == "FAILED"
    assert draft["status"] == "FAILED"
    assert draft["executed_at"] is None
