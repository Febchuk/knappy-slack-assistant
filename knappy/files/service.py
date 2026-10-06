"""Files in and out (Spec 15). Shared files are downloaded, read, stored as documents with a memory record, and
handed to the agent for the turn. Long deliverables go back to the requester's DM as an uploaded file."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from html import escape
from pathlib import PurePosixPath
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from knappy.agent.tools import SlackThread
from knappy.files.extract import MAX_TEXT_CHARS, Piece, UnreadableError, extract, image_mime, kind_of
from knappy.files.store import Document, DocumentStore, NewDocument, chunks, embed_chunks
from knappy.llm.types import Attachment, Model, UserMessage
from knappy.memory.store import MemoryStore, slugify

logger = logging.getLogger("knappy")

MAX_BYTES = 20 * 1024 * 1024
INLINE_CHARS = 100_000
DIGEST_INPUT_CHARS = 60_000
DOWNLOAD_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 60.0
CONCURRENT_READS = 4
DocumentFormat = Literal["md", "txt", "csv"]
FORMAT_MIMES: dict[str, str] = {"md": "text/markdown", "txt": "text/plain", "csv": "text/csv"}

DIGEST_PROMPT = (
    "You index a document a user shared with their personal assistant, so it can be found and used later. "
    "Write a summary of 3 to 6 sentences: what the document is, and its key points, figures, dates, and names. "
    "Then list up to 8 key terms someone would search for to find it: topics, names, products, places."
)
DESCRIBE_PROMPT = (
    "Describe this image for someone who cannot see it: what it shows and what matters in it. "
    "Transcribe any visible text exactly. Plain text, no preamble."
)
TRANSCRIBE_PROMPT = "Transcribe all text on this scanned page exactly, in reading order. Return only the text."

StatusFn = Callable[[str], Awaitable[None]]


class DocumentDigest(BaseModel):
    summary: str = Field(..., description="3-6 sentences")
    key_terms: list[str] = Field(default_factory=list, description="Up to 8 search terms")


@dataclass(frozen=True)
class SlackFile:
    id: str
    name: str
    mimetype: str
    size: int | None
    url: str | None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> SlackFile:
        size = raw.get("size")
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or raw.get("title") or "file"),
            mimetype=str(raw.get("mimetype") or ""),
            size=int(size) if isinstance(size, int | float) else None,
            url=raw.get("url_private_download") or raw.get("url_private"),
        )


class DownloadError(Exception):
    pass


class TooLarge(DownloadError):
    pass


def size_limit_text(name: str) -> str:
    return f"I can't read *{name}*: files over {MAX_BYTES // (1024 * 1024)} MB are too large for me."


class SlackDownloader:
    """GET a Slack file with the bot token, reading at most MAX_BYTES. The token only goes to slack.com hosts."""

    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None, timeout_s: float = DOWNLOAD_TIMEOUT_S) -> None:
        self.token = token
        self.transport = transport
        self.timeout_s = timeout_s

    async def download(self, file: SlackFile) -> bytes:
        url = httpx.URL(file.url or "")
        if url.scheme != "https" or not (url.host == "slack.com" or url.host.endswith(".slack.com")):
            raise DownloadError("it isn't a Slack file link")
        async with httpx.AsyncClient(
            transport=self.transport, timeout=self.timeout_s, follow_redirects=True, trust_env=False,
            headers={"Authorization": f"Bearer {self.token}"},
        ) as client:
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise DownloadError(f"Slack answered {response.status_code}")
                served = response.headers.get("content-type", "")
                if served.startswith("text/html") and not file.mimetype.startswith("text/html"):
                    # Without files:read Slack serves its sign-in page instead of the file.
                    raise DownloadError("Slack wouldn't let me download it (the app may be missing the files:read scope)")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise TooLarge
        return bytes(body)


@dataclass(frozen=True)
class Received:
    document: Document
    attachment: Attachment | None = None


@dataclass
class Shared:
    """What the files on one message became: documents for the turn, and anything the user must be told."""

    received: list[Received] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    @property
    def attachments(self) -> tuple[Attachment, ...]:
        return tuple(item.attachment for item in self.received if item.attachment is not None)

    def prompt(self, text: str) -> str:
        """The message as the model sees it this turn: full text up to INLINE_CHARS in total, else the summary."""
        if not self.received and not self.notices:
            return text
        blocks: list[str] = []
        budget = INLINE_CHARS
        for item in self.received:
            document = item.document
            opening = f'<file name="{escape(document.name)}" document_id="{document.id}">'
            if item.attachment is not None:
                body = "The image is attached to this message."
            elif document.text and len(document.text) <= budget:
                body = document.text
                budget -= len(document.text)
            else:
                body = (
                    f"Summary: {document.summary}\n"
                    f"The full text ({len(document.text):,} characters) is too long to include. "
                    "Call read_file with this document_id, and a query, for the parts you need."
                )
            blocks.append(f"{opening}\n{body}\n</file>")
        blocks.extend(f"[Already told the user: {notice}]" for notice in self.notices)
        return f"{text}\n\n[Files shared with this message]\n" + "\n".join(blocks)

    def logged(self, text: str) -> str:
        """The message as the conversation log keeps it: a pointer to each document, never its content."""
        notes = [f"[Shared file {item.document.name}, document_id {item.document.id}]" for item in self.received]
        return "\n".join([text, *notes]).strip()


class FileService:
    def __init__(
        self,
        documents: DocumentStore,
        memory: MemoryStore,
        model: Model,
        downloader: SlackDownloader | None,
        slack: Any | None,
    ) -> None:
        self.documents = documents
        self.memory = memory
        self.model = model
        self.downloader = downloader
        self.slack = slack
        self._reads = asyncio.Semaphore(CONCURRENT_READS)

    async def receive(self, owner: str, key: str, raw_files: list[dict[str, Any]], status: StatusFn) -> Shared:
        shared = Shared()
        for raw in raw_files:
            file = await self._resolved(SlackFile.parse(raw))
            try:
                received = await self._receive_one(owner, key, file, status)
            except TooLarge:
                shared.notices.append(size_limit_text(file.name))
                continue
            except (DownloadError, UnreadableError) as exc:
                shared.notices.append(f"I couldn't read *{file.name}*: {exc}.")
                continue
            except Exception:
                logger.exception("file read failed owner=%s file=%s", owner, file.id)
                shared.notices.append(f"I couldn't read *{file.name}* because of an error on my side.")
                continue
            if isinstance(received, str):
                shared.notices.append(received)
            else:
                shared.received.append(received)
        return shared

    async def _resolved(self, file: SlackFile) -> SlackFile:
        """Some file_share events carry only a stub; files.info has the download link."""
        if file.url or not file.id or self.slack is None:
            return file
        try:
            info = await self.slack.files_info(file=file.id)
        except Exception as exc:
            logger.info("files.info failed file=%s error=%s", file.id, type(exc).__name__)
            return file
        return SlackFile.parse(info.get("file") or {})

    async def _receive_one(self, owner: str, key: str, file: SlackFile, status: StatusFn) -> Received | str:
        existing = await self.documents.by_slack_file(owner, file.id)
        if existing is not None:
            return Received(existing)
        kind = kind_of(file.mimetype, file.name)
        if kind is None:
            return f"I can't read *{file.name}* yet ({file.mimetype or 'unknown type'})."
        if file.size is not None and file.size > MAX_BYTES:
            raise TooLarge
        if self.downloader is None:
            raise DownloadError("downloads aren't set up")
        await status(f"reading {file.name}")
        data = await self.downloader.download(file)
        mime = image_mime(file.mimetype, file.name) if kind == "image" else file.mimetype
        pieces = await asyncio.to_thread(extract, kind, data, mime)
        text = "\n\n".join(await asyncio.gather(*(self._read(piece, file.name) for piece in pieces))).strip()
        attachment = pieces[0] if kind == "image" and isinstance(pieces[0], Attachment) else None
        if not text:
            raise UnreadableError("I found no text in it")
        digest = await self.model.generate_structured(
            tier="light",
            system=DIGEST_PROMPT,
            text=f"File name: {file.name}\n\n{text[:DIGEST_INPUT_CHARS]}",
            schema=DocumentDigest,
            timeout_s=READ_TIMEOUT_S,
        )
        new = NewDocument(
            slack_file_id=file.id, name=file.name, mimetype=file.mimetype or mime, size_bytes=file.size or len(data),
            text=text[:MAX_TEXT_CHARS], summary=digest.summary.strip(), conversation_key=key,
        )
        document = await self.store(owner, new, digest.key_terms[:8])
        return Received(document, attachment)

    async def _read(self, piece: Piece, name: str) -> str:
        if isinstance(piece, str):
            return piece
        system = DESCRIBE_PROMPT if piece.mime_type.startswith("image/") else TRANSCRIBE_PROMPT
        async with self._reads:
            turn = await self.model.generate(
                tier="light", system=system, contents=[UserMessage(f"File: {name}", (piece,))], timeout_s=READ_TIMEOUT_S
            )
        return (turn.text or "").strip()

    async def store(self, owner: str, new: NewDocument, key_terms: list[str]) -> Document:
        """The documents row, its chunks, and a `document` memory record citing it, in one transaction."""
        pieces = chunks(new.text)
        embeddings = await asyncio.to_thread(embed_chunks, pieces)
        now = self.memory.clock()
        async with self.memory.repo.transaction():
            document_id = await self.documents.insert(owner, new, pieces, embeddings, now)
            event_id = await self.memory.add_event(
                owner, kind="document_added", summary=f"Document: {new.name}", occurred_at=now, score=1.0, now=now,
                sources=[("document", document_id)],
            )
            await self.memory.create_record(
                owner,
                record_id=await self.memory.free_id(owner, "document", new.name),
                type="document",
                title=new.name,
                body=f"{new.summary}\n\n[[doc:{document_id}]]",
                aliases=key_terms,
                source="document",
                now=now,
                sources=[("event", event_id), ("document", document_id)],
            )
        logger.info("document stored owner=%s id=%s chars=%d chunks=%d", owner, document_id, len(new.text), len(pieces))
        document = await self.documents.get(owner, document_id)
        assert document is not None
        return document

    async def create_document(
        self, owner: str, thread: SlackThread, title: str, content: str, format: DocumentFormat
    ) -> dict[str, Any]:
        """Upload to the requester's own DM. Not gated: it reaches nobody else (Spec 15 §3)."""
        if self.slack is None:
            return {"error": "Slack is not available in this context."}
        own_dm = thread.channel_id.startswith("D")
        channel = thread.channel_id if own_dm else await open_dm(self.slack, owner)
        filename = f"{slugify(title)}.{format}"
        upload: dict[str, Any] = {
            "channel": channel, "content": content, "filename": filename, "title": title,
            "initial_comment": f"Here's *{title}*.",
        }
        if own_dm and thread.thread_ts:
            upload["thread_ts"] = thread.thread_ts
        response = await self.slack.files_upload_v2(**upload)
        uploaded = _uploaded(response)
        lead = " ".join(content.split())
        document = await self.store(
            owner,
            NewDocument(
                slack_file_id=str(uploaded.get("id") or filename), name=filename, mimetype=FORMAT_MIMES[format],
                size_bytes=len(content.encode()), text=content[:MAX_TEXT_CHARS],
                summary=f"{title}. {lead[:400]}", conversation_key=f"dm:{channel}",
            ),
            [title],
        )
        return {
            "document_id": document.id,
            "name": filename,
            "permalink": uploaded.get("permalink"),
            "status": "Uploaded to the user's DM.",
        }


async def open_dm(slack: Any, user: str) -> str:
    response = await slack.conversations_open(users=user)
    return str(response["channel"]["id"])


def _uploaded(response: Any) -> dict[str, Any]:
    files = response.get("files") if response is not None else None
    if files:
        return dict(files[0])
    return dict((response or {}).get("file") or {})


def share_filename(document: Document) -> str:
    """Shared documents go out as their stored text: a PDF's text, not the PDF, since raw bytes are not kept."""
    if document.mimetype.startswith("text/") or kind_of(document.mimetype, document.name) == "text":
        return document.name
    return f"{PurePosixPath(document.name).stem}.txt"
