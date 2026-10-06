# Specification 11: Model Layer (Gemini)

## 1. Overview & Objectives

Knappy has no model today. Every seam that should call one is filled by a regex heuristic ([GAP-01](./00_gap_analysis.md)). This spec adds one model layer, `knappy/llm/`, and wires it into every seam in production.

Gemini Flash is the default model. The layer is small and Knappy-shaped, not a general provider abstraction: one client, typed calls, a fake for tests.

---

## 2. Seams to Fill

| Seam | Today | Signature | Model tier |
| :--- | :--- | :--- | :--- |
| Agent turn | `heuristic_complete` (`knappy/runtime.py`) | `complete(messages, tools) -> ModelTurn` | `agent` |
| Ingestion extraction | `heuristic_extract` via `SlmExtractor()` (`knappy/ingestion/extract.py`) | `extract(prompt, text) -> dict` (Pydantic-validated) | `light` |
| Alert triage | `heuristic_triage` (`knappy/runtime.py`) | `triage(candidate) -> dict` | `light` |
| Memory reconciliation | none | `reconcile(...)` ([Spec 13](./13_memory_system.md)) | `light` |
| Profile generation | none | `summarize(...)` ([Spec 13](./13_memory_system.md)) | `agent` |
| Web search | none | grounded call ([Spec 14](./14_web_research.md)) | `agent` |

`heuristic_intent` and the regex router are removed from the main path by [Spec 12](./12_agent_loop_v2.md). All `heuristic_*` functions move to `tests/fakes.py` as named test doubles. Production code never falls back to them.

---

## 3. Module Contract

```
knappy/llm/
  __init__.py
  client.py     # GeminiClient: generate(), generate_structured(), with retries and logging
  types.py      # ModelTurn, ToolCall, ToolSpec, Usage
  tools.py      # converts ToolRegistry entries to Gemini function declarations
  fake.py       # FakeModel: scripted turns for tests
```

```python
@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]

@dataclass
class ModelTurn:
    text: str | None                 # final answer when no tool calls
    tool_calls: list[ToolCall]       # zero or more; parallel calls allowed
    usage: Usage                     # input, output, cached tokens; cost_usd

class GeminiClient:
    async def generate(self, *, tier: Literal["agent", "light"], system: str,
                       contents: list[Content], tools: list[ToolSpec] | None = None,
                       timeout_s: float = 30) -> ModelTurn: ...
    async def generate_structured(self, *, tier, system: str, text: str,
                                  schema: type[BaseModel]) -> BaseModel: ...
```

- SDK: `google-genai` (async client), added to `pyproject.toml`.
- Function calling uses native function declarations generated from the tool registry. Tool parameters are described by Pydantic models, so the same schema validates the model's arguments before a tool runs.
- `ModelTurn` replaces the current single-tool `ModelTurn` in `knappy/agent/react.py`. Several tool calls in one turn are allowed.
- Structured calls use `response_schema`, then validate with the Pydantic model. One retry on validation failure, then raise.

---

## 4. Configuration

| Variable | Required | Default | Purpose |
| :--- | :--- | :--- | :--- |
| `GEMINI_API_KEY` | yes | — | API key. Startup fails with `ConfigError` when missing, like the Slack tokens. |
| `KNAPPY_MODEL_AGENT` | no | current Gemini Flash id | Agent loop, profile, web search |
| `KNAPPY_MODEL_LIGHT` | no | current Gemini Flash-Lite id | Extraction, triage, reconciliation |
| `KNAPPY_DAILY_BUDGET_USD` | no | `1.00` | Per-owner daily spend ceiling |

`Settings.from_env` (`knappy/config.py`) gains these fields. Model ids are config values, not code constants. Verify the current Flash and Flash-Lite ids against Google's model list when implementing; at the time of writing the Gemini 3 Flash preview id is `gemini-3-flash-preview`.

---

## 5. Reliability and Cost

- **Timeouts and retries:** 30 s per agent call, 10 s per light call. Retry twice with backoff on 429 and 5xx. No retry on 4xx validation errors.
- **Logging:** each call logs `owner`, `tier`, `model`, latency, input, output, and cached tokens, and estimated cost. Never log message content at INFO.
- **Budget:** a `model_usage` table records `(owner_user_id, day, cost_usd)`. When an owner passes `KNAPPY_DAILY_BUDGET_USD`, the agent replies that it has hit today's limit instead of calling the model. Background reconciliation for that owner pauses until the next day.
- **Caching:** the system prompt and tool declarations come first and stay stable between turns, so Gemini's implicit prompt caching applies. Per-turn context (profile, recap) follows them.

---

## 6. Scope

### In-Scope
- `knappy/llm/` client, types, tool conversion, and fake.
- Wiring the client into `KnappyRuntime` in `knappy/main.py` for every seam in §2.
- Config, budget table, and usage logging.

### Out-of-Scope
- Other providers. If one is needed later, it implements the same `GeminiClient` methods.
- Fine-tuning, local models, and streaming token output to Slack (the placeholder in [Spec 12](./12_agent_loop_v2.md) covers perceived latency).

---

## 7. Verification

| Test Case ID | Procedure | Expected Outcome |
| :--- | :--- | :--- |
| **TEST-LLM-01** | Start `knappy.main` without `GEMINI_API_KEY`. | `ConfigError` naming the variable. Process exits non-zero. |
| **TEST-LLM-02** | Build the runtime as `main.py` does and inspect every seam in §2. | No seam is a `heuristic_*` function. |
| **TEST-LLM-03** | `FakeModel` returns two parallel tool calls. | Both tools run. Both results go back in the next request. |
| **TEST-LLM-04** | Model returns tool args that fail the tool's Pydantic schema. | The tool does not run. The validation error goes back as the tool result. |
| **TEST-LLM-05** | Owner's recorded spend exceeds the budget. | No model call. The user gets the limit message. |
| **TEST-LLM-06** (live, `-m live`) | One real `generate` with a single tool against Gemini. | Returns a valid `ModelTurn` with usage populated. |
