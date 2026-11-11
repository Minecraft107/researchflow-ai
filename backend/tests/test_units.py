"""Unit tests for the pieces with real logic: citations, URL safety, search parsing, costs, validation."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.agents.researcher import classify_source
from app.schemas import Report, ReportSection, Source, StartResearchRequest
from app.services import report_service
from app.services.cost_tracker import CostTracker
from app.tools.citation_validator import extract_cited_claims, normalize_citations, verify_quote
from app.tools.search import BraveSearch, SearchError, TavilySearch, cached_search
from app.tools.webpage_extractor import FetchError, extract_page, fetch_page, make_client, normalize_url, validate_url


def _source(n: int) -> Source:
    return Source(id=f"S{n}", title=f"Source {n}", url=f"https://example.org/{n}", retrieved_date="2026-01-01")


# ---------------------------------------------------------------- citations
def test_verify_quote_normalises_whitespace_quotes_and_ellipses():
    page = "Researchers found that  “AI triage” cut waiting\ntimes by 30% in 2023. Costs rose slightly afterwards."
    assert verify_quote('"AI triage" cut waiting times by 30% in 2023', page)
    assert verify_quote("Researchers found that ... Costs rose slightly afterwards.", page)
    assert not verify_quote("AI triage cut waiting times by 50% in 2023", page)
    assert not verify_quote("short", page)  # too short to be meaningful evidence


def test_normalize_citations_removes_invalid_and_renumbers_by_first_appearance():
    sources = [_source(1), _source(2), _source(3)]
    sections = {"a": "First [3]. Second [1, 99]. Bogus [42].", "b": "Again [3] and [1]."}
    cleaned, refs, stats = normalize_citations(sections, sources)
    assert cleaned["a"] == "First [1]. Second [2]. Bogus."
    assert cleaned["b"] == "Again [1] and [2]."
    assert [(r.number, r.source_id) for r in refs] == [(1, "S3"), (2, "S1")]  # S2 never cited -> not a reference
    assert stats == {"total_citations": 4, "invalid_citations_removed": 2}


def test_extract_cited_claims_only_returns_sentences_with_citations():
    claims = extract_cited_claims({"a": "- Rates fell 5% [1]. This is commentary. Both agree [1, 2].", "b": "No citation here."})
    assert [(c["claim"], c["citations"]) for c in claims] == [("Rates fell 5% [1].", [1]), ("Both agree [1, 2].", [1, 2])]


# ---------------------------------------------------------------- URL safety (SSRF)
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/admin", "http://localhost:8000/", "http://10.0.0.5/", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data", "http://[::1]/", "file:///etc/passwd", "ftp://example.com/x",
    "http://user:pw@example.com/", "javascript:alert(1)",
])
def test_validate_url_rejects_unsafe_targets(url):
    with pytest.raises(FetchError):
        asyncio.run(validate_url(url))


def test_validate_url_accepts_public_ip_and_normalize_url_dedupes():
    asyncio.run(validate_url("https://8.8.8.8/"))
    assert normalize_url("HTTPS://Example.org/Path/#frag") == normalize_url("https://example.org/Path")


def test_redirect_to_a_private_address_is_blocked_and_bad_content_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
        if request.url.path == "/pdf":
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
        return httpx.Response(404)

    async def fetch(path: str):
        async with make_client(5, "test-agent", transport=httpx.MockTransport(handler)) as client:
            return await fetch_page(f"https://8.8.8.8{path}", client)

    for path, message in (("/redirect", "non-public"), ("/pdf", "unsupported content type"), ("/missing", "HTTP 404")):
        with pytest.raises(FetchError, match=message):
            asyncio.run(fetch(path))


def test_extract_page_reads_text_and_only_explicit_dates():
    body = "<p>" + "Artificial intelligence improved diagnostic accuracy in the trial. " * 12 + "</p>"
    html = f'<html><head><title>AI Trial</title><meta property="article:published_time" content="2024-03-02"></head><body><article>{body}</article></body></html>'
    page = extract_page(html, "https://example.org/a")
    assert "diagnostic accuracy" in page.text and page.published_date == "2024-03-02"
    undated = extract_page(f"<html><body><article>{body}</article></body></html>", "https://example.org/b")
    assert undated.published_date is None  # never invented
    with pytest.raises(FetchError):
        extract_page("<html><body><p>too short</p></body></html>", "https://example.org/c")


def test_classify_source():
    assert classify_source("https://www.cdc.gov/x") == ("high", "government")
    assert classify_source("https://www.nature.com/articles/1")[0] == "high"
    assert classify_source("https://www.reuters.com/x")[0] == "medium"
    assert classify_source("https://notnature.com/x")[0] == "unknown"  # suffix match must respect the dot


# ---------------------------------------------------------------- search providers
def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_tavily_request_and_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.tavily.com/search" and request.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"results": [{"title": "T", "url": "https://a.org", "content": "c", "score": 0.7, "raw_content": "raw"}, {"title": "no url"}]})

    results = asyncio.run(TavilySearch("k", _client(handler)).search("q", 5))
    assert len(results) == 1 and results[0].score == 0.7 and results[0].raw_content == "raw"


def test_brave_request_and_parsing_and_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-subscription-token"] == "k" and request.url.params["q"] == "q"
        return httpx.Response(200, json={"web": {"results": [{"title": "T", "url": "https://b.org", "description": "d", "page_age": "2024-01-01"}]}})

    results = asyncio.run(BraveSearch("k", _client(handler)).search("q", 5))
    assert results[0].snippet == "d" and results[0].published_date == "2024-01-01"
    invalid = httpx.Response(422, json={"error": {"code": "SUBSCRIPTION_TOKEN_INVALID"}})  # what Brave really returns
    for response in (httpx.Response(401), invalid):
        with pytest.raises(SearchError, match="API key"):
            asyncio.run(BraveSearch("k", _client(lambda r, resp=response: resp)).search("q", 5))


def test_cached_search_only_bills_the_first_request():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": [{"title": "T", "url": "https://a.org"}]})

    tracker = CostTracker({}, "tavily")
    provider = TavilySearch("k", _client(handler))
    for _ in range(2):
        assert len(asyncio.run(cached_search(provider, "cache me unique-1", 5, tracker, ttl=3600))) == 1
    assert (calls, tracker.search_requests, tracker.search_cache_hits) == (1, 1, 1)


# ---------------------------------------------------------------- cost tracking
def test_cost_tracker_uses_configured_prices_and_flags_unpriced_models():
    pricing = {"models": {"m1": {"input": 2.0, "output": 10.0}}, "search": {"tavily": 0.01}}
    tracker = CostTracker(pricing, "tavily")
    tracker.record_llm("planner", "m1", SimpleNamespace(input_tokens=1_000_000, output_tokens=100_000))
    tracker.record_llm("critic", "mystery-model", SimpleNamespace(input_tokens=500, output_tokens=500))
    tracker.search_requests = 3
    snap = tracker.snapshot()
    assert snap["llm_cost_usd"] == 3.0 and snap["search_cost_usd"] == 0.03 and snap["estimated_cost_usd"] == 3.03
    assert snap["unpriced"] == ["mystery-model"] and snap["llm_calls"] == 2 and snap["input_tokens"] == 1_000_500
    assert snap["by_agent"]["planner"] == {"calls": 1, "input_tokens": 1_000_000, "output_tokens": 100_000}


# ---------------------------------------------------------------- input validation
def test_start_request_validation():
    assert StartResearchRequest(topic="  Impact of\n AI   on healthcare ").topic == "Impact of AI on healthcare"
    for bad in ({"topic": "hey"}, {"topic": "x" * 501}, {"topic": "valid topic", "depth": "extreme"}, {"topic": "valid topic", "provider": "google"}):
        with pytest.raises(ValidationError):
            StartResearchRequest(**bad)


# ---------------------------------------------------------------- exports
def test_exports_produce_real_files_and_neutralise_html():
    report = Report(
        title="Tést Report – Ünïcode",
        sections=[ReportSection(heading="Executive Summary", content="### Sub\n\n- **Bold** point [1]\n- <img src=http://127.0.0.1/x> ![i](http://127.0.0.1/y)\n\nPlain text.")],
        references=[{"number": 1, "source_id": "S1", "title": "A & B <source>", "url": "https://example.org/a?x=1&y=2", "retrieved_date": "2026-01-01"}],
        markdown="# x",
    )
    assert "<img" not in report_service._safe_html(report.sections[0].content)
    pdf = report_service.to_pdf(report)
    assert pdf.startswith(b"%PDF") and len(pdf) > 1000
    docx = report_service.to_docx(report)
    assert docx.startswith(b"PK")
    md = report_service.build_markdown(report.title, report.sections, report.references)
    assert "## References" in md and "1. [A & B <source>](https://example.org/a?x=1&y=2)" in md
