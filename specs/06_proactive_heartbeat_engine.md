# Specification 06: Proactive Heartbeat Engine

## 1. Overview & Objectives

Traditional conversational bots are purely reactive: they remain silent until a user explicitly sends a prompt. 

Following Google Cloud's **Evaluator-Optimizer / Monitor Pattern**, Knappy's **Proactive Heartbeat Engine** periodically monitors state to reach out unprompted with:
1. **Morning Briefings (Daily at 8:00 AM):** Synthesizes upcoming deadlines and dossiers for upcoming meetings.
2. **Upcoming Commitment Alerts:** Alerts users to commitments due within the next 12 hours.
3. **Relationship Cadence Check-Ins:** Detects important contacts who have not been contacted within their defined reminder window (e.g., > 30 days).

```mermaid
flowchart TD
    Tick["Heartbeat Scheduler Tick (Every 30m / Daily 8:00 AM)"] --> Scanner["1. Deterministic SQL Scanner (Zero LLM, < 5ms)"]
    
    Scanner --> Evaluate{"Candidate Records Found?"}
    Evaluate -->|No Matches| Sleep["Exit Immediately (Cost: $0.00)"]
    
    Evaluate -->|Candidates Found| Triage["2. Jev Alert Triage Gate (System 1 Scoring, ~80ms)"]
    Triage --> TriageEval{"Interruptibility Decision?"}
    TriageEval -->|SUPPRESS_NOISE| Sleep
    TriageEval -->|QUEUE_MORNING_DIGEST| Queue["Queue for Daily 8:00 AM Briefing"]
    TriageEval -->|DISPATCH_IMMEDIATE_DM| Aggregator["3. Context Aggregator (Fetch Contact & Interaction History)"]
    
    Aggregator --> Synthesizer["4. Proactive Synthesizer (Fast SLM)"]
    Synthesizer --> AsymmetricEgress["5. Slack Asymmetric Egress (chat.postMessage to User DM)"]
    
    AsymmetricEgress --> InteractiveDM["Deliver Block Kit Card with 1-Click Action Buttons"]
    InteractiveDM --> UserChoice{"User Response"}
    
    UserChoice -->|Clicks [Execute Action]| HITLPath["Routes to HITL Approval Gateway (Spec 05)"]
    UserChoice -->|Replies in Thread| ReActHandoff["Converts Thread into Reactive ReAct Session (Spec 04)"]
```

---

## 2. Zero-Cost Deterministic Sweeper

Running an LLM every 15 minutes to evaluate whether to send a message would incur massive API bills. Knappy guards all proactive actions with **zero-cost SQL evaluation**. 95%+ of background ticks execute in $< 5\text{ ms}$ and terminate at $\$0.00$ cost.

### 2.1 Upcoming Commitments Scan
Executed every 30 minutes:
```sql
SELECT 
    c.id as contact_id,
    c.name as contact_name,
    c.slack_user_id,
    i.id as interaction_id,
    i.commitment,
    i.due_date
FROM interactions i
JOIN contacts c ON i.contact_id = c.id
WHERE i.status = 'PENDING'
  AND i.commitment IS NOT NULL
  AND i.due_date IS NOT NULL
  AND i.due_date <= CURRENT_TIMESTAMP + INTERVAL '12 hours'
  AND (i.last_alerted_at IS NULL OR i.last_alerted_at < CURRENT_TIMESTAMP - INTERVAL '24 hours');
```

### 2.2 Network Cadence Scan
Executed daily at 8:00 AM, on the same tick as the morning briefing:
```sql
SELECT 
    id as contact_id,
    name,
    company,
    reminder_cadence_days,
    last_interaction_ts
FROM contacts
WHERE reminder_cadence_days IS NOT NULL
  AND last_interaction_ts <= CURRENT_TIMESTAMP - (reminder_cadence_days || ' days')::INTERVAL;
```

---

## 3. Jev-Powered Alert Triage & Fatigue Prevention

Deterministic SQL queries can return multiple candidate records (e.g., 6 commitments due within 12 hours, or 10 contacts exceeding their cadence). Firing immediate Slack DMs for every database match overwhelms the user and leads to bot muting.

Following TypeSafe AI's **Confidence-Gated Routing & Composite Scoring Pattern** (`docs/skills/SKILL.md`), candidate records found by the SQL scanner pass through a **Jev System 1 Alert Triage Gate** before invoking generative SLMs:

