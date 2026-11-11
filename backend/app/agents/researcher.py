"""Researcher agent: parallel search -> de-duplicate -> fetch pages -> extract quoted evidence."""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date
from urllib.parse import urlsplit

from ..database import cache_get, cache_set
from ..graph.state import RunContext
from ..llm import UNTRUSTED_NOTICE
from ..schemas import Evidence, ExtractionResult, ResearchPlan, ResearchQuestion, Source
from ..tools.citation_validator import verify_quote
from ..tools.search import SearchResult, cached_search
from ..tools.webpage_extractor import FetchError, Page, fetch_page, make_client, normalize_url

log = logging.getLogger(__name__)

# ponytail: naive domain heuristic for source reliability. Good enough to bias ranking toward
# primary sources; replace with a curated/maintained list if rankings need to be defensible.
_HIGH_SUFFIXES = (".gov", ".edu", ".int", ".mil", ".ac.uk", ".gov.uk", ".europa.eu", ".gc.ca", ".gov.au")
_ACADEMIC = {
    "nature.com", "science.org", "thelancet.com", "nejm.org", "bmj.com", "jamanetwork.com", "cell.com",
    "arxiv.org", "pnas.org", "plos.org", "sciencedirect.com", "springer.com", "wiley.com", "ieee.org",
    "acm.org", "nber.org", "ssrn.com", "frontiersin.org", "mdpi.com", "biomedcentral.com",
}
_OFFICIAL = {
    "who.int", "un.org", "oecd.org", "worldbank.org", "imf.org", "ipcc.ch", "iea.org", "weforum.org",
    "unesco.org", "wto.org", "bis.org", "enisa.europa.eu",
}
_REPUTABLE = {
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "nytimes.com", "ft.com", "economist.com", "wsj.com",
    "theguardian.com", "washingtonpost.com", "bloomberg.com", "npr.org", "scientificamerican.com",
    "technologyreview.com", "hbr.org", "mckinsey.com", "pewresearch.org", "statista.com", "brookings.edu",
    "rand.org", "gartner.com", "wired.com", "theverge.com", "arstechnica.com",
}
_RANK_BONUS = {"high": 0.3, "medium": 0.15, "unknown": 0.0}


