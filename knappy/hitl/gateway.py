"""Two-phase approval. Compare-and-swap claims a draft before any external call."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from knappy.db.repository import SqliteRepository
from knappy.hitl.blocks import cancelled_blocks, edit_modal, failed_blocks, receipt_blocks

logger = logging.getLogger("knappy")

UNAUTHORIZED = "Unauthorized: Only the creator of this request can approve it."


class ActionExecutor(Protocol):
    async def execute(self, draft: dict[str, Any]) -> None: ...


@dataclass
class HitlResult:
    ok: bool
    status: str
    ephemeral: str | None = None
    replacement_blocks: list[dict[str, Any]] | None = None
    modal: dict[str, Any] | None = None
    executed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class ApprovalGateway:
    def __init__(self, repo: SqliteRepository, executor: ActionExecutor) -> None:
        self.repo = repo
        self.executor = executor

    async def approve(self, draft_id: str, user_id: str) -> HitlResult:
        draft = await self.repo.get_draft(draft_id)
        if draft is None:
            return HitlResult(ok=False, status="missing")
        if draft["user_id"] != user_id:
            return HitlResult(ok=False, status="PENDING", ephemeral=UNAUTHORIZED)
        if not await self.repo.cas_approve(draft_id):
            return HitlResult(ok=False, status="ignored")
        payload = draft["payload"]
        described = {
            "action_type": draft.get("action_type"),
            "file_name": (payload.get("metadata") or {}).get("file_name"),
        }
        try:
            await self.executor.execute(draft)
        except Exception:
            logger.exception("approved draft failed draft=%s action=%s", draft_id, draft.get("action_type"))
            await self.repo.mark_failed(draft_id)
            return HitlResult(
                ok=False,
                status="FAILED",
                replacement_blocks=failed_blocks(payload["recipient_name"], **described),
            )
        await self.repo.mark_executed(draft_id)
        return HitlResult(
            ok=True,
            status="APPROVED",
            executed=True,
            replacement_blocks=receipt_blocks(user_id, payload["recipient_name"], **described),
        )

    async def cancel(self, draft_id: str, user_id: str) -> HitlResult:
        draft = await self.repo.get_draft(draft_id)
        if draft is None:
            return HitlResult(ok=False, status="missing")
        if draft["user_id"] != user_id:
            return HitlResult(ok=False, status=draft["status"], ephemeral=UNAUTHORIZED)
        cancelled = await self.repo.cancel_draft(draft_id)
        if not cancelled:
            return HitlResult(ok=False, status="ignored")
        return HitlResult(ok=True, status="CANCELLED", replacement_blocks=cancelled_blocks())

    async def open_edit(self, draft_id: str, user_id: str) -> HitlResult:
        draft = await self.repo.get_draft(draft_id)
        if draft is None or draft["user_id"] != user_id:
            return HitlResult(ok=False, status="unauthorized", ephemeral=UNAUTHORIZED)
        content = draft["payload"].get("staged_content", "")
        return HitlResult(ok=True, status="PENDING", modal=edit_modal(draft_id, content))

    async def save_edit(self, draft_id: str, user_id: str, staged_content: str) -> HitlResult:
        draft = await self.repo.get_draft(draft_id)
        if draft is None or draft["user_id"] != user_id:
            return HitlResult(ok=False, status="unauthorized", ephemeral=UNAUTHORIZED)
        saved = await self.repo.update_draft_content(draft_id, staged_content)
        return HitlResult(ok=saved, status="PENDING" if saved else "ignored")
