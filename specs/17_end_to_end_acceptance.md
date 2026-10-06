# Specification 17: End-to-End Acceptance

## 1. Overview & Objectives

Today's tests pass against regex heuristics and fake Slack clients. Even `tests/test_08_slack_live.py` uses `FakeSlack`. A green suite therefore says nothing about whether a person can talk to Knappy ([GAP-10](./00_gap_analysis.md)).

This spec defines **journeys**: multi-turn, multi-day scenarios that *are* the definition of "works end to end". Each journey runs at three levels:

| Level | Model | Slack | DB | Runs |
| :--- | :--- | :--- | :--- | :--- |
| **Offline** | `FakeModel` scripted per journey ([Spec 11](./11_model_layer_gemini.md)) | `FakeSlack` recording every call | Temp SQLite file | Every `pytest` run |
| **Live model** (`-m live_model`) | Real Gemini | `FakeSlack` | Temp SQLite file | Before merging any change to prompts, tools, or memory. Needs `GEMINI_API_KEY`. |
| **Live Slack** (manual script) | Real Gemini | Real workspace, test user | Temp Postgres or SQLite | Before a release. `scripts/smoke.md` checklist. |

**Amends:** [Spec 07](./07_verification_plan_and_test_matrix.md) (master test matrix) and [Spec 10](./10_seeded_e2e_conversations.md). Spec 10 cases that assert regex wording ("I can't answer general questions", the fixed identity text) are deleted. Its seeded-DB and path-logging approach is kept and extended.

---

## 2. Harness

`tests/journeys/` with a shared harness:

```python
class Journey:
    def __init__(self, tmp_path, *, model: Model, clock: FakeClock): ...
    async def dm(self, user: str, text: str, *, thread: str | None = None,
                 files: list[FakeFile] = ()) -> SlackCapture: ...
    async def click(self, user: str, action_id: str, value: str) -> SlackCapture: ...
    async def restart(self) -> None          # closes runtime and repo, reopens from the same DB file
    async def advance(self, **delta) -> None # moves FakeClock, runs due heartbeat and reconciler work
```

- `restart()` is mandatory in journeys that test memory. It builds a fresh runtime from the same file, exactly as `python -m knappy.main` would.
- `advance()` runs the scheduler and reconciler paths that real time would trigger, so "next day" is testable.
- Offline assertions are on **behavior**: which tools ran with which arguments, what reached Slack, what is in the DB. They never assert exact model wording.
- Live-model assertions use checks a person would make, written as code: the reply mentions X, cites a URL, does not claim to have sent anything. Where a check needs judgment, a `light`-tier judge call returns pass/fail with a reason.

---

## 3. Journeys

