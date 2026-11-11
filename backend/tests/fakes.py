"""Test doubles for Claude and the search provider. Used ONLY by the test suite - the application
has no mock mode; without real API keys it refuses to start research."""
from __future__ import annotations

import re
from types import SimpleNamespace

from app.schemas import (
    CitationReview, CitationVerdict, Critique, ExtractedEvidence, ExtractionResult, FollowUpQuery,
    ReportDraft, ResearchPlan, ResearchQuestion, SourceExtraction,
)
from app.tools.search import SearchProvider, SearchResult
from app.tools.webpage_extractor import FetchError, Page

REAL_QUOTE = "The measured improvement was 42 percent in the 2024 trial."
FAKE_QUOTE = "This sentence does not appear anywhere in the page."
BLOCKED_URL_PART = "blocked"


def page_text(url: str) -> str:
    return f"Report published at {url}. {REAL_QUOTE} " + "Further background detail follows in this paragraph. " * 10


async def fake_fetch_page(url: str, client) -> Page:
    if BLOCKED_URL_PART in url:
        raise FetchError("HTTP 403")
    return Page(text=page_text(url), title=f"Title of {url}", publisher="Example Press", published_date="2024-05-01")


class FakeSearch(SearchProvider):
    name = "tavily"

    def __init__(self):
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        self.queries.append(query)
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")
        return [
            SearchResult(title=f"Gov result for {query}", url=f"https://data.example.gov/{slug}", snippet="snippet a", score=0.9),
            SearchResult(title=f"Blog result for {query}", url=f"https://{BLOCKED_URL_PART}.example.com/{slug}", snippet="snippet b", score=0.8),
            SearchResult(title="Shared result", url="https://shared.example.org/page", snippet="snippet c", score=0.5),
        ]


class FakeLLM:
    """Returns schema-valid objects; `scores` drives the critic so the loop can be tested."""

    def __init__(self, tracker, scores: list[int]):
        self.tracker = tracker
        self.scores = list(scores)
        self.calls: list[str] = []
        self.critic_calls = 0

    async def structured(self, *, agent, system, user, schema, model=None, max_tokens=0):
        self.calls.append(agent)
        self.tracker.record_llm(agent, model or "claude-opus-5-5", SimpleNamespace(input_tokens=1000, output_tokens=200))
        if schema is ResearchPlan:
            return ResearchPlan(
                objective="Test objective", subtopics=["a", "b", "c"], information_gaps=["gap"],
                questions=[
                    ResearchQuestion(id="x", subtopic=f"sub{i}", question=f"Question {i}?", priority="high",
                                     required_evidence="statistics", search_queries=[f"query {i} one", f"query {i} two", f"query {i} three"])
                    for i in range(1, 9)
                ],
            )
        if schema is ExtractionResult:
            ids = re.findall(r'<source id="(S\d+)"', user)
            return ExtractionResult(sources=[
                SourceExtraction(source_id=sid, relevance_score=0.8, key_findings=["finding"], evidence=[
                    ExtractedEvidence(claim=f"Improvement was 42 percent ({sid})", quote=REAL_QUOTE),
                    ExtractedEvidence(claim=f"Fabricated claim ({sid})", quote=FAKE_QUOTE),
                ]) for sid in ids
            ] + [SourceExtraction(source_id="S999", relevance_score=1.0, key_findings=[], evidence=[])])
        if schema is Critique:
            self.critic_calls += 1
            score = self.scores[min(self.critic_calls - 1, len(self.scores) - 1)]
            return Critique(
                quality_score=score, coverage_score=score, evidence_score=score, reliability_score=score,
                summary="review", unanswered_questions=["Q2", "Q99"], unsupported_claims=[], contradictions=[],
                missing_perspectives=["critics' view"], outdated_information=[], reliability_concerns=[],
                additional_queries=[FollowUpQuery(question_id="Q2", query=f"follow up {self.critic_calls} {i}") for i in range(3)],
            )
        if schema is ReportDraft:
            return ReportDraft(
                title="Test Report", executive_summary="Improvement was 42 percent [1]. A second point holds [2, 99].",
                research_objectives="Objectives.", research_methodology="Method.",
                detailed_findings="- Finding one is supported [1].\n- Finding two cites nothing real [77].",
                analysis_and_discussion="Analysis [2].", contradictory_evidence="None found.",
                limitations="Some limits.", conclusion="Conclusion [1].",
            )
        if schema is CitationReview:
            ids = [int(i) for i in re.findall(r'<claim id="(\d+)">', user)]
            return CitationReview(verdicts=[
                CitationVerdict(claim_id=i, verdict="unsupported" if n == 0 else "supported", reason="because") for n, i in enumerate(ids)
            ])
        raise AssertionError(f"unexpected schema {schema}")
