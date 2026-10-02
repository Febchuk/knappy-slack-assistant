# Specification 01: System Architecture and Master Scope

## 0. Product Vision (amended — supersedes the framing in §1 and §3)

**Knappy is a personal assistant that lives in Slack.** You can talk to it about anything. It remembers you — your preferences, people, projects, and past conversations — across threads and restarts. It does work for you: researches, reads what you send it, drafts, tracks what you owe people, and reaches out before things slip. Reference products are Instinct and Meta Muse; Pally is the first slice, not the ceiling. See [Spec 00](./00_gap_analysis.md) for the gap between this vision and the code.

### 0.1 Operating Principles

1. **Answer, don't refuse.** Any reasonable request gets a real attempt. "I can't answer general questions" is a defect.
2. **Remember by default.** Every conversation is persisted. Durable facts are reconciled into memory in the background ([Spec 13](./13_memory_system.md)).
3. **Reads are free, boundary-crossing writes are gated.** Searching memory, the web, files, and Slack history runs without asking. Anything that reaches another person or an external account goes through the HITL gate ([Spec 05](./05_hitl_approval_gateways.md)). Writing into the user's own DM with Knappy is not boundary-crossing.
4. **One agent, real brain, cheap edges.** A single Gemini Flash tool-calling loop is the center ([Spec 11](./11_model_layer_gemini.md), [Spec 12](./12_agent_loop_v2.md)). Deterministic code handles what code can: SQL sweeps, dedupe, approvals, and explicit commands. No swarms.

### 0.2 Capability Waves

| Wave | Contents | Status |
| :--- | :--- | :--- |
| 1 | Brain, agent loop, memory, web search and fetch, files in and out, proactive fixes | Specs 11–17 |
| 2 | Google Workspace: Gmail and Calendar, writes through HITL | To be specified |
| 3 | Sandboxed browser with an action monitor | To be specified |

### 0.3 Revised Targets

| Metric | Target |
| :--- | :--- |
| Visible acknowledgement (placeholder or answer) | < 1.5 s |
| Simple answer, no tools | < 4 s |
| Tool-using answer (memory, web) | < 15 s, with the placeholder updated while working |
| Monthly model cost, one active user | < $10 |
| Unapproved boundary-crossing actions | 0 |

Where §1–§5 below conflict with §0, §0 wins. In particular, §1's "relationship intelligence agent" framing, §3.1 item 4's fixed tool list, and §4's latency and cost targets are superseded.

---

## 1. Executive Summary & Objective

**Knappy Slack Assistant** is an ambient executive assistant and relationship intelligence agent operating natively within Slack. Modeled after Pally (YC S25) and architected according to Google Cloud's Agentic AI Design Patterns, Knappy provides:
1. **Passive Relationship Intelligence:** Ingests conversations and notes, extracting contacts, commitments, and interaction summaries.
2. **Context-Aware Conversational Assistant:** Answers multi-hop questions about people, past agreements, and schedules using semantic recall.
3. **Safety-Guarded Autonomous Action (HITL):** Drafts outbound actions (emails, DMs, calendar invites) but executes strictly upon explicit Slack Block Kit user approval.
4. **Proactive Heartbeat Monitoring:** Evaluates deadlines and dormant relationships via zero-LLM deterministic sweeps, dispatching actionable morning briefings and alerts.

---

## 2. Agentic Pattern Architecture

Knappy deliberately avoids token-heavy, unpredictable Multi-Agent Swarms or Coordinator patterns. It employs a **hybrid dual-path architecture**:

