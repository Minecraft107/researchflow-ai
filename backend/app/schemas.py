"""Pydantic schemas: agent outputs (Claude structured outputs), domain objects, API payloads."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .config import Depth, ProviderName

# ---------------------------------------------------------------- Planner


class ResearchQuestion(BaseModel):
    id: str = Field(description="Short id such as Q1, Q2")
    subtopic: str
    question: str
    priority: Literal["high", "medium", "low"]
    required_evidence: str = Field(description="What kind of evidence would answer this question")
    search_queries: list[str] = Field(description="Web search queries, most useful first")


class ResearchPlan(BaseModel):
    objective: str
    subtopics: list[str]
    questions: list[ResearchQuestion]
    information_gaps: list[str] = Field(description="Things that may be hard to find or are likely contested")


# ---------------------------------------------------------------- Researcher


class ExtractedEvidence(BaseModel):
    claim: str = Field(description="One factual claim, stated in your own words")
    quote: str = Field(description="Verbatim passage copied from the source text that supports the claim")


class SourceExtraction(BaseModel):
    source_id: str
    relevance_score: float = Field(description="0.0-1.0: how well this source answers the research question")
    key_findings: list[str]
    evidence: list[ExtractedEvidence]


class ExtractionResult(BaseModel):
    sources: list[SourceExtraction]


class Evidence(BaseModel):
    id: str
    source_id: str
    question_id: str
    claim: str
    quote: str
    quote_verified: bool  # True only if the quote was found in the fetched page text


class Source(BaseModel):
    id: str
    title: str
    url: str
    publisher: str | None = None
    published_date: str | None = None  # only ever set from page metadata / the search API, never guessed
    retrieved_date: str
    relevance_score: float = 0.0
    reliability: Literal["high", "medium", "unknown"] = "unknown"
    source_type: str = "other"
    content_status: Literal["full", "search_provider_content", "snippet_only"] = "snippet_only"
    fetch_error: str | None = None
    snippet: str = ""
    question_ids: list[str] = []
    key_findings: list[str] = []
    supporting_claims: list[str] = []


# ---------------------------------------------------------------- Critic


class UnsupportedClaim(BaseModel):
    evidence_id: str | None = Field(description="Id of the evidence item (e.g. E7), or null if not tied to one")
    claim: str
    reason: str


class FollowUpQuery(BaseModel):
    question_id: str = Field(description="Id of the research question this query serves")
    query: str


class Critique(BaseModel):
    quality_score: int = Field(description="Overall research quality, 0-100")
    coverage_score: int = Field(description="0-100: are all research questions answered")
    evidence_score: int = Field(description="0-100: are claims backed by quotes that really support them")
    reliability_score: int = Field(description="0-100: are sources reliable and current")
    summary: str
    unanswered_questions: list[str] = Field(description="Ids of research questions not adequately answered")
    unsupported_claims: list[UnsupportedClaim]
    contradictions: list[str]
    missing_perspectives: list[str]
    outdated_information: list[str]
    reliability_concerns: list[str]
    additional_queries: list[FollowUpQuery] = Field(description="New search queries that would fix the gaps")

    @field_validator("quality_score", "coverage_score", "evidence_score", "reliability_score")
    @classmethod
    def clamp(cls, v: int) -> int:
        return max(0, min(100, v))


# ---------------------------------------------------------------- Report


class ReportDraft(BaseModel):
    title: str
    executive_summary: str
    research_objectives: str
    research_methodology: str
    detailed_findings: str
    analysis_and_discussion: str
    contradictory_evidence: str
    limitations: str
    conclusion: str


class CitationVerdict(BaseModel):
    claim_id: int
    verdict: Literal["supported", "partially_supported", "unsupported"]
    reason: str


class CitationReview(BaseModel):
    verdicts: list[CitationVerdict]


class CitationCheck(BaseModel):
    claim: str
    citations: list[int]
    verdict: Literal["supported", "partially_supported", "unsupported", "not_checked"]
    reason: str = ""


class Reference(BaseModel):
    number: int
    source_id: str
    title: str
    url: str
    publisher: str | None = None
    published_date: str | None = None
    retrieved_date: str


class ReportSection(BaseModel):
    heading: str
    content: str


class Report(BaseModel):
    title: str
    sections: list[ReportSection]
    references: list[Reference]
    markdown: str
    citation_checks: list[CitationCheck] = []
    citation_stats: dict = {}
    quality_score: int | None = None
    threshold_met: bool = False


# ---------------------------------------------------------------- API


class StartResearchRequest(BaseModel):
    topic: str = Field(min_length=5, max_length=500)
    depth: Depth = "standard"
    provider: ProviderName | None = None

    @field_validator("topic")
    @classmethod
    def clean_topic(cls, v: str) -> str:
        v = " ".join(v.split())  # collapses whitespace and drops newlines/tabs
        if any(ord(ch) < 32 for ch in v):
            raise ValueError("topic contains control characters")
        if len(v) < 5:
            raise ValueError("topic is too short")
        return v


class StartResearchResponse(BaseModel):
    research_id: str
    status: str
