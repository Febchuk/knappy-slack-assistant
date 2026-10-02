# Specification 15: Files and Documents

## 1. Overview & Objectives

You cannot hand Knappy a PDF, and it cannot hand you anything longer than a chat message ([GAP-07](./00_gap_analysis.md)). This spec covers both directions:

- **In:** files shared with Knappy in a DM or a mention are read, understood, stored, and searchable later from any conversation.
- **Out:** long deliverables (a plan, a summary, a draft) arrive as a file in the user's DM.

---

## 2. Files In

### 2.1 Receiving

Slack delivers a shared file as a `message` event with `subtype: "file_share"` and a `files` array. Today two things drop it:
- `LocalStructuralFilter` rejects messages with any subtype.
- `on_message` (`knappy/slack/events.py`) passes it through, but nothing downstream reads `files`.

Change: `file_share` is allowed through. `InboundMessage` ([Spec 12](./12_agent_loop_v2.md)) gains `files: list[SlackFile]`.

### 2.2 Ingesting

For each file, before the agent loop runs:

1. **Download** `url_private_download` with the bot token (`Authorization: Bearer`). Cap at 20 MB. Larger files get a reply saying the size limit.
2. **Extract text** by type:

   | Type | Method |
   | :--- | :--- |
   | PDF | `pypdf` text. If a page has under 50 chars of text (scanned), send that page's image to Gemini for a transcription. |
   | DOCX | `python-docx` paragraphs and tables. |
   | TXT, MD, CSV, JSON, code | Decode as UTF-8. CSV is kept as text with the first 200 rows. |
   | PNG, JPG, GIF, WEBP | No text extraction. The image goes to Gemini as an inline part, which writes a description. |
   | Anything else | Stored as metadata only. The reply says the type isn't readable yet. |

3. **Store** in a `documents` table and as a `document` memory record ([Spec 13](./13_memory_system.md)):

```sql
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    slack_file_id TEXT NOT NULL,
    name TEXT NOT NULL,
    mimetype TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    text TEXT,                         -- extracted text, at most 500k chars
    summary TEXT,                      -- 3-6 sentence summary, light model
    conversation_key TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(owner_user_id, slack_file_id)
);

CREATE TABLE document_chunks (          -- for search inside long documents
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,                -- about 1,500 chars, 200-char overlap
    embedding BLOB,
    PRIMARY KEY (document_id, seq)
);
```

The `document` memory record has `title` = file name, `aliases` from the summary's key terms, `body` = summary plus `[[doc:{id}]]`. So "that contract I sent last week" finds it through `memory_search`.

Raw file bytes are not kept after extraction. Images keep only their description.

### 2.3 Using

The current message gets the document directly: for files ≤ 100k chars of text, the full text goes into the model's contents for that turn; for images, the image part. Larger files go in as the summary plus a note to use `read_file`.

| Tool | Args | Returns |
| :--- | :--- | :--- |
| `read_file` | `document_id: str`, `query: str | None`, `max_chars: int = 20000` | Full text up to `max_chars`, or the best-matching chunks when `query` is set |
| `list_files` | `query: str | None`, `limit: int = 10` | Recent documents with id, name, date, summary |

---

## 3. Files Out

| Tool | Args | Behavior |
| :--- | :--- | :--- |
| `create_document` | `title: str`, `content_markdown: str`, `format: Literal["md", "txt", "csv"] = "md"` | Uploads with `files_upload_v2` to the **requesting user's DM with Knappy**, with a one-line comment. Returns the file permalink. |

- This is not gated by HITL: it only reaches the user who asked. Sending a document to anyone else goes through `stage_outbound_action` ([Spec 05](./05_hitl_approval_gateways.md)) with a new `SHARE_FILE` action type.
- The agent uses it when the answer would be over ~3,000 characters or the user asks for a doc, plan, or file. It posts a short summary in the reply next to the file.
- Created documents are stored in `documents` too, so they are searchable later.

---

## 4. Manifest Changes (`slack/manifest.yml`)

Add bot scopes `files:read`, `files:write`, `reactions:write` (the last for [Spec 12](./12_agent_loop_v2.md) §6). Event subscriptions stay `message.im` and `app_mention`; `file_share` messages arrive through `message.im`. The app must be reinstalled after the scope change.

---

## 5. Scope

### In-Scope
- Reading shared files in DMs and mentions, extraction, storage, chunk search.
- `read_file`, `list_files`, `create_document` tools.
- `SHARE_FILE` HITL action: added to the `action_drafts.action_type` CHECK constraint in both schemas (`knappy/db/schema.py`), plus an executor branch (`knappy/slack/executor.py`) that shares the stored file to the recipient's DM.
- `pypdf` and `python-docx` added to `pyproject.toml`.

### Out-of-Scope
- Audio and video files.
- Slack canvases (possible later; uploaded files work on every plan).
- Google Drive and Notion (wave 2).
- Editing a file the user shared and returning a modified copy of the original format (for example, editing a DOCX in place).

---

## 6. Verification

| Test Case ID | Procedure | Expected Outcome |
| :--- | :--- | :--- |
| **TEST-FILE-01** | DM a 3-page PDF with "summarize this" (fake Slack download, fake model). | A `documents` row and a `document` memory record exist. The model's contents include the PDF text. |
| **TEST-FILE-02** | Next day, a new thread: "what did that PDF say about pricing?" | `memory_search` or `list_files` finds the document. `read_file` with `query="pricing"` returns the matching chunk. |
| **TEST-FILE-03** | Share a 30 MB file. | No download beyond the cap. The reply states the size limit. |
| **TEST-FILE-04** | Share a PNG screenshot. | The model receives an image part. The stored record has a description and no image bytes. |
| **TEST-FILE-05** | Ask for "a one-page project plan". The model calls `create_document`. | One `files_upload_v2` to the user's DM. No HITL card. |
| **TEST-FILE-06** | Ask to "send that plan to Alex". | A `SHARE_FILE` approval card. Nothing reaches Alex before approval. |
| **TEST-FILE-07** | User B asks for user A's document by id. | `read_file` returns not found. |
