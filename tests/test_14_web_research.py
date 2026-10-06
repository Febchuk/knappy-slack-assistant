"""Spec 14: web_search, fetch_url with its SSRF guard, and citations."""

from __future__ import annotations

import ipaddress
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from google.genai import types

from fakes import FakeSdk, pdf_with
from knappy.agent.loop import AgentLoop, InboundMessage
from knappy.agent.tools import ToolRegistry
from knappy.llm.client import GeminiClient, ModelIds
from knappy.llm.fake import FakeModel, GenerateRequest
from knappy.llm.types import ModelTurn, Source, ToolCall, ToolResult, WebSearchResult
from knappy.web import WebFetcher, is_public

IDS = ModelIds(agent="gemini-3-flash-preview", light="gemini-3.1-flash-lite-preview")
ARTICLE = "<p>" + "The Pro plan costs forty dollars per seat. " * 20 + "</p>"


def loopback_or_public(ip) -> bool:
    return ip.is_loopback or is_public(ip)


ROUTES: dict[str, tuple[int, str, bytes]] = {
    "/article": (200, "text/html; charset=utf-8", f"<html><head><title>Pricing</title></head><body><nav>Home</nav><article><h1>Pricing</h1>{ARTICLE}</article></body></html>".encode()),
    "/bare": (200, "text/html", b"<html><body><script>var x = 1;</script><div>Just a line.</div></body></html>"),
    "/megabyte": (200, "text/plain", ("lorem ipsum " * 90_000).encode()),
    "/huge.pdf": (200, "application/pdf", b"%PDF-1.4\n" + b"x" * (6 * 1024 * 1024)),
    "/doc.pdf": (200, "application/pdf", pdf_with("Quarterly revenue was 12 million")),
    "/image": (200, "image/png", b"\x89PNG"),
    "/to-metadata": (302, "http://169.254.169.254/latest/meta-data/", b""),
    "/loop": (302, "/loop", b""),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/hang":
            time.sleep(30)
            return
        if self.path == "/endless":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b"more text " * 6553)
            except OSError:
                return
        status, kind, body = ROUTES.get(self.path, (404, "text/plain", b"missing"))
        self.send_response(status)
        self.send_header("Location" if status == 302 else "Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def local() -> WebFetcher:
    """The production fetcher, except that this test server's loopback address is allowed."""
    return WebFetcher(allow=loopback_or_public)


class Recording(httpx.AsyncBaseTransport):
    """A transport that answers from a table keyed by Host header, and remembers every request that reached it."""

    def __init__(self, pages: dict[str, httpx.Response] | None = None) -> None:
        self.pages = pages or {}
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        page = self.pages.get(request.headers["host"], httpx.Response(404))
        return httpx.Response(page.status_code, headers=page.headers, stream=httpx.ByteStream(page.content))


def resolver(table: dict[str, str]):
    async def resolve(host: str, port: int):
        return [ipaddress.ip_address(table[host])]

    return resolve


# TEST-WEB-01


def searching(sources: list[Source], answer: str = "Python 3.14 is the latest release.") -> FakeModel:
    async def respond(request: GenerateRequest) -> ModelTurn:
        if isinstance(request.contents[-1], ToolResult):
            return ModelTurn(text=answer)
        return ModelTurn(tool_calls=[ToolCall("c1", "web_search", {"query": "latest python release"})])

    return FakeModel(respond, search=WebSearchResult(answer="3.14", sources=sources, searched_queries=["python"]))


PYTHON = Source(title="python.org", url="https://www.python.org/downloads/")
NEWS = Source(title="Python | news", url="https://news.example/python-3-14")


async def run_search(model: FakeModel) -> str:
    loop = AgentLoop(ToolRegistry(None, "T", searcher=model), model)  # type: ignore[arg-type]
    return (await loop.run(InboundMessage(text="what's the latest python?", system="s"))).text


async def test_web_01_answer_cites_both_sources_as_slack_links() -> None:
    model = searching([PYTHON, NEWS])
    text = await run_search(model)

    assert model.searches == [("latest python release", "any")]
    assert "<https://www.python.org/downloads/|python.org>" in text
    assert "<https://news.example/python-3-14|Python   news>" in text, "a | in a title would end the Slack label"


async def test_web_01_citations_stop_at_three_and_respect_the_models_own() -> None:
    many = [Source(title=f"s{index}", url=f"https://s{index}.example/") for index in range(5)]
    capped = await run_search(searching(many))
    cited = await run_search(searching([PYTHON, NEWS], answer="See <https://www.python.org/downloads/|python.org>."))

    assert capped.count("<https://") == 3
    assert cited == "See <https://www.python.org/downloads/|python.org>.", "the model already cited; nothing appended"


async def test_web_search_reports_unavailable_without_a_searcher() -> None:
    assert await ToolRegistry(None, "T").web_search("anything") == {"error": "Web search is not available in this context."}  # type: ignore[arg-type]


def grounded(uris: list[str], queries: list[str]) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=[types.Part.from_text(text="Python 3.14.")]),
                grounding_metadata=types.GroundingMetadata(
                    web_search_queries=queries,
                    grounding_chunks=[
                        types.GroundingChunk(web=types.GroundingChunkWeb(uri=uri, title=f"site {index}"))
                        for index, uri in enumerate(uris)
                    ],
                ),
            )
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=1000, candidates_token_count=0),
    )