| ID | Journey | Proves | Gaps |
| :--- | :--- | :--- | :--- |
| **J-01 Talk** | DM "hey" → DM "what's a good way to structure a 1:1 agenda?" | A real answer, no refusal, the placeholder replaced by the answer. | 01, 02 |
| **J-02 Remember** | "Remember I'm vegetarian and I hate early meetings." → `restart()` → `advance(days=1)` → new thread: "pick a lunch spot near Union Square and suggest a time to meet Sam". | Survives restart. The preference is used without being restated. The time avoids early morning. | 04, 05 |
| **J-03 Learn passively** | Mention in passing "I just started at Stripe, my manager is Priya" → `advance(minutes=30)` (reconciler runs) → `restart()` → "who's my manager?" | The reconciler created records with no explicit "remember". The answer is Priya. | 05 |
| **J-04 Correct and forget** | "I work at Google" → reconcile → "actually I moved to Stripe" → reconcile → "where do I work?" → "forget where I work" → "where do I work?" | Supersede, then forget. The last answer says it doesn't know. | 05 |
| **J-05 Research** | "what's the latest Python release and what changed?" | `web_search` ran. The answer cites a source. | 06 |
| **J-06 Read a link** | Paste a URL with "tl;dr this" | `fetch_url` ran on that URL. Answer summarizes the page. | 06 |
| **J-07 Files in** | DM a PDF with "summarize this" → `advance(days=1)` → new thread: "what did that PDF say about pricing?" | Stored, summarized, found later from a different conversation. | 07 |
| **J-08 Files out** | "Write me a one-page launch plan for the Q3 offsite." | `create_document` uploads to the user's DM. No HITL card. | 07 |
| **J-09 Commitments by talking** | "I told Alex I'd send the budget by Thursday" → "what do I owe people?" → "I sent Alex the budget" → "what do I owe people?" | `add_commitment` then `complete_commitment` without `note:`. The last list excludes it. | 03 |
| **J-10 Act with approval** | "Follow up with Alex about the budget" → card → `click(approve)` → `click(approve)` again | Exactly one DM to Alex, with the drafted text. Nothing before approval. | 03 |
| **J-11 Proactive** | Seed a commitment due in 2 hours for owner tz `America/New_York` → `advance` to 08:00 New York time | One morning brief to the owner. The follow-up draft text is addressed to Alex, not the reminder. A reply in the brief's thread continues with context. | 08 |
| **J-12 Isolation** | U1 and U2 each "remember my manager is …" with different names; each asks "who's my manager?" | Each gets their own answer. No cross-reads in the DB queries. | 05 |
| **J-13 Failure is visible** | Model raises on every call | The placeholder becomes the error text with a reference id. No silence. | 10 |
| **J-14 Scales with history** | Seed 60 simulated days: about 500 turns across 30 conversations, including 15 facts that matter and lots of small talk. Reconcile day by day. Then ask about 5 of the facts. | Prompt size for a new message stays under 6k tokens of memory context (profile, open loops, recap) regardless of history length. All 5 answers are correct. Small talk created no records. | 04, 05 |
| **J-15 Reversal without bleed** | "I love steak" → reconcile → "plan my dinners this week" (creates a derived workstream) → reconcile → "actually I'm vegetarian now" → reconcile → `restart()` → "suggest a dinner". Then "forget that I used to eat meat" → "what do you know about my diet?" | The suggestion is vegetarian. After the forget, neither the answer, the profile, nor any active record mentions steak, including the derived workstream. | 05 |
| **J-16 Deliberate silence** | `advance` through 7 simulated days in which the only events are low-value: chit-chat, a fulfilled commitment, a contact touched yesterday. | Zero unprompted Slack posts all week. Every tick is logged `silent`. | 08 |
| **J-17 Follow-through** | "I asked Alex for the contract. If he hasn't sent it by Thursday, help me chase him." → `advance` past Thursday with nothing from Alex → tick. | One DM surfaces it with a staged chase to Alex behind approval. Nothing is sent to Alex before approval. | 03, 08 |

J-14, J-15, and J-16 test the three predictions in [The Instinct Thesis](https://x.com/ashwingop/article/2093026452929405356): degrade gracefully as history grows, handle reversals without old preferences bleeding through, and sometimes deliberately do nothing.

GAP-05b (real embeddings) has no journey of its own. It is covered by TEST-MEM-10 ([Spec 13](./13_memory_system.md)) at live-model level, because the offline harness uses the hash embedder. GAP-09 (specs contradict the vision) is closed by this spec set itself: the Spec 01 §0 amendment and the banners on Specs 04, 06, 07, and 10.

---

## 4. Definition of Done for Wave 1

- J-01 to J-17 pass **offline** in CI.
- J-01, J-02, J-03, J-04, J-05, J-07, J-10, J-15 pass at **live model** level.
- **Memory journeys carry the most weight.** The agent loop is a commodity; memory is the product. If time is short, J-02, J-03, J-04, J-14, J-15, and J-16 are the ones that must not be cut or weakened. Any change to the reconciler prompt or model reruns them at live-model level.
- The **live Slack** smoke checklist passes once in a real workspace: J-01, J-02 (with an actual process restart), J-07 with a real PDF, J-10 with a real second user.

---

## 5. Scope

### In-Scope
- `tests/journeys/` harness, `FakeClock`, `FakeModel` scripts for each journey.
- `live_model` pytest marker registered in `pyproject.toml`, skipped without `GEMINI_API_KEY`.
- `scripts/smoke.md`: the manual live Slack checklist with expected outcomes.
- Deleting Spec 10 cases that assert heuristic wording.

### Out-of-Scope
- Load and latency benchmarking (latency is logged per turn by Spec 11 and reviewed by hand).
- Automating the live Slack level (needs a second bot or user token; revisit later).

---

## 6. Verification

This spec is verified when §4 holds and the live smoke result is recorded with a date in `scripts/smoke.md`.
