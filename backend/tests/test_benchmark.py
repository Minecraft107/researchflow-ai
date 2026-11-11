"""The benchmark runner itself, exercised with fake Claude/search so its measurements can be checked."""
import asyncio
import random
import re

from app.config import Settings
from app.schemas import Critique, Evidence, UnsupportedClaim
from app.services.research_service import ResearchService
from benchmarks.run_benchmark import (
    ClaimJudgement, JudgeResult, PlanScore, citation_integrity, render_report, run_all, seed_unsupported_claims,
)

from .fakes import FakeLLM, FakeSearch, fake_fetch_page


class FakeJudge(FakeLLM):
    """Judges every claim 'supported' except the first, and flags the newest evidence ids (the seeded ones)."""

    async def structured(self, *, agent, system, user, schema, model=None, max_tokens=0):
        if schema is JudgeResult:
            ids = [int(i) for i in re.findall(r'<claim id="(\d+)">', user)]
            return JudgeResult(judgements=[ClaimJudgement(claim_id=i, verdict="unsupported" if n == 0 else "supported", reason="r") for n, i in enumerate(ids)])
        if schema is PlanScore:
            return PlanScore(coverage=80, specificity=70, query_quality=90, overall=80, comment="ok")
        if schema is Critique:
            ids = sorted((int(i) for i in re.findall(r'<evidence id="E(\d+)"', user)), reverse=True)[:2]
            base = await super().structured(agent=agent, system=system, user=user, schema=schema)
            base.unsupported_claims = [UnsupportedClaim(evidence_id=f"E{i}", claim="c", reason="quote does not support it") for i in ids]
            return base
        return await super().structured(agent=agent, system=system, user=user, schema=schema)


def test_citation_integrity_detects_broken_mappings():
    report = {
        "sections": [{"heading": "a", "content": "One [1]. Two [2, 3]."}],
        "references": [{"number": 1, "source_id": "S1", "url": "https://a.org"}, {"number": 2, "source_id": "S9", "url": "https://fake.org"}],
    }
    result = citation_integrity(report, [{"id": "S1", "url": "https://a.org"}])
    assert result["citations_mapped_to_reference"] == round(2 / 3, 3)  # [3] has no reference
    assert result["references_that_are_collected_sources"] == 0.5  # S9 was never collected


def test_seeded_claims_pair_a_real_quote_with_a_claim_from_elsewhere():
    evidence = [Evidence(id=f"E{i}", source_id=f"S{i}", question_id=f"Q{i}", claim=f"claim {i}", quote=f"quote number {i}", quote_verified=True) for i in range(1, 5)]
    combined, seeded_ids = seed_unsupported_claims(evidence, 3, random.Random(1))
    assert seeded_ids == ["E5", "E6", "E7"] and len(combined) == 7
    originals = {e.quote: e.claim for e in evidence}
    for item in combined[4:]:
        assert originals[item.quote] != item.claim  # the quote belongs to a different claim


def test_benchmark_runs_measures_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agents.researcher.fetch_page", fake_fetch_page)
    settings = Settings(anthropic_api_key="t", tavily_api_key="t", cache_ttl_seconds=0, fetch_timeout_seconds=2)

    class FlakySearch(FakeSearch):
        async def search(self, query, max_results):
            return [] if "fusion" in query else await super().search(query, max_results)

    class TopicLLM(FakeLLM):  # make the plan's queries depend on the topic so one question can fail
        async def structured(self, **kw):
            result = await super().structured(**kw)
            if kw["schema"].__name__ == "ResearchPlan" and "fusion" in kw["user"]:
                for q in result.questions:
                    q.search_queries = [f"fusion {s}" for s in q.search_queries]
            return result

    service = ResearchService(settings, llm_factory=lambda s, t: TopicLLM(t, [90]), search_factory=lambda s, p: FlakySearch())
    questions = [
        {"id": "ok-1", "domain": "Test", "question": "How effective is the tested intervention?"},
        {"id": "bad-1", "domain": "Test", "question": "What is the status of nuclear fusion research?"},
    ]
    result = asyncio.run(run_all(questions, service, settings, "tavily", "quick", tmp_path, judge_factory=lambda s, t: FakeJudge(t, [90]), judge_sample=4))

    ok, bad = result["rows"]
    assert ok["status"] == "completed" and bad["status"] == "failed" and "No verifiable evidence" in bad["error"]
    assert ok["citations"]["citations_mapped_to_reference"] == 1.0 and ok["citations"]["references_that_are_collected_sources"] == 1.0
    assert ok["citations"]["urls_checked"] == 2 and ok["citations"]["urls_resolve"] == 0.0  # example.gov hosts do not exist
    assert ok["citations"]["judged_claims"] == 4 and ok["citations"]["judge_supported"] == 0.75
    assert ok["quality"]["question_coverage"] == 1.0 and ok["quality"]["evidence_quote_verified"] == round(4 / 14, 3)  # 3 gov + 1 shared page verified, of 7 sources x 2 items
    assert ok["agents"]["planner_overall"] == 80
    assert ok["agents"]["critic_seeded"] == 3 and ok["agents"]["critic_detected"] == 2 and ok["agents"]["critic_detection_rate"] == round(2 / 3, 3)
    assert ok["performance"]["llm_calls"] > 0 and ok["evaluation_cost_usd"] > 0

    report = (tmp_path / "BENCHMARK_REPORT.md").read_text(encoding="utf-8")
    assert "**1 completed, 1 failed**" in report and "## Failures" in report and "bad-1" in report
    assert "| Critic - seeded unsupported claims detected | 66.7% |" in report
    assert list(tmp_path.glob("benchmark_*.json"))
    assert "No question completed" in render_report({**result, "rows": [bad]})
