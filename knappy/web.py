"""Reading web pages for the agent (Spec 14 §3), and citing what was read."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import trafilatura
from pydantic import BaseModel

from knappy.files.extract import UnreadableError, pdf_page_texts

logger = logging.getLogger("knappy")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolve = Callable[[str, int], Awaitable[list[IPAddress]]]

TIMEOUT_S = 10.0
MAX_REDIRECTS = 5
MAX_BYTES = 5 * 1024 * 1024
MAX_CHARS = 20_000
USER_AGENT = "KnappyBot/1.0"
MAX_CITATIONS = 3
BLOCKED = {"error": "blocked"}


class FetchUrlResult(BaseModel):
    url: str
    title: str | None
    text: str
    truncated: bool


def is_public(ip: IPAddress) -> bool:
    """The SSRF policy: only globally routable unicast addresses. IPv4-mapped IPv6 counts as not global."""
    return ip.is_global and not ip.is_multicast


async def resolve_host(host: str, port: int) -> list[IPAddress]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos))


class Blocked(Exception):
    pass


class WebFetcher:
    """GET one URL for the model. Every hop is resolved, checked against `allow`, and connected to by that address,
    so a redirect or a second DNS answer cannot reach a private network."""

    def __init__(
        self,
        *,
        allow: Callable[[IPAddress], bool] = is_public,
        resolve: Resolve = resolve_host,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self.allow = allow
        self.resolve = resolve
        self.transport = transport
        self.timeout_s = timeout_s

    async def fetch(self, url: str, question: str | None = None) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.timeout_s):
                final, content_type, body, cut = await self._download(httpx.URL(url))
        except Blocked:
            logger.info("fetch_url blocked url=%s", url)
            return BLOCKED
        except TimeoutError:
            return {"error": f"Timed out after {self.timeout_s:g} s fetching {url}"}
        except httpx.HTTPError as exc:
            return {"error": f"Could not fetch {url}: {type(exc).__name__}: {exc}"}
        if cut and content_type == "application/pdf":
            return {"error": f"The PDF is larger than {MAX_BYTES // (1024 * 1024)} MB."}
        try:
            title, text = await asyncio.to_thread(readable, content_type, body, str(final))
        except UnreadableError as exc:
            return {"error": str(exc)}
        clipped = focus(text, question, MAX_CHARS)
        return FetchUrlResult(url=str(final), title=title, text=clipped, truncated=cut or len(clipped) < len(text)).model_dump()

    async def _download(self, url: httpx.URL) -> tuple[httpx.URL, str, bytes, bool]:
        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            timeout=self.timeout_s,
            trust_env=False,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                request = await self._pinned(client, url)
                response = await client.send(request, stream=True)
                try:
                    if response.is_redirect and "location" in response.headers:
                        url = url.join(response.headers["location"])
                        continue
                    response.raise_for_status()
                    body, cut = await _read_capped(response)
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    return url, content_type, body, cut
                finally:
                    await response.aclose()
        raise httpx.TooManyRedirects(f"More than {MAX_REDIRECTS} redirects")

    async def _pinned(self, client: httpx.AsyncClient, url: httpx.URL) -> httpx.Request:
        if url.scheme not in ("http", "https") or not url.host:
            raise Blocked
        host = url.raw_host.decode("ascii")
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            addresses = await self.resolve(host, port)
        except (OSError, ValueError):
            raise httpx.ConnectError(f"Could not resolve {host}") from None
        if not addresses or not all(self.allow(ip) for ip in addresses):
            raise Blocked
        host_header = url.netloc.decode("ascii")
        request = client.build_request("GET", url.copy_with(host=str(addresses[0])), headers={"Host": host_header})
        # TLS still verifies the certificate against the real hostname.
        request.extensions["sni_hostname"] = host
        return request


async def _read_capped(response: httpx.Response) -> tuple[bytes, bool]:
    """Read at most MAX_BYTES of decoded body, which also bounds the bytes downloaded and defuses compression bombs."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BYTES:
            return b"".join(chunks)[:MAX_BYTES], True
    return b"".join(chunks), False


def readable(content_type: str, body: bytes, url: str) -> tuple[str | None, str]:
    if content_type == "application/pdf" or (not content_type and url.lower().endswith(".pdf")):
        return None, _pdf_text(body)
    if content_type in ("text/html", "application/xhtml+xml", ""):
        return _html_text(body)
    if content_type.startswith("text/") or content_type in ("application/json", "application/xml"):
        return None, body.decode("utf-8", errors="replace")
    raise UnreadableError(f"Can't read {content_type} content from {url}.")


def _html_text(body: bytes) -> tuple[str | None, str]:
    metadata = trafilatura.extract_metadata(body)
    title = metadata.title if metadata is not None else None
    return title, trafilatura.extract(body, include_comments=False, include_tables=True) or _body_text(body)


def _body_text(body: bytes) -> str:
    """The fallback when trafilatura finds no main content: all visible text in <body>."""
    tree = trafilatura.load_html(body)
    if tree is None:
        return ""
    for node in tree.xpath("//script|//style|//noscript|//template"):
        node.drop_tree()
    root = tree.find("body")
    text = (root if root is not None else tree).text_content()
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _pdf_text(body: bytes) -> str:
    return "\n\n".join(pdf_page_texts(body)).strip()


def focus(text: str, question: str | None, limit: int) -> str:
    """Clip to `limit`. With a question, keep the paragraphs that share the most words with it, in page order."""
    if len(text) <= limit:
        return text
    if not question:
        return text[:limit]
    words = set(re.findall(r"\w{3,}", question.lower()))
    paragraphs = [part for part in re.split(r"\n\s*\n", text) if part.strip()]
    ranked = sorted(range(len(paragraphs)), key=lambda i: -len(words & set(re.findall(r"\w{3,}", paragraphs[i].lower()))))
    kept: set[int] = set()
    size = 0
    for index in ranked:
        if size + len(paragraphs[index]) + 2 > limit:
            continue
        kept.add(index)
        size += len(paragraphs[index]) + 2
    return "\n\n".join(paragraphs[i] for i in sorted(kept)) or text[:limit]


def sources_of(name: str, result: Any) -> list[dict[str, str]]:
    """The pages a web tool result was drawn from, as {title, url}."""
    if not isinstance(result, dict) or "error" in result:
        return []
    if name == "web_search":
        return list(result.get("sources") or [])
    if name == "fetch_url":
        return [{"title": result.get("title") or result["url"], "url": result["url"]}]
    return []


def with_citations(text: str, sources: list[dict[str, str]]) -> str:
    """Spec 14 §4: the answer cites its sources as Slack links. When the model cited none, add up to three."""
    if not sources or any(source["url"] in text for source in sources):
        return text
    unique = list({source["url"]: source for source in sources}.values())[:MAX_CITATIONS]
    links = ", ".join(f"<{source['url']}|{_label(source['title'])}>" for source in unique)
    return f"{text}\n\nSources: {links}"


def _label(title: str) -> str:
    return re.sub(r"[<>|]", " ", title).strip() or "source"
