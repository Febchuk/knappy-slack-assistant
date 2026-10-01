# Specification 07: Master Verification Plan and Test Matrix

## 1. Overview & Objectives

In **Spec-Driven Development (SDD)**, code is not considered complete until every functional requirement satisfies a deterministic verification procedure. 

This document defines the **Master Verification Plan**, mock test harnesses, automated testing matrix, and step-by-step end-to-end validation checklist for Knappy Slack Assistant.

---

## 2. Test Architecture & Directory Layout

The test suite is structured to mirror the specifications:

```text
tests/
├── conftest.py                     # Shared fixtures (in-memory DB, mock Slack Bolt client, mock TypeSafe client, embeddings)
├── test_02_infra_and_storage.py    # Schema migrations, CRUD operations, vector search
├── test_03_ingestion_pipeline.py   # Tier 0 filter, TypeSafe SystemOneGate, fallback circuit breaker, CPU embeddings
├── test_04_react_agent.py          # TypeSafe fast-path router, ReAct loop, tool dispatch, thread boundary
├── test_05_hitl_gateways.py        # Action staging, authorization validation, CAS idempotency
├── test_06_proactive_engine.py     # Deterministic sweeper, TypeSafe alert triage, proactive DM dispatch
└── test_e2e_workflow.py            # End-to-end integration flow from note ingest to action approval
```

---

## 3. Comprehensive Test Matrix

| ID | Test Target | Spec Ref | Test Type | Acceptance Threshold |
| :--- | :--- | :--- | :--- | :--- |
| **TEST-INFRA-01** | Slack App Credential Load | Spec 02 | Unit | Environment tokens loaded without error |
| **TEST-INFRA-02** | Slack Event Acknowledgment | Spec 02 | Integration | Event ack returned in $< 500\text{ ms}$ |
| **TEST-INFRA-03** | Dual-Memory Schema Init | Spec 02 | Integration | Tables created in SQLite and PostgreSQL |
| **TEST-INFRA-04** | Vector Cosine Retrieval | Spec 02 | Integration | Ingested vector retrieved with similarity $> 0.95$ |
| **TEST-INFRA-05** | Cascading Relationship Clean | Spec 02 | Unit | Deleting contact deletes all linked interactions |
| **TEST-INGEST-01** | Tier 0 Gate: Bot Message Filter | Spec 03 | Unit | Discards bot/webhook events in $< 2\text{ ms}$ |
| **TEST-INGEST-02** | Tier 0 Gate: Short Ping Filter | Spec 03 | Unit | Discards messages $< 4$ words |
| **TEST-INGEST-03** | Tier 1 Jev Gate: Note Acceptance | Spec 03 | Integration | High confidence ($\ge 0.90$) routing for explicit meeting notes |
| **TEST-INGEST-04** | Tier 1 Jev Gate: Non-Regex Commitment | Spec 03 | Integration | Accurately catches subtly phrased commitment without regex triggers |
| **TEST-INGEST-05** | Tier 1 Circuit Breaker Fallback | Spec 03 | Unit | Seamless fallback to `RegexFallbackAdapter` on Jev timeout/error |
| **TEST-INGEST-06** | SLM Pydantic Schema Parsing | Spec 03 | Integration | Extracts valid `ExtractedInteraction` schema |
| **TEST-INGEST-07** | Local CPU Embedding Gen | Spec 03 | Unit | Produces 384-dim vector in $< 20\text{ ms}$ on CPU |
| **TEST-INGEST-08** | Atomic Upsert Transaction | Spec 03 | Integration | Single contact record upserted, interaction added |
| **TEST-REACT-01** | Fast-Path Intent Routing | Spec 04 | Integration | High-confidence ($\ge 0.90$) query bypasses ReAct loop in $< 400\text{ ms}$ |
| **TEST-REACT-02** | Multi-Hop ReAct Escalation | Spec 04 | Integration | Complex/ambiguous query correctly escalates to multi-step ReAct loop |
| **TEST-REACT-03** | Observation Synthesis | Spec 04 | Integration | Tool outputs synthesized into grounded answer |
| **TEST-REACT-04** | Missing Data Honesty | Spec 04 | Integration | Clear negative answer when tool returns empty |
| **TEST-REACT-05** | Safety Action Staging | Spec 04 | Unit | Outbound commands routed to staging tool |
| **TEST-REACT-06** | Thread Memory Boundary | Spec 04 | Unit | Zero context leakage across distinct `thread_ts` |
| **TEST-REACT-07** | Max Loop Termination | Spec 04 | Unit | Agent stops at iteration 3 and asks user for clarification |
| **TEST-HITL-01** | Draft Staging Creation | Spec 05 | Integration | Row inserted into `action_drafts` (PENDING) |
| **TEST-HITL-02** | Authorized Approval Flow | Spec 05 | Integration | Authorizer click sets status to APPROVED |
| **TEST-HITL-03** | Unauthorized User Rejection | Spec 05 | Unit | Click from non-creator blocked with warning |
| **TEST-HITL-04** | Double-Click Idempotency | Spec 05 | Unit | Atomic CAS ensures exactly one execution |
| **TEST-HITL-05** | Cancellation Workflow | Spec 05 | Unit | Click on Cancel updates block and drops draft |
| **TEST-PROACT-01** | Zero-Trigger Zero-Cost | Spec 06 | Unit | Clean DB scan terminates in $< 5\text{ ms}$, 0 LLM calls |
| **TEST-PROACT-02** | High-Priority Alert Trigger | Spec 06 | Integration | Jev returns `IMMEDIATE_DM` ($\ge 0.75$), bot posts proactive DM |
| **TEST-PROACT-03** | Alert Noise Suppression | Spec 06 | Integration | Low-urgency reminder batched or suppressed by Jev triage |
| **TEST-PROACT-04** | Proactive Snooze (24h) | Spec 06 | Unit | Increases `due_date` by 24h, disables prompt |
| **TEST-PROACT-05** | Mark Commitment Done | Spec 06 | Unit | Updates status to FULFILLED |
| **TEST-PROACT-06** | Proactive-to-ReAct Handoff | Spec 06 | Integration | Thread reply converts outbound ping to conversation |

