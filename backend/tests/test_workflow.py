"""End-to-end LangGraph workflow tests with fake Claude/search: the orchestration logic is real."""
import asyncio

import pytest

from app.config import Settings
from app.database import ResearchRun, SessionLocal, events_after
from app.services.research_service import ResearchService

from .fakes import BLOCKED_URL_PART, FakeLLM, FakeSearch, fake_fetch_page


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr("app.agents.researcher.fetch_page", fake_fetch_page)


def run_research(scores: list[int], depth: str, **overrides):
    settings = Settings(anthropic_api_key="test", tavily_api_key="test", cache_ttl_seconds=0, **overrides)
    made: dict = {}

    def llm_factory(s, tracker):
        made["llm"] = FakeLLM(tracker, scores)
        return made["llm"]

    def search_factory(s, provider):
        made["search"] = FakeSearch()
        return made["search"]

    service = ResearchService(settings, llm_factory=llm_factory, search_factory=search_factory)

    async def go():
        research_id = service.start("Impact of AI on healthcare", depth, "tavily")
        await service._tasks[research_id]
        return research_id

    research_id = asyncio.run(go())
    with SessionLocal() as db:
        run = db.get(ResearchRun, research_id)
    return run, made["llm"], made["search"]


def test_loops_back_to_researcher_until_quality_threshold_is_met():
    run, llm, search = run_research(scores=[60, 90], depth="deep")
    assert run.status == "completed", run.error
    assert run.iteration == 2 and run.quality_score == 90 and len(run.critiques) == 2

    # deep = 7 questions x 3 queries, then one follow-up round of 3 critic queries
    assert len(search.queries) == 21 + 3
    assert [q for q in search.queries if q.startswith("follow up 1")] and not [q for q in search.queries if q.startswith("follow up 2")]

    nodes_order = [e.agent for e in events_after(run.id, 0) if e.type == "started"]
    assert nodes_order == ["system", "planner", "researcher", "critic", "researcher", "critic", "writer", "critic"]

    report = run.report
    assert report["threshold_met"] is True
    assert [s["heading"] for s in report["sections"]] == [
        "Executive Summary", "Research Objectives", "Research Methodology", "Detailed Findings",
        "Analysis and Discussion", "Contradictory Evidence", "Limitations", "Conclusion",
    ]
    # Invalid citations [99] and [77] were removed; every remaining citation maps to a reference.
    assert report["citation_stats"]["invalid_citations_removed"] == 2
    assert "[99]" not in report["markdown"] and "[77]" not in report["markdown"]
    assert [r["number"] for r in report["references"]] == [1, 2]
    source_urls = {s["id"]: s["url"] for s in run.sources}
    assert all(source_urls[r["source_id"]] == r["url"] for r in report["references"])
    # The one claim the citation audit rejected is disclosed in Limitations.
    limitations = next(s["content"] for s in report["sections"] if s["heading"] == "Limitations")
    assert "Citation audit" in limitations and "Quality notice" not in limitations


def test_sources_are_deduplicated_and_unverifiable_evidence_is_never_citable():
    run, _, _ = run_research(scores=[95], depth="quick")
    urls = [s["url"] for s in run.sources]
    assert len(urls) == len(set(urls)) and urls.count("https://shared.example.org/page") <= 1

    blocked = [s for s in run.sources if BLOCKED_URL_PART in s["url"]]
    assert blocked and all(s["content_status"] == "snippet_only" and s["fetch_error"] == "HTTP 403" for s in blocked)
    assert len(run.failed_sources) == len(blocked)

    blocked_ids = {s["id"] for s in blocked}
    for item in run.evidence:
        expected = item["quote"].startswith("The measured") and item["source_id"] not in blocked_ids
        assert item["quote_verified"] is expected  # fabricated quotes and snippet-only sources are unverified
    cited_ids = {r["source_id"] for r in run.report["references"]}
    assert cited_ids and not cited_ids & blocked_ids
    assert "Source access" in next(s["content"] for s in run.report["sections"] if s["heading"] == "Limitations")


def test_stops_after_max_iterations_and_reports_limitations():
    run, llm, search = run_research(scores=[50], depth="deep")
    assert run.status == "completed", run.error
    assert run.iteration == 3 and len(run.critiques) == 3 and llm.critic_calls == 3  # hard cap
    assert len(search.queries) == 21 + 3 + 3  # two follow-up rounds, then the cap ends the loop
    report = run.report
    assert report["threshold_met"] is False and report["quality_score"] == 50
    limitations = next(s["content"] for s in report["sections"] if s["heading"] == "Limitations")
    assert "Quality notice" in limitations and "50/100" in limitations and "Question 2?" in limitations


def test_search_query_budget_ends_the_loop_early():
    run, llm, search = run_research(scores=[50], depth="deep", max_search_queries=22)
    assert len(search.queries) == 22  # 21 planned + the single follow-up the budget allowed
    assert run.iteration == 2 and llm.critic_calls == 2  # no third review of unchanged research
    assert run.report["threshold_met"] is False


def test_iteration_cap_is_configurable():
    run, llm, _ = run_research(scores=[50], depth="deep", max_critique_iterations=1)
    assert run.iteration == 1 and llm.critic_calls == 1


def test_metrics_are_tracked():
    run, llm, search = run_research(scores=[95], depth="quick")
    m = run.metrics
    assert m["llm_calls"] == len(llm.calls) and m["search_requests"] == len(search.queries) == 3
    assert m["input_tokens"] == 1000 * len(llm.calls) and m["output_tokens"] == 200 * len(llm.calls)
    assert m["estimated_cost_usd"] > 0 and m["elapsed_seconds"] >= 0
    assert set(m["by_agent"]) == {"planner", "researcher", "critic", "writer", "citation_validator"}


def test_failure_is_recorded_not_hidden():
    class NoResults(FakeSearch):
        async def search(self, query, max_results):
            return []

    settings = Settings(anthropic_api_key="test", tavily_api_key="test", cache_ttl_seconds=0)
    service = ResearchService(settings, llm_factory=lambda s, t: FakeLLM(t, [90]), search_factory=lambda s, p: NoResults())

    async def go():
        research_id = service.start("A topic with no results", "quick", "tavily")
        await service._tasks[research_id]
        return research_id

    research_id = asyncio.run(go())
    with SessionLocal() as db:
        run = db.get(ResearchRun, research_id)
    assert run.status == "failed" and "No verifiable evidence" in run.error and run.report is None
