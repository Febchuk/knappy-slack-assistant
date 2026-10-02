# Specification 00: Gap Analysis — From Reminder Bot to Personal Assistant

## 1. Why This Exists

Specs 01–10 were written from `docs/Mechanics of Play, MUSE, and Instruct.pdf`. That document's blueprint is **Pally**: a relationship CRM with a commitment tracker, approval cards, and morning nudges. The code implements that blueprint faithfully.

The product goal is larger. Knappy is a **personal assistant that lives in Slack**, in the class of Instinct and Meta Muse: you can talk to it about anything, it remembers you across conversations, and it does work for you. Reminders and relationship tracking are the first slice, not the product.

This document records where the product stands today, why it does not work end to end, and which spec closes each gap. Every gap below maps to a spec section and to at least one acceptance journey in [Spec 17](./17_end_to_end_acceptance.md).

---

## 2. Reference Products

| Product | What it is | What Knappy takes from it |
| :--- | :--- | :--- |
| **Pally** (YC S25) | Texting assistant built on a relationship graph, with OAuth connectors and approval before sends. | Relationship graph, commitments, HITL approval, proactive briefs. Already built. |
| **Meta Muse** | Outcome-driven agent: browses, connects to apps, creates documents, keeps working after you leave. Remembers context, preferences, people, boundaries, and feedback. Runs in an isolated VM with a Sentinel monitor. | Talk-to-it-about-anything scope, durable personal memory, deliverables. Sandboxed browsing is wave 3. |
| **Instinct** | High-autonomy agent with its own phone and computer. Memory is typed, aliased markdown records reconciled by a background process, with a profile one-pager in every prompt. | The memory architecture ([Spec 13](./13_memory_system.md)). Full credential delegation stays out of scope. |

