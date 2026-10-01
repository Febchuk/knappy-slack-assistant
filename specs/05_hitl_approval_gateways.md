# Specification 05: Human-in-the-Loop (HITL) Approval Gateway

## 1. Overview & Objectives

In accordance with Google Cloud's **Human-in-the-Loop (HITL) Pattern**, Knappy enforces a strict safety boundary: **No autonomous blind writes to external systems.**

Any state-mutating operation (dispatching a Slack message, sending an email draft, scheduling a calendar event, or posting a broadcast) must pass through a two-phase staged approval workflow rendered via Slack Block Kit interactive components.

```mermaid
sequenceDiagram
    autonumber
    actor User as Slack User
    participant ReAct as ReAct Agent
    participant DB as action_drafts Table
    participant Router as Bolt Interactivity Router
    participant Worker as Execution Worker
    participant External as External Service (Slack API / Gmail)

    User->>ReAct: "Follow up with Sarah and ask for the deck"
    ReAct->>DB: Stage draft (status: PENDING, draft_id: UUID)
    ReAct->>User: Render Block Kit card with [Approve & Send] & [Cancel]
    
    alt User clicks [Cancel]
        User->>Router: Interaction payload (action_id: btn_cancel_action)
        Router->>DB: UPDATE status = 'CANCELLED'
        Router->>User: chat.update -> ":x: Action cancelled."
    else User clicks [Approve & Send]
        User->>Router: Interaction payload (action_id: btn_approve_action, value: draft_id)
        Router->>Router: Verify clicker user_id == draft.user_id
        Router->>DB: Atomic CAS (UPDATE status = 'APPROVED' WHERE status = 'PENDING'; executed_at stays null)
        alt Already executed or cancelled
            Router->>User: Ignore duplicate click (Idempotent)
        else First execution
            Router->>Worker: Dispatch verified payload
            Worker->>External: Execute API call
            Worker->>DB: SET executed_at (status stays APPROVED; FAILED if the call errors)
            Router->>User: chat.update -> ":white_check_mark: Executed: Dispatched to Sarah."
        end
    end
```

---

## 2. Staged Action Schema & State Machine

### 2.1 State Transitions
```text
  [ PENDING ] ──► [ CANCELLED ]  (User clicked Cancel)
       │
       ├────────► [ EXPIRED ]    (expires_at passed)
       │
       ▼ (CAS wins; executed_at still null)
  [ APPROVED ] ──► executed_at set (API succeeded; status stays APPROVED)
       │
       ▼
  [ FAILED ]                     (API failed; draft is terminal)
```

### 2.2 Draft Data Contract
Every draft stored in `action_drafts` contains:
```python
from pydantic import BaseModel
from typing import Literal, Any
from datetime import datetime

class ActionDraftPayload(BaseModel):
    action_type: Literal["SEND_SLACK_DM", "GMAIL_DRAFT", "CALENDAR_INVITE", "POST_CHANNEL"]
    recipient_identifier: str       # Contact ID, email, or channel ID
    recipient_name: str
    preview_summary: str           # User-readable description
    staged_content: str            # Exact body of the message or invitation
    metadata: dict[str, Any] = {}
```

---

## 3. Slack Block Kit Interactive UI Specifications

When an action is staged, Knappy delivers an interactive Block Kit message. If staged inside a shared channel, Knappy posts using `chat.postEphemeral` to protect sensitive draft contents; in 1-on-1 DMs, it posts directly via `chat.postMessage`.

### Block Kit JSON Layout
```json
{
  "blocks": [
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": "*Action Required:* Staged Outbound Message\n*Target:* {{recipient_name}}"
      }
    },
    {
      "type": "section",
      "text": {
        "type": "mrkdwn",
        "text": "> {{staged_content}}"
      }
    },
    {
      "type": "actions",
      "block_id": "hitl_action_block_{{draft_id}}",
      "elements": [
        {
          "type": "button",
          "action_id": "btn_approve_action",
          "text": {
            "type": "plain_text",
            "text": "Approve & Send"
          },
          "style": "primary",
          "value": "{{draft_id}}"
        },
        {
          "type": "button",
          "action_id": "btn_edit_draft",
          "text": {
            "type": "plain_text",
            "text": "Edit"
          },
          "value": "{{draft_id}}"
        },
        {
          "type": "button",
          "action_id": "btn_cancel_action",
          "text": {
            "type": "plain_text",
            "text": "Cancel"
          },
          "style": "danger",
          "value": "{{draft_id}}"
        }
      ]
    }
  ]
}
```

---

## 4. Security & Idempotency Safeguards

1. **Authorization Verification**:
   - The user ID extracted from `payload["user"]["id"]` **must** match `action_drafts.user_id`.
   - If an unauthorized user in the channel clicks the button, the system returns an ephemeral rejection: `"Unauthorized: Only the creator of this request can approve it."`
2. **Atomic Compare-And-Swap (CAS)**:
   - To prevent double-execution from rapid multi-clicks or network retries, execution is guarded by:
   ```sql
   UPDATE action_drafts
   SET status = 'APPROVED'
   WHERE id = :draft_id AND status = 'PENDING';
   ```
   - If the update returns 0 affected rows, the event is immediately discarded.
   - `executed_at` is set only after the external call succeeds. If that call fails, status moves from `APPROVED` to `FAILED` and the draft is terminal.
3. **In-Place Immutable Receipt**:
   - Immediately upon receiving the approval click, the interactive buttons are stripped using `client.chat_update()`, rendering an immutable receipt:
   ```json
   {
     "blocks": [
       {
         "type": "section",
         "text": {
           "type": "mrkdwn",
           "text": ":white_check_mark: *Executed:* Action approved by <@{{user_id}}> and dispatched to *{{recipient_name}}* at {{timestamp}}."
         }
       }
     ]
   }
   ```

---

## 5. Scope Boundaries

### In-Scope
- Staging and persisting outbound actions into `action_drafts`.
- Rendering Block Kit approval cards with Approve, Edit modal trigger, and Cancel buttons.
- Verification of clicker authorization.
- In-place block updates guaranteeing idempotency.

### Out-of-Scope
- Blind automated execution of state-mutating actions without user interaction.
- Third-party payment transactions or bank transfers.
- Irreversible destructive actions (e.g. deleting channels, bulk database drops).

---

## 6. Verification Plan

| Test Case ID | Test Focus | Scenario | Expected Outcome |
| :--- | :--- | :--- | :--- |
| **TEST-HITL-01** | Draft Staging | ReAct agent invokes `stage_outbound_action` | Row created in `action_drafts` with status `PENDING`; Block Kit message emitted with draft UUID. |
| **TEST-HITL-02** | Authorized Approval | Authorized user clicks `[Approve & Send]` | Status updates to `APPROVED`; external API is dispatched; message updates to green checkmark receipt. |
| **TEST-HITL-03** | Unauthorized Rejection | Different user in channel clicks `[Approve & Send]` | API call is blocked; ephemeral message warns user; draft remains `PENDING`. |
| **TEST-HITL-04** | Double-Click Idempotency | User double-clicks `[Approve & Send]` within 100ms | Only one execution occurs; second event is rejected by atomic CAS check. |
| **TEST-HITL-05** | Cancellation | User clicks `[Cancel]` | Status updates to `CANCELLED`; message updates to `:x: Action cancelled`; no outbound call is made. |