```python
from typesafe_sdk import AsyncTypeSafeClient, Noul, Choice, Score
from typing import Optional, Literal

class ProactiveAlertTriager:
    """
    Evaluates candidate alert records using TypeSafe System One (Jev).
    Uses TypeSafe primitives:
    - Noul: Probability that candidate justifies an immediate unprompted interruption.
    - Choice: Selects routing strategy across competing delivery channels.
    - Score: Graded evaluation of business consequence and urgency.
    """
    IMMEDIATE_INTERRUPT_THRESHOLD = 0.75
    STRATEGY_CONFIDENCE_THRESHOLD = 0.65

    @classmethod
    async def triage_candidate(cls, candidate: dict) -> dict:
        # Step 1: Prepare structured state with backticked path references
        state = {
            "candidate": {
                "contact_name": candidate.get("contact_name"),
                "company": candidate.get("company"),
                "commitment": candidate.get("commitment"),
                "due_date": candidate.get("due_date"),
                "hours_until_due": candidate.get("hours_until_due"),
                "days_since_last_contact": candidate.get("days_since_last_contact")
            }
        }

        # Step 2: Formulate narrow, independent questions evaluated concurrently
        questions = {
            # Noul: Condition evaluation (probability of yes)
            "is_interrupt_worthy": Noul(
                instructions="Does `candidate.commitment` or deadline represent a critical, time-sensitive priority that justifies interrupting an executive with an immediate unprompted Slack DM?"
            ),
            # Choice: Disjoint routing channels with descriptive criteria
            "delivery_strategy": Choice(
                instructions="What is the optimal delivery channel for `candidate`?",
                criteria={
                    "immediate_dm": "Send an immediate proactive direct message ping now",
                    "batch_into_morning_digest": "Save for aggregation in the daily 8:00 AM morning executive digest",
                    "suppress_low_value": "Suppress completely; low consequence, noisy, or premature"
                }
            ),
            # Score: Concrete graded consequence levels
            "consequence_score": Score(
                instructions="How severe are the consequences if the user misses this reminder?",
                criteria=[
                    "Negligible consequence, internal ping, or casual check-in",
                    "Moderate impact or normal business deliverable",
                    "High impact, contract milestone, or executive commitment"
                ]
            )
        }

        # Step 3: Fast parallel execution (~80ms)
        async with AsyncTypeSafeClient() as client:
            response = await client.system_one(state=state, questions=questions)

        interrupt_prob = response.nouls["is_interrupt_worthy"].noul
        strategy = response.choices["delivery_strategy"].choice
        strategy_conf = response.choices["delivery_strategy"].confidence
        consequence = response.scores["consequence_score"].score

        # Step 4: Code evaluates thresholds and controls delivery
        if (
            strategy == "immediate_dm" 
            and interrupt_prob >= cls.IMMEDIATE_INTERRUPT_THRESHOLD 
            and strategy_conf >= cls.STRATEGY_CONFIDENCE_THRESHOLD
        ):
            action = "DISPATCH_IMMEDIATE_DM"
        elif strategy == "batch_into_morning_digest" or interrupt_prob >= 0.40:
            action = "QUEUE_MORNING_DIGEST"
        else:
            action = "SUPPRESS_NOISE"

        return {
            "action": action,
            "interrupt_probability": interrupt_prob,
            "strategy": strategy,
            "strategy_confidence": strategy_conf,
            "consequence_score": consequence
        }
```

### 3.1 Triage Rules & Routing Paths
1. **Immediate Pings (`DISPATCH_IMMEDIATE_DM`):** High interrupt probability ($\ge 0.75$) and confidence ($\ge 0.65$). Proceeds directly to Stage 4 (SLM Synthesis) and Stage 5 (Asymmetric Slack DM).
2. **Batched Digest (`QUEUE_MORNING_DIGEST`):** Non-urgent reminders ($> 6$ hours out) or moderate importance items are inserted into `briefing_items` with status `QUEUED` and delivered during the 8:00 AM briefing. After delivery, those rows are marked `DELIVERED`.
3. **Suppressed Noise (`SUPPRESS_NOISE`):** Low-consequence reminders are dropped silently without pinging the user and without inserting a `briefing_items` row. Commitment candidates update `interactions.last_alerted_at = CURRENT_TIMESTAMP` to prevent scanner re-query churn.

---

## 4. Proactive Synthesis & Prompt Contract