Memory design sources: [Supermemory — Reverse engineering Instinct's memory](https://supermemory.ai/blog/reverse-engineering-instinct-memory/), [agentnativedev — Memory system behind a $10B personal agent](https://agentnativedev.medium.com/memory-system-behind-10b-invite-only-personal-agent-d809b8366e9d).

---

## 3. Gap Table (verified against code on `feat/knappy-implementation`)

| ID | Area | Today | Effect | Closed by |
| :--- | :--- | :--- | :--- | :--- |
| **GAP-01** | Brain | No LLM is called anywhere. `knappy/main.py` builds `KnappyRuntime` with its defaults: `heuristic_intent`, `heuristic_complete`, `heuristic_triage` (`knappy/runtime.py`). `SlmExtractor()` has no `complete`, so it uses `heuristic_extract` (`knappy/ingestion/extract.py`). `JevSystemOneAdapter` has no client, so the regex fallback always wins. `.env` has no model key. | Every reply is a regex template. | [Spec 11](./11_model_layer_gemini.md) |
| **GAP-02** | Refusals | `knappy/agent/router.py` replies "I can't answer general questions…" to anything it does not pattern-match. | The assistant refuses most of what a person would ask. | [Spec 12](./12_agent_loop_v2.md) |
| **GAP-03** | Reasoning | `ReActAgent.MAX_ITERATIONS = 3`. The loop returns immediately after `search_slack_history`, an empty `search_commitments`, or staging. The system prompt is one line: "You are Knappy, a relationship assistant." | No multi-step work. Tool results are formatted by templates instead of synthesized. | [Spec 12](./12_agent_loop_v2.md) |
| **GAP-04** | Short-term memory | `ThreadMemory` (`knappy/agent/memory.py`) is an in-process dict of 10 messages per thread. It is lost on restart. | The bot forgets the conversation when the process restarts, and cannot see other threads. | [Spec 13](./13_memory_system.md) |
| **GAP-05** | Long-term memory | Durable state is only `contacts`, `interactions`, `commitments`, `action_drafts`, and `briefing_items` (`knappy/db/schema.py`). No preferences, facts, decisions, projects, or profile. | It cannot learn who you are or how you like things done. | [Spec 13](./13_memory_system.md) |
| **GAP-05b** | Semantic recall | `generate_embedding` (`knappy/ingestion/embed.py`) returns a SHA-256-seeded random vector unless `KNAPPY_EMBEDDER=fastembed` is set and `fastembed` is installed. Neither is the default, and `fastembed` is not in `pyproject.toml`. | Vector "similarity" over interactions is noise in a default install. | [Spec 13](./13_memory_system.md) §5 |
| **GAP-06** | Reach | Five tools in `knappy/agent/tools.py`: four relationship lookups plus `stage_outbound_action`. | It cannot research, read a link, or answer a factual question. | [Spec 14](./14_web_research.md) |
| **GAP-07** | Files and deliverables | No file events or scopes in `slack/manifest.yml`. Output is plain message text only. | It cannot read a PDF you send or hand you a document. | [Spec 15](./15_files_and_documents.md) |
| **GAP-08** | Proactive delivery | Sweeps route by `owner_user_id` (`knappy/heartbeat/engine.py`), but the fallback is the placeholder `user_id="user"` (`knappy/runtime.py`; `bind_user` is never called). Proactive follow-up drafts set `recipient_identifier` to the contact's *name* when no `slack_user_id` is known. The draft's `staged_content` is the reminder written *for the user*, so approving sends Alex "You promised Alex…". The 8:00 brief uses server time (UTC in Docker). Briefs are fixed templates that ignore everything else Knappy knows. | Approving a proactive follow-up sends the wrong text, or fails with no reachable recipient. Briefs arrive at the wrong hour and read like a cron job. | [Spec 16](./16_proactive_v2.md) |
| **GAP-09** | Specs | Spec 01 frames Knappy as a relationship assistant. Spec 04 lists web search as out of scope. Specs 07 and 10 assert the regex behavior. | The written contract forbids the intended product. | [Spec 01](./01_system_architecture_and_scope.md) amendment, [Spec 12](./12_agent_loop_v2.md), [Spec 17](./17_end_to_end_acceptance.md) |
| **GAP-10** | Proof | Tests pass against fakes and heuristics. No test proves a person can hold a real conversation across restarts. | "Green" does not mean "works". | [Spec 17](./17_end_to_end_acceptance.md) |

---

## 4. What Carries Over

These parts are sound and stay:

- Slack ingress, dedupe, and egress rules: `knappy/slack/app.py`, `events.py`, `egress.py` ([Spec 08](./08_slack_egress.md)).
- HITL approval gateway, Block Kit cards, and compare-and-swap idempotency: `knappy/hitl/*` ([Spec 05](./05_hitl_approval_gateways.md)).
- Repository, SQLite and Postgres factory: `knappy/db/*`.
- Per-user isolation through `owner_user_id` and `current_owner` (`knappy/agent/tools.py`).
- The heartbeat scheduler shell and zero-LLM SQL sweeps ([Spec 06](./06_proactive_heartbeat_engine.md)).
- The seeded end-to-end harness (`tests/test_10_seeded_e2e.py`).

---

## 5. Capability Waves

| Wave | Contents | Specs |
| :--- | :--- | :--- |
| **1 — Talk, remember, research, read** | Gemini brain, agent loop, Instinct-style memory, web search and fetch, files in and out, proactive fixes, acceptance journeys. | 11–17 |
| **2 — Act in your accounts** | Google Workspace: Gmail read, draft, send; Calendar read and schedule. All writes through HITL. | Not yet written |
| **3 — Act anywhere** | Sandboxed browser for sites without an API, with a monitor that screens planned actions (Muse's Sentinel). | Not yet written |

---

## 6. Build Order

`11 → 12 → 13 → 17 (offline journeys) → 14 → 15 → 16`

Each step ends in something you can run in Slack. After Spec 12 you can hold a conversation. After Spec 13 it remembers you across restarts. After Spec 17's offline journeys, that is proven in CI.