---

## 4. End-to-End Live Validation Checklist

Once automated tests pass, the live Slack workspace validation is performed using the 5-step checklist:

### Step 1: Socket Mode Connection
```bash
python -m knappy.main
```
- **Validation:** Terminal displays `⚡️ Knappy is connected via Socket Mode!`.

### Step 2: Note Ingestion Test
- Open Slack and send DM to bot:
  ```text
  note: Met with Alex from Acme Corp, promised to send the revised budget by Thursday.
  ```
- **Validation:** 
  1. Bot immediately replies with acknowledgment:
     > Logged interaction with **Alex**.\
     > **Commitment:** Send the revised budget by Thursday
  2. Inspect database:
     ```sql
     SELECT * FROM contacts WHERE name = 'Alex';
     SELECT commitment, due_date FROM interactions WHERE contact_name = 'Alex';
     ```
     Records exist with non-null embedding.

### Step 3: Conversational Recall Query
- In the same Slack DM:
  ```text
  What did I promise to send Alex?
  ```
- **Validation:** Bot invokes `search_commitments` and replies:
  > You promised to send Alex the revised budget by Thursday.

### Step 4: HITL Action Gateway & Idempotency
- Send the bot:
  ```text
  Follow up with Alex
  ```
- **Validation:**
  1. Bot renders an interactive Block Kit card with `[Approve & Send]` and `[Cancel]`.
  2. Clicking `[Approve & Send]` immediately replaces the buttons with a green checkmark receipt:
     > :white_check_mark: *Executed: Follow-up dispatched to Alex.*
  3. Clicking again has no effect (idempotency verified).

### Step 5: Proactive Sweeper & Morning Briefing
- Trigger a manual heartbeat sweep:
  ```bash
  python -m knappy.scheduler --run-now
  ```
- **Validation:** Bot proactively initiates a new DM thread alerting the user to upcoming deadlines with 1-click action buttons.

---

## 5. Execution Command

To execute the entire automated test suite:
```bash
pytest -v tests/ --cov=knappy --cov-report=term-missing
```
Minimum required test coverage for merge: **85%**.