When candidate triggers are found and approved by the Jev Triage Gate, context is packaged and dispatched to `gpt-4o-mini`:

```python
PROACTIVE_SYSTEM_PROMPT = """
You are an executive relationship assistant reaching out proactively to the user via Slack.
Be direct, helpful, and concise. Never use fluff or robotic pleasantries.

Instructions:
1. Explain clearly why you are surfacing this now (e.g. deadline approaching in 4 hours, haven't spoken in 30 days).
2. Propose a concrete action draft that the user can execute in one click.
"""
```

---

## 5. Slack Asymmetric Egress & Action Block Kit

Unlike reactive chat where the bot replies using the event's `say()` helper, proactive pings initiate an outbound conversation:

1. **Direct Message Delivery:** Bot opens a new thread in the user's DM using `client.chat_postMessage(channel=user_slack_id)`.
2. **Actionable Block Kit Layout:**
```json
{
  "channel": "{{user_slack_id}}",
  "text": "Reminder: Commitment due for {{contact_name}}",
  "blocks": [
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": ":alarm_clock: *Commitment Due Today*\nYou promised *{{contact_name}}*:\n> \"{{commitment_text}}\""
      }
    },
    {
      "type": "actions",
      "elements": [
        {
          "type": "button",
          "text": { "type": "plain_text", "text": "Send Slack DM" },
          "style": "primary",
          "action_id": "btn_approve_proactive_action",
          "value": "{{draft_id}}"
        },
        {
          "type": "button",
          "text": { "type": "plain_text", "text": "Mark as Done" },
          "action_id": "btn_resolve_commitment",
          "value": "{{interaction_id}}"
        },
        {
          "type": "button",
          "text": { "type": "plain_text", "text": "Snooze (24h)" },
          "action_id": "btn_snooze_commitment",
          "value": "{{interaction_id}}"
        }
      ]
    }
  ]
}
```

---

## 6. Seamless Transition from Proactive to Reactive (Thread Continuity)

If the user does not want to click a 1-click button but instead replies to the proactive message in the thread:
> *User: "Actually, tell her I was caught up in an incident and will send it Monday morning."*

1. The Slack event router identifies `event.thread_ts` matching the proactive message.
2. The message loader retrieves the initial proactive context.
3. The ReAct agent takes over the thread, updates the staged action payload, and renders a new approval card in the same thread.

---

## 7. Scope Boundaries

### In-Scope
- Background scheduler running deterministic checks at defined intervals (every 30 mins / daily morning).
- Sub-100ms Jev-based alert triage to eliminate notification fatigue.
- Proactive DM dispatch to the user.
- 1-click action buttons (Approve, Mark as Done, Snooze).
- Graceful handoff from proactive outbound ping to reactive thread conversation.

### Out-of-Scope
- Spamming public channels with unprompted reminders.
- Scheduling proactive voice calls or SMS alerts.
- Polling external web apis continuously when no database triggers exist.

---

## 8. Verification Plan

| Test Case ID | Test Focus | Scenario | Expected Outcome |
| :--- | :--- | :--- | :--- |
| **TEST-PROACT-01** | Zero Trigger Cost | Run scheduler tick with 0 overdue items | DB query finishes in $< 5\text{ ms}$; 0 LLM calls made; 0 Slack messages sent. |
| **TEST-PROACT-02** | High-Priority Alert Trigger | Commitment due in 2 hours with high counterparty importance | Triage returns `action="DISPATCH_IMMEDIATE_DM"`, `strategy="immediate_dm"`, `interrupt_probability >= 0.75`; bot posts proactive DM. |
| **TEST-PROACT-03** | Alert Noise Suppression | Trivial reminder due in 11 hours | Triage returns `action="QUEUE_MORNING_DIGEST"`, `strategy="batch_into_morning_digest"`; a `briefing_items` row is inserted with status `QUEUED` and no immediate DM is sent. |
| **TEST-PROACT-04** | Snooze Action | User clicks `[Snooze (24h)]` | `due_date` updated in DB $+24\text{ hours}$; message updated to confirm snooze. |
| **TEST-PROACT-05** | Mark as Done | User clicks `[Mark as Done]` | Status in `interactions` updated to `'FULFILLED'`; message updated to confirmed complete. |
| **TEST-PROACT-06** | Thread Handoff | User replies with text to proactive DM | ReAct agent loads proactive context and replies in thread with revised action card. |
