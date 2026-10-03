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

## J-07 Files in, and a document out

Needs the `files:read` and `files:write` scopes from `slack/manifest.yml`. Reinstall the app after updating the manifest, or Slack serves a sign-in page instead of the file and Knappy replies that it couldn't download it.

| Step | Do | Expect |
| :--- | :--- | :--- |
| 1 | DM a real PDF (a pricing sheet works well) with the message `summarize this`. | The placeholder reads `_reading <name>.pdf…_`, then becomes a summary of that PDF. The log shows `document stored`. |
| 2 | The next day, or after a restart, start a new thread: `what did that PDF say about pricing?` | An answer drawn from the PDF, naming it. The log shows `list_files`, `read_file`, or `memory_search`, not `web_search`. |
| 3 | DM a screenshot with `what does this say?` | An answer that reads the screenshot's text. |
| 4 | DM `write me a one-page launch plan for the Q3 offsite`. | A `.md` file arrives in your DM with a short summary next to it. No approval card. |
| 5 | DM `send that plan to <@second user>`. | An approval card naming the file. The second user receives nothing until you approve; then exactly one file. |

## J-10 Act with approval, with a real second user

| Step | Do | Expect |
| :--- | :--- | :--- |
| 1 | DM `I told <second user's name> I'd send the budget by Thursday`. | Knappy records the commitment. |
| 2 | DM `Follow up with <second user's name> about the budget`. | An approval card with the drafted text and Approve, Edit, and Cancel buttons. The reply says the message is drafted, not sent. The second user has received nothing. |
| 3 | Click **Approve & Send** as the owner. | The card becomes a receipt. The second user receives exactly one DM with the drafted text. |
| 4 | Click the button again quickly, or retry from another client. | Still exactly one DM to the second user. |

Step 2 uses a plain name on purpose. Knappy resolves it at staging time: a mention, the contact's stored Slack id, `users.lookupByEmail` with the contact's email, then a unique match in the workspace directory. The card names the person as `Name (@handle)`; check it is the right person. If the name matches nobody or several people, the card shows the text with no send button and says why. Email lookup needs the `users:read.email` scope: reinstall the app after updating `slack/manifest.yml`.

Result: ____

## J-11 Proactive brief and follow-up

Use a scratch database. Your Slack profile's timezone decides when the brief comes.

| Step | Do | Expect |
| :--- | :--- | :--- |
| 1 | DM `I told <second user's name> I'd send the deck by 10am tomorrow`. | Knappy records the commitment. |
| 2 | Leave Knappy running overnight, or rerun the next morning after 08:00 your time with `python -m knappy.scheduler --run-now`. | Nothing arrives between 21:00 and 08:00. One brief arrives after 08:00 your time, listing the deck, with a drafted message to the second user and Send, Edit, Mark as Done, and Snooze buttons. The log shows one `heartbeat tick owner=... outcome=sent` and `outcome=silent` on the other ticks. |
| 3 | Read the draft. | It is written to the second user in your voice, not the reminder to you. |
| 4 | Reply in the brief's thread: `actually tell them I need until Monday`. | Knappy answers in the thread with a new draft card, using the brief as context. No second commitment appears in `what do I owe people?`. |
| 5 | Click **Send** on the original draft, or the new one. | The second user gets exactly that text. The brief's other items keep their buttons. |

Result: ____

## Results

| Date | Commit | Model ids | J-01 | J-02 | J-07 | J-10 | J-11 | Who ran it | Notes |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| | | | | | | | | | |
