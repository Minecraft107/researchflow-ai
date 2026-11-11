"""Shared, typed state that flows through the LangGraph workflow, plus per-run dependencies."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypedDict

from ..config import Settings
from ..llm import ClaudeLLM
from ..schemas import CitationCheck, Critique, Evidence, Reference, Report, ResearchPlan, Source
from ..services.cost_tracker import CostTracker
from ..tools.search import SearchProvider


class ResearchState(TypedDict, total=False):
    topic: str
    depth: str
    plan: ResearchPlan
    pending_queries: list[dict]  # [{question_id, query}] the researcher should run next
    queries_run: list[str]
    sources: list[Source]
    evidence: list[Evidence]
    page_texts: dict[str, str]  # source_id -> the text Claude actually read (for quote verification)
    failed_sources: list[dict]
    critiques: list[Critique]
    iteration: int  # number of critic reviews completed
    # report assembly
    report_title: str
    report_sections: dict[str, str]
    references: list[Reference]
    citation_stats: dict
    citation_checks: list[CitationCheck]
    report: Report
    metrics: dict


class Emit(Protocol):
    async def __call__(self, agent: str, type: str, message: str, **data: Any) -> None: ...


@dataclass
class RunContext:
    """Everything a node needs besides state. Built once per research run."""

    settings: Settings
    llm: ClaudeLLM
    search: SearchProvider
    tracker: CostTracker
    emit: Emit
    preset: dict[str, int]