```mermaid
flowchart TD
    subgraph Client ["Slack Workspace Interface"]
        SlackUser["Slack User (DMs / @mentions)"]
        SocketStream["Slack Socket Mode / Events API"]
        BlockKitUI["Block Kit Cards & Modals"]
    end

    subgraph Ingress ["Ingress & Session Router"]
        Router["Event Router & Deduplicator (Bolt)"]
    end

    subgraph PathA ["Path A: Sequential Pattern (ETL)"]
        NoiseFilter["Two-Tier Ingestion Gate (Local + SystemOneGate Jev)"]
        SLMExtractor["SLM Entity & Commitment Extractor (Pydantic)"]
        LocalEmbedder["CPU Embedding Engine (bge-small-en-v1.5)"]
    end

    subgraph PathB ["Path B: Hybrid Routing & ReAct (Conversational Agent)"]
        FastRouter["Fast Intent Router (Jev System 1, ~80ms)"]
        DirectTool["Fast-Path Direct Tool Dispatch (< 200ms)"]
        ReActLoop["ReAct Reasoning Loop (Complex / Multi-Hop)"]
        ToolRouter["Tool Call Registry"]
    end

    subgraph PathC ["Path C: HITL Gate (Safety Boundary)"]
        DraftStage["Action Draft Stager (PENDING)"]
        HITLApproval["Interactive Block Kit Approval Button"]
        ExecWorker["Idempotent Execution Worker"]
    end

    subgraph PathD ["Path D: Proactive Monitor Pattern"]
        Scheduler["Deterministic Cron Sweeper (Zero LLM)"]
        AlertTriage["Jev Alert Triage Gate (~80ms)"]
        ProactiveSynthesizer["Proactive Briefing Synthesizer"]
    end

    subgraph Storage ["Dual-Memory Persistence"]
        RelationalDB[("Relational DB: contacts, interactions, action_drafts, briefing_items")]
        VectorStore[("Vector Store: sqlite-vec / pgvector")]
    end

    SlackUser -->|Message/Mention| SocketStream
    SocketStream --> Router
    
    Router -->|Background Message Stream| NoiseFilter
    NoiseFilter -->|Filtered Message| SLMExtractor
    SLMExtractor -->|Structured JSON| LocalEmbedder
    LocalEmbedder --> Storage

    Router -->|User Query in DM/@bot| FastRouter
    FastRouter -->|High Confidence Direct Tool| DirectTool
    DirectTool --> Storage
    FastRouter -->|Complex Multi-Hop| ReActLoop
    ReActLoop <--> ToolRouter
    ToolRouter <--> Storage
    ToolRouter -->|Mutating Action Requested| DraftStage
    DraftStage --> BlockKitUI

    BlockKitUI -->|User clicks [Approve]| Router
    Router --> HITLApproval
    HITLApproval --> ExecWorker
    ExecWorker -->|State Updated| Storage
    ExecWorker -->|Immutable Receipt| BlockKitUI

    Scheduler -->|Every 30m / 8am Scan| Storage
    Storage -->|Candidate Matches| AlertTriage
    AlertTriage -->|DISPATCH_IMMEDIATE_DM| ProactiveSynthesizer
    ProactiveSynthesizer --> BlockKitUI
```

---

## 3. Scope Boundaries: In-Scope vs. Out-of-Scope

Clear boundaries ensure predictable execution and prevent cost overruns or permission failures:

### 3.1 In-Scope (Deliverables)

1. **Transport Layer**:
   - Python `slack-bolt` running over **Socket Mode** (development and single-tenant production without public webhooks/ngrok).
   - Event handling for `message.im` (1-on-1 private DMs) and `app_mention` (in-channel invocations).
   - Sub-3-second acknowledgment (`200 OK` or immediate Slack ack) with asynchronous event processing.
2. **Dual-Memory Layer**:
   - Relational tables: `workspaces`, `contacts`, `interactions`, `action_drafts`, and `briefing_items`.
   - Vector store: 384-dimensional dense vectors using local CPU embeddings (`bge-small-en-v1.5` or `all-MiniLM-L6-v2`) via `fastembed` or `sqlite-vec`/`pgvector`.
   - Local SQLite support for zero-config local development, with clean migration path to PostgreSQL + `pgvector`.
3. **Passive Ingestion Pipeline**:
   - Two-tier gate: Local structural filter (drops bots, subtypes, short tokens) + `SystemOneGate` (TypeSafe AI Jev with regex fallback) to reliably catch nuanced commitments and meeting notes without expensive autoregressive generation.
   - Structured JSON entity extraction using a lightweight model (`gpt-4o-mini` or equivalent SLM) enforced via Pydantic schemas.
   - Extraction targets: contact names, interaction summaries, explicit commitments, and due dates.