def _domain(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def classify_source(url: str) -> tuple[str, str]:
    """-> (reliability, source_type)"""
    domain = _domain(url)

    def matches(names: set[str]) -> bool:
        return any(domain == n or domain.endswith("." + n) for n in names)

    if domain.endswith(_HIGH_SUFFIXES):
        return "high", "academic" if ".edu" in domain or ".ac." in domain else "government"
    if matches(_ACADEMIC):
        return "high", "academic"
    if matches(_OFFICIAL):
        return "high", "official"
    if matches(_REPUTABLE):
        return "medium", "reputable_publication"
    return "unknown", "other"


EXTRACT_SYSTEM = f"""You are the Researcher in a three-agent research team. You are given one research question \
and the text of several web sources. Extract the evidence that helps answer the question.

For each source return:
- relevance_score: 0.0-1.0, how much this source helps answer the question.
- key_findings: up to 4 short findings relevant to the question.
- evidence: up to 5 items. Each item is one factual claim in your own words plus a quote: a passage copied \
character-for-character from that source's text (one or two sentences, under 300 characters) that supports \
the claim. Quotes are checked against the source automatically, so paraphrased or stitched quotes are \
discarded. Prefer statistics, dated facts, named studies and stated limitations or criticisms.

Use only what the sources say - never your own knowledge. If a source has nothing relevant, return empty \
lists and a low relevance score. Sources marked content="snippet" are search snippets, not full pages.

{UNTRUSTED_NOTICE}"""


def _source_block(source: Source, text: str) -> str:
    kind = "snippet" if source.content_status == "snippet_only" else "page"
    return f'<source id="{source.id}" content="{kind}">\nTitle: {source.title}\nURL: {source.url}\n\n{text}\n</source>'


async def _load_page(ctx: RunContext, result: SearchResult, client, sem: asyncio.Semaphore) -> tuple[Page | None, str | None]:
    key = normalize_url(result.url)
    cached = cache_get("page", key, ctx.settings.cache_ttl_seconds)
    if cached is not None:
        ctx.tracker.page_cache_hits += 1
        return Page(**cached), None
    async with sem:
        ctx.tracker.page_fetches += 1
        try:
            page = await fetch_page(result.url, client)
        except FetchError as exc:
            return None, str(exc)
    cache_set("page", key, page.model_dump(), ctx.settings.cache_ttl_seconds)
    return page, None


async def run_research(
    ctx: RunContext,
    plan: ResearchPlan,
    queries: list[dict],
    sources: list[Source],
    evidence: list[Evidence],
    page_texts: dict[str, str],
    failed_sources: list[dict],
) -> dict:
    """Run `queries` ([{question_id, query}]) and merge the results into the existing research.

    Returns new lists/dicts (inputs are not mutated) so a retried node starts clean.
    """
    settings = ctx.settings
    questions = {q.id: q for q in plan.questions}
    sources = [s.model_copy(deep=True) for s in sources]
    evidence, page_texts, failed_sources = list(evidence), dict(page_texts), list(failed_sources)

    # 1. Search - all queries in parallel.
    await ctx.emit("researcher", "progress", f"Searching the web: {len(queries)} queries via {ctx.search.name}", step="searching")
    results = await asyncio.gather(
        *(cached_search(ctx.search, q["query"], settings.max_results_per_query, ctx.tracker, settings.cache_ttl_seconds) for q in queries),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors and len(errors) == len(results):
        raise errors[0]  # every search failed (bad key, quota, network) - surface it, don't continue empty
    for query, result in zip(queries, results):
        if isinstance(result, BaseException):
            log.warning("search failed for %r: %s", query["query"], result)

    # 2. De-duplicate against existing sources and rank candidates per question.
    by_url = {normalize_url(s.url): s for s in sources}
    candidates: dict[str, dict[str, tuple[float, SearchResult]]] = defaultdict(dict)
    duplicates = 0
    for query, result in zip(queries, results):
        if isinstance(result, BaseException):
            continue
        qid = query["question_id"]
        for position, item in enumerate(result):
            key = normalize_url(item.url)
            if key in by_url:  # already collected: just record that it also serves this question
                duplicates += 1
                if qid not in by_url[key].question_ids:
                    by_url[key].question_ids.append(qid)
                continue
            base = item.score if item.score is not None else max(0.0, 1.0 - position * 0.1)
            rank = base + _RANK_BONUS[classify_source(item.url)[0]]
            if key in candidates[qid]:
                duplicates += 1
            if key not in candidates[qid] or rank > candidates[qid][key][0]:
                candidates[qid][key] = (rank, item)

    selected: dict[str, tuple[str, SearchResult]] = {}  # url key -> (first question id, result)
    shared: dict[str, list[str]] = defaultdict(list)
    for qid, ranked in candidates.items():
        taken = 0
        for key, (_, item) in sorted(ranked.items(), key=lambda kv: kv[1][0], reverse=True):
            if key in selected:
                shared[key].append(qid)
                duplicates += 1
            elif taken < ctx.preset["sources_per_question"]:
                selected[key] = (qid, item)
                taken += 1

    if not selected:
        await ctx.emit("researcher", "progress", "No new sources found for these queries", step="collecting", sources_count=len(sources))
        return {"sources": sources, "evidence": evidence, "page_texts": page_texts, "failed_sources": failed_sources}

    # 3. Fetch page content in parallel (search snippets alone are never treated as verified evidence).
    await ctx.emit(
        "researcher", "progress",
        f"Collecting {len(selected)} new sources ({duplicates} duplicates skipped)", step="collecting",
    )
    sem = asyncio.Semaphore(settings.fetch_concurrency)
    async with make_client(settings.fetch_timeout_seconds, settings.fetch_user_agent) as client:
        pages = await asyncio.gather(*(_load_page(ctx, item, client, sem) for _, item in selected.values()))

    today = date.today().isoformat()
    new_by_question: dict[str, list[Source]] = defaultdict(list)
    for (key, (qid, item)), (page, error) in zip(selected.items(), pages):
        if page is not None:
            status, text = "full", page.text
        elif item.raw_content and len(item.raw_content) > 200:
            status, text = "search_provider_content", item.raw_content
        else:
            status, text = "snippet_only", item.snippet
        reliability, source_type = classify_source(item.url)
        source = Source(
            id=f"S{len(sources) + 1}",
            title=(page.title if page and page.title else item.title)[:300],
            url=item.url,
            publisher=(page.publisher if page and page.publisher else _domain(item.url)) or None,
            published_date=(page.published_date if page else None) or item.published_date,
            retrieved_date=today,
            reliability=reliability,
            source_type=source_type,
            content_status=status,
            fetch_error=error,
            snippet=item.snippet[:500],
            question_ids=[qid, *shared.get(key, [])],
        )
        if error:
            failed_sources.append({"url": item.url, "title": item.title, "error": error, "fallback": status})
        sources.append(source)
        page_texts[source.id] = text[: settings.max_page_chars]
        new_by_question[qid].append(source)

    fetched = sum(1 for page, _ in pages if page is not None)
    await ctx.emit(
        "researcher", "progress",
        f"Retrieved full page content for {fetched} of {len(pages)} sources", step="extracting", sources_count=len(sources),
    )

    # 4. Extract evidence: one Claude call per research question, in parallel.
    llm_sem = asyncio.Semaphore(settings.llm_concurrency)

    async def extract(question: ResearchQuestion, batch: list[Source]) -> ExtractionResult:
        async with llm_sem:
            return await ctx.llm.structured(
                agent="researcher",
                model=settings.extraction_model,
                system=EXTRACT_SYSTEM,
                user=(
                    f"Research question ({question.id}): {question.question}\n"
                    f"Evidence wanted: {question.required_evidence}\n\n"
                    + "\n\n".join(_source_block(s, page_texts[s.id]) for s in batch)
                ),
                schema=ExtractionResult,
            )

    batches = [(questions[qid], batch) for qid, batch in new_by_question.items()]
    extractions = await asyncio.gather(*(extract(q, b) for q, b in batches), return_exceptions=True)
    failures = [e for e in extractions if isinstance(e, BaseException)]
    if failures and len(failures) == len(extractions):
        raise failures[0]

    new_evidence = 0
    for (question, batch), extraction in zip(batches, extractions):
        if isinstance(extraction, BaseException):
            log.warning("evidence extraction failed for %s: %s", question.id, extraction)
            await ctx.emit("researcher", "progress", f"Evidence extraction failed for {question.id}: {type(extraction).__name__}", step="extracting")
            continue
        batch_by_id = {s.id: s for s in batch}
        for extracted in extraction.sources:
            source = batch_by_id.get(extracted.source_id)
            if source is None:  # the model referenced a source it was not given - ignore it
                continue
            source.relevance_score = round(max(0.0, min(1.0, extracted.relevance_score)), 2)
            source.key_findings = extracted.key_findings[:4]
            for item in extracted.evidence[:5]:
                verified = source.content_status != "snippet_only" and verify_quote(item.quote, page_texts[source.id])
                evidence.append(
                    Evidence(
                        id=f"E{len(evidence) + 1}", source_id=source.id, question_id=question.id,
                        claim=item.claim, quote=item.quote, quote_verified=verified,
                    )
                )
                source.supporting_claims.append(item.claim)
                new_evidence += 1

    verified_total = sum(1 for e in evidence if e.quote_verified)
    await ctx.emit(
        "researcher", "progress",
        f"Extracted {new_evidence} evidence items ({verified_total} of {len(evidence)} total verified against source text)",
        step="extracting", sources_count=len(sources),
    )
    return {"sources": sources, "evidence": evidence, "page_texts": page_texts, "failed_sources": failed_sources}
