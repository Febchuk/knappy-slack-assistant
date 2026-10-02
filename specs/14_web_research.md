# Specification 14: Web Research

## 1. Overview & Objectives

Knappy cannot answer a factual question or read a link ([GAP-06](./00_gap_analysis.md)). This spec adds two read-only tools, `web_search` and `fetch_url`. With them the agent can research, check facts, and read pages the user pastes, and it cites its sources.

**Amends:** [Spec 04](./04_conversational_react_agent.md) §5, which listed web search as out of scope.

---

## 2. `web_search`

```python
class WebSearchArgs(BaseModel):
    query: str
    recency: Literal["any", "week", "month"] = "any"

class WebSearchResult(BaseModel):
    answer: str                      # grounded summary written by the search call
    sources: list[Source]            # [{title, url}], deduplicated, at most 8
    searched_queries: list[str]
```

**Implementation:** a separate Gemini call (tier `agent`) with the built-in Google Search grounding tool enabled and **no** function declarations. It returns `answer` plus `sources` taken from the response's grounding metadata.

Why a separate call: the Gemini API has documented that the Google Search tool cannot be combined with custom function declarations in the same request. Some newer models relax this. Wrapping search as our own function works either way, and keeps every tool visible in our own logs and step limits. Revisit this if the chosen model supports combining them natively.

Cost: one grounded request per call, so it counts toward the owner's budget ([Spec 11](./11_model_layer_gemini.md) §5) at grounded-request pricing.

---

## 3. `fetch_url`

```python
class FetchUrlArgs(BaseModel):
    url: HttpUrl
    question: str | None = None      # optional focus for long pages

class FetchUrlResult(BaseModel):
    url: str
    title: str | None
    text: str                        # readable text, truncated to 20k chars
    truncated: bool
```

- `httpx` async GET: 10 s timeout, at most 5 redirects, at most 5 MB downloaded, user agent `KnappyBot/1.0`.
- HTML → readable text with `trafilatura`, falling back to stripped `<body>` text.
- PDF URLs go through the same text extraction as uploaded files ([Spec 15](./15_files_and_documents.md)).
- **SSRF guard:** refuse non-`http(s)` schemes, and refuse hosts that resolve to private, loopback, or link-local addresses. Return `{"error": "blocked"}`.
- When the user pastes a URL into a message, the agent prompt tells the model it may fetch it. No automatic fetch.

---

## 4. Answer Rules (system prompt additions)

- Use `web_search` for anything current, factual and checkable, or outside what memory knows. Do not guess at prices, dates, news, or availability.
- Cite sources as Slack links `<url|title>` at the end of the answer, at most 3.
- If search returns nothing useful, say so and answer from general knowledge, labeled as such.
- Research results are not remembered automatically. If the user wants to keep something, they say so and the agent calls `remember` ([Spec 13](./13_memory_system.md)).

---

## 5. Scope

### In-Scope
- `web_search`, `fetch_url`, SSRF guard, citations.
- `httpx` and `trafilatura` added to `pyproject.toml`.

### Out-of-Scope
- Logged-in pages, forms, or clicking through sites (that is wave 3's sandboxed browser).
- Watching or monitoring pages over time.
- Crawling more than the one requested URL.

---

## 6. Verification

| Test Case ID | Procedure | Expected Outcome |
| :--- | :--- | :--- |
| **TEST-WEB-01** | Fake model calls `web_search`. The fake grounded client returns two sources. | The final answer contains both as Slack links. |
| **TEST-WEB-02** | `fetch_url("http://127.0.0.1:8080")` and `fetch_url("http://169.254.169.254/")`. | Both return `{"error": "blocked"}` without a request leaving the host. |
| **TEST-WEB-03** | `fetch_url` on a local test server page of 1 MB of text. | `text` has 20k chars and `truncated=True`. |
| **TEST-WEB-04** | `fetch_url` on a server that never responds. | Returns a timeout error within 11 s. The agent still answers. |
| **TEST-WEB-05** (live) | DM "what's the weather in Lagos today?" against real Gemini. | The reply cites at least one source URL. |