4. **Conversational ReAct Agent**:
   - Multi-turn conversational loop bounded strictly by Slack `thread_ts`.
   - Tools: `query_relationship_graph`, `search_commitments`, `get_meeting_context`, `stage_outbound_action`.
5. **Human-in-the-Loop (HITL) Gate**:
   - Zero direct execution for state-altering actions (e.g., sending an email, scheduling an event, or posting a channel broadcast).
   - Interactive Slack Block Kit draft cards with `[Approve & Send]` and `[Cancel]` buttons.
   - User authorization check: Only the initiating user can approve staged actions.
   - Idempotent execution receipt replacing interactive buttons in-place.
6. **Proactive Heartbeat Engine**:
   - Deterministic SQL sweepers for upcoming commitments (due within 12 hours) and relationship cadences (> 30 days without contact).
   - Zero-LLM execution cost when no triggers match.
   - Proactive Slack DM delivery with inline 1-click action buttons.

### 3.2 Out-of-Scope (Explicit Non-Goals)

1. **Global Workspace Eavesdropping**:
   - Knappy will **not** request global `channels:read` or `channels:history` on all public/private channels. In-channel activity is strictly restricted to channels where Knappy is explicitly invited or mentioned (`app_mention`).
2. **Multi-Agent Swarm / Hierarchical Orchestration**:
   - No multi-agent debate, swarm consensus, or LangChain multi-agent managers. These add prohibitive latency (>10s) and token costs without user benefit for personal assistance.
3. **Full Direct Credential Delegation**:
   - No raw credential or password scraping (unlike Instinct). All external services connect via OAuth tokens or scoped API tokens.
4. **Autonomous State Mutation**:
   - Knappy must **never** execute an outbound communication or external calendar mutation without a human clicking `[Approve]`.
5. **Commercial Billing & Multi-Tenant SaaS Subscriptions**:
   - Billing engines (Stripe webhooks, subscription tiers) are deferred to post-v1. The focus is single/multi-workspace core functionality.
6. **Raw Audio / Video Processing**:
   - Real-time telephony streams or Zoom/Google Meet bots are out-of-scope; only text transcripts or meeting summaries pasted/synced into Slack are processed.

---

## 4. Key Performance & Cost Targets

| Metric | Target | Rationale |
| :--- | :--- | :--- |
| **Slack Ack Latency** | $< 500\text{ ms}$ | Slack enforces a strict 3-second timeout before retrying webhooks. |
| **Simple Query Latency** | $< 2.0\text{ s}$ | Fast interactive feel in Slack DMs. |
| **ReAct Tool Loop Latency** | $< 4.5\text{ s}$ | Multi-hop DB retrieval and response synthesis. |
| **Deterministic Ingestion Pre-Filter** | $< 5\text{ ms}$ | Zero LLM overhead for 90%+ of background chatter. |
| **Monthly Operating Cost (Single User)** | $\$1.50 - \$5.00$ | Free local embeddings + SLM extraction + selective caching. |
| **Action Safety Integrity** | $100\%$ | 0 unauthorized or unconfirmed state-mutating actions executed. |

---

## 5. Verification Plan & Acceptance Gates

Before any milestone is signed off as complete, the following gates must be validated:

- [ ] **Gate 1 (Transport):** Socket Mode connects reliably; receives DM and mention events; acks within 500ms.
- [ ] **Gate 2 (Ingestion):** Noise filter rejects bots and short pings; SLM extracts valid JSON matching Pydantic schema; embeddings generated on CPU; stored in DB.
- [ ] **Gate 3 (Conversational ReAct):** Querying "Who promised to send the revised budget?" accurately retrieves the interaction via vector/relational search and answers in-thread.
- [ ] **Gate 4 (HITL Safety):** Asking "Follow up with Alex" stages a draft in `action_drafts` and emits a Block Kit card. Clicking `[Approve]` updates the block with a green checkmark and dispatches the action. Unapproved actions never execute.
- [ ] **Gate 5 (Proactive Sweeper):** Mocking a commitment due in 2 hours triggers the deterministic scanner, passes the Jev alert triage gate, and only then invokes the SLM synthesizer and posts a proactive DM to the user with action buttons.