async def test_client_search_is_one_grounded_call_with_deduplicated_sources_and_query_cost() -> None:
    uris = [f"https://r.example/{index}" for index in range(10)]
    sdk = FakeSdk([grounded([uris[0], uris[0], *uris[1:]], ["python release", "python 3.14"])])
    costs: list[float] = []

    async def on_usage(tier, model, usage) -> None:
        costs.append(usage.cost_usd)

    result = await GeminiClient("k", IDS, sdk=sdk, on_usage=on_usage).search("latest python", "week")

    assert result.answer == "Python 3.14."
    assert [source.url for source in result.sources] == uris[:8], "deduplicated, at most 8"
    assert result.sources[0].title == "site 0"
    assert result.searched_queries == ["python release", "python 3.14"]
    config = sdk.calls[0]["config"]
    assert [tool.function_declarations for tool in config.tools] == [None]
    window = config.tools[0].google_search.time_range_filter
    assert (window.end_time - window.start_time).days == 7 and window.end_time <= datetime.now(timezone.utc)
    assert window.end_time.microsecond == 0, "the API rejects sub-second times"
    assert costs == [pytest.approx(1000 * 0.50 / 1e6 + 2 * 0.014)], "tokens plus $14 per 1,000 search queries"


async def test_client_search_any_recency_has_no_time_filter() -> None:
    sdk = FakeSdk([grounded([], [])])
    result = await GeminiClient("k", IDS, sdk=sdk).search("q")
    assert result.sources == [] and sdk.calls[0]["config"].tools[0].google_search.time_range_filter is None


# TEST-WEB-02


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://10.0.0.1/",
        "http://0.0.0.0/",
        "http://localhost/",
        "file:///etc/passwd",
        "ftp://example.com/",
    ],
)
async def test_web_02_production_guard_blocks_private_addresses_without_a_request(url: str) -> None:
    transport = Recording()
    result = await WebFetcher(transport=transport).fetch(url)

    assert result == {"error": "blocked"}
    assert transport.requests == [], "nothing left the host"


async def test_web_02_a_name_that_resolves_privately_is_blocked() -> None:
    transport = Recording()
    fetcher = WebFetcher(transport=transport, resolve=resolver({"intranet.example": "10.1.2.3"}))
    assert await fetcher.fetch("https://intranet.example/") == {"error": "blocked"}
    assert transport.requests == []


async def test_web_02_every_redirect_hop_is_checked() -> None:
    transport = Recording({"public.example": httpx.Response(302, headers={"location": "http://sneaky.example/admin"})})
    fetcher = WebFetcher(
        transport=transport, resolve=resolver({"public.example": "93.184.216.34", "sneaky.example": "127.0.0.1"})
    )
    assert await fetcher.fetch("http://public.example/") == {"error": "blocked"}
    assert [request.headers["host"] for request in transport.requests] == ["public.example"]


async def test_web_02_redirect_to_metadata_from_a_real_server_is_blocked(server: str, local: WebFetcher) -> None:
    assert await local.fetch(f"{server}/to-metadata") == {"error": "blocked"}


