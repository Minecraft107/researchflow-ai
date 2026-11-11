"""Fetch a web page safely and extract its readable text + metadata.

Security: only http(s), no private/loopback/link-local targets (SSRF), every redirect hop is
re-validated, response size is capped, and only HTML/text is parsed. The returned text is
untrusted data - callers wrap it in <source> tags and never treat it as instructions.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit, urlunsplit

import httpx
import trafilatura
from pydantic import BaseModel

from . import tls_context

log = logging.getLogger(__name__)

MAX_BYTES = 3_000_000


class FetchError(RuntimeError):
    pass


class Page(BaseModel):
    text: str
    title: str | None = None
    publisher: str | None = None
    published_date: str | None = None


def normalize_url(url: str) -> str:
    """Canonical form used for de-duplication: no fragment, no trailing slash, lowercase host."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))


def _is_public_ip(ip: str) -> bool:
    return ipaddress.ip_address(ip).is_global


async def validate_url(url: str) -> None:
    """Raise FetchError unless `url` is an http(s) URL that resolves only to public addresses."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("only http(s) URLs are allowed")
    if parts.username or parts.password:
        raise FetchError("URLs with credentials are not allowed")
    host = parts.hostname
    try:
        ips = [host] if _is_ip(host) else await asyncio.to_thread(_resolve, host, parts.port or 443)
    except socket.gaierror as exc:
        raise FetchError("host does not resolve") from exc
    # ponytail: validates at resolve time only; a DNS-rebinding attacker could still swap the
    # record before connect. Pin the resolved IP in a custom transport if that threat matters.
    if not ips or not all(_is_public_ip(ip) for ip in ips):
        raise FetchError("URL points to a non-public address")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _resolve(host: str, port: int) -> list[str]:
    return list({info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})


async def _validate_request(request: httpx.Request) -> None:
    await validate_url(str(request.url))  # runs for the first request and every redirect hop


def make_client(timeout: float, user_agent: str, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        verify=tls_context(),
        transport=transport,
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"},
        event_hooks={"request": [_validate_request]},
    )


def extract_page(html: str, url: str) -> Page:
    doc = trafilatura.bare_extraction(
        html,
        url=url,
        with_metadata=True,
        include_comments=False,
        include_tables=True,
        # Only trust explicit date markup; never let the extractor guess a date from body text.
        date_extraction_params={"extensive_search": False, "original_date": True},
    )
    text = (getattr(doc, "text", None) or "").strip() if doc else ""
    if len(text) < 200:
        raise FetchError("no readable article text found")
    return Page(text=text, title=doc.title or None, publisher=doc.sitename or None, published_date=doc.date or None)


async def fetch_page(url: str, client: httpx.AsyncClient) -> Page:
    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "").lower()
            if not any(t in content_type for t in ("html", "text/plain")):
                raise FetchError(f"unsupported content type: {content_type.split(';')[0] or 'unknown'}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    break
            encoding = response.encoding or "utf-8"
    except httpx.TimeoutException as exc:
        raise FetchError("timed out") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"request failed: {type(exc).__name__}") from exc
    html = bytes(body[:MAX_BYTES]).decode(encoding, errors="replace")
    return await asyncio.to_thread(extract_page, html, url)
