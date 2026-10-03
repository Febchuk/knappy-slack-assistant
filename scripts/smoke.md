# Live Slack smoke checklist (Spec 17 §4)

Run before a release, in a real workspace, with real Gemini. It covers what the offline and live-model journeys cannot: Socket Mode, real Block Kit rendering, a real process restart, and a real second person.

## Setup

1. Use a scratch database, never the production one: `export KNAPPY_DATABASE_URL=sqlite:///smoke.db` (or a temporary Postgres URL), then `rm -f smoke.db`.
2. Make sure no other Knappy process is connected with the same app token. Two Socket Mode connections split the events between them.
3. Start Knappy with `python -m knappy.main` and wait for `Knappy is connected via Socket Mode!`.
4. You need two Slack accounts: the **owner** (you) and a **second user** who will receive a message in J-10.

Record each step's result as `pass`, `fail`, or `blocked`, with a note on anything surprising. Logs are at INFO under the `knappy` logger.

## J-01 Talk

| Step | Do | Expect |
| :--- | :--- | :--- |
| 1 | DM Knappy `hey`. | A `_thinking…_` placeholder appears within a second, then is edited in place into a greeting. No second message. |
| 2 | DM `what's a good way to structure a 1:1 agenda?` | The placeholder becomes a real, structured answer in Slack formatting (bold with single asterisks, bullets, no `#` headings). No refusal. |

Result: ____

## J-02 Remember, across a real restart

| Step | Do | Expect |
| :--- | :--- | :--- |
| 1 | DM `Remember I'm vegetarian and I hate early meetings.` | Knappy confirms it will remember both. The log shows `tool name=remember outcome=ok`. |
| 2 | Stop the process with Ctrl-C. Start it again with `python -m knappy.main`. | It reconnects. `smoke.db` still exists. |
| 3 | Start a new thread (a fresh top-level DM, or reply in a new thread) with `pick a lunch spot near Union Square and suggest a time to meet Sam`. | The suggestion is vegetarian-friendly and the time is not early morning. You did not repeat either preference. |

Result: ____

## J-07 Files in (blocked until Spec 15)

| Step | Do | Expect |
| :--- | :--- | :--- |
| 1 | DM a real PDF (a pricing sheet works well) with the message `summarize this`. | A summary of that PDF. |
| 2 | The next day, or after a restart, start a new thread: `what did that PDF say about pricing?` | An answer drawn from the PDF, naming it. |

Result: ____

## J-10 Act with approval, with a real second user

| Step | Do | Expect |
| :--- | :--- | :--- |
| 1 | DM `I told <second user's name> I'd send the budget by Thursday`. | Knappy records the commitment. |
| 2 | DM `Follow up with <second user's name> about the budget`. | An approval card with the drafted text and Approve, Edit, and Cancel buttons. The reply says the message is drafted, not sent. The second user has received nothing. |
| 3 | Click **Approve & Send** as the owner. | The card becomes a receipt. The second user receives exactly one DM with the drafted text. |
| 4 | Click the button again quickly, or retry from another client. | Still exactly one DM to the second user. |

Known risk: nothing resolves a display name to a Slack user id yet, so a draft addressed to a bare name fails on approval ("Couldn't send"). Mention the second user as `@name` in step 2 so the model sees their id, and note in the results whether a plain name worked.

Result: ____

## Results

| Date | Commit | Model ids | J-01 | J-02 | J-07 | J-10 | Who ran it | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| | | | | | | | | |