async def test_web_02_connects_to_the_address_it_checked() -> None:
    page = httpx.Response(200, headers={"content-type": "text/plain"}, content=b"hello")
    transport = Recording({"public.example": page})
    fetcher = WebFetcher(transport=transport, resolve=resolver({"public.example": "93.184.216.34"}))
    result = await fetcher.fetch("https://public.example/path?q=1")

    assert result["text"] == "hello" and result["url"] == "https://public.example/path?q=1"
    sent = transport.requests[0]
    assert str(sent.url) == "https://93.184.216.34/path?q=1", "a second DNS answer cannot redirect the connection"
    assert sent.headers["host"] == "public.example" and sent.extensions["sni_hostname"] == "public.example"
    assert sent.headers["user-agent"] == "KnappyBot/1.0"


# TEST-WEB-03


async def test_web_03_long_page_is_truncated_to_20k_chars(server: str, local: WebFetcher) -> None:
    result = await local.fetch(f"{server}/megabyte")
    assert len(result["text"]) == 20_000 and result["truncated"] is True


async def test_web_03_download_stops_at_5_mb(server: str, local: WebFetcher) -> None:
    started = time.monotonic()
    endless = await local.fetch(f"{server}/endless")

    assert endless["truncated"] is True and len(endless["text"]) == 20_000
    assert time.monotonic() - started < 5, "stopped reading at the cap, not at the timeout"
    assert await local.fetch(f"{server}/huge.pdf") == {"error": "The PDF is larger than 5 MB."}


async def test_short_page_is_not_truncated(server: str, local: WebFetcher) -> None:
    result = await local.fetch(f"{server}/article")
    assert result["truncated"] is False
    assert result["title"] == "Pricing"
    assert "forty dollars per seat" in result["text"] and "Home" not in result["text"], "main content, not navigation"


async def test_question_keeps_the_relevant_part_of_a_long_page() -> None:
    filler = "\n\n".join(f"Paragraph {index} about gardening and soil." for index in range(2000))
    body = f"{filler}\n\nThe refund window is 30 days from delivery.\n\n{filler}".encode()
    page = httpx.Response(200, headers={"content-type": "text/plain"}, content=body)
    fetcher = WebFetcher(transport=Recording({"shop.example": page}), resolve=resolver({"shop.example": "93.184.216.34"}))
    result = await fetcher.fetch("https://shop.example/terms", question="how long is the refund window?")

    assert "refund window is 30 days" in result["text"]
    assert len(result["text"]) <= 20_000 and result["truncated"] is True


async def test_html_without_main_content_falls_back_to_body_text(server: str, local: WebFetcher) -> None:
    result = await local.fetch(f"{server}/bare")
    assert result["text"] == "Just a line."


async def test_pdf_text_is_extracted(server: str, local: WebFetcher) -> None:
    result = await local.fetch(f"{server}/doc.pdf")
    assert "Quarterly revenue was 12 million" in result["text"]


async def test_unreadable_types_redirect_loops_and_http_errors_are_errors(server: str, local: WebFetcher) -> None:
    assert "image/png" in (await local.fetch(f"{server}/image"))["error"]
    assert "TooManyRedirects" in (await local.fetch(f"{server}/loop"))["error"]
    assert "404" in (await local.fetch(f"{server}/nowhere"))["error"]


# TEST-WEB-04


async def test_web_04_unresponsive_server_times_out_and_the_agent_still_answers(server: str) -> None:
    async def respond(request: GenerateRequest) -> ModelTurn:
        last = request.contents[-1]
        if isinstance(last, ToolResult):
            return ModelTurn(text=f"I couldn't read that page: {last.result['error']}")
        return ModelTurn(tool_calls=[ToolCall("c1", "fetch_url", {"url": f"{server}/hang"})])

    model = FakeModel(respond)
    loop = AgentLoop(ToolRegistry(None, "T", searcher=model, fetcher=WebFetcher(allow=loopback_or_public)), model)  # type: ignore[arg-type]
    started = time.monotonic()
    reply = await loop.run(InboundMessage(text="tl;dr this", system="s"))

    assert time.monotonic() - started < 11
    assert reply.text.startswith("I couldn't read that page: Timed out after 10 s")
