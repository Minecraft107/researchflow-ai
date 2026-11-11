"""Benchmark: run the real pipeline on the 20 questions and measure the results.

    python -m benchmarks.run_benchmark                 # all 20 questions, quick depth
    python -m benchmarks.run_benchmark --limit 2       # cheap smoke run
    python -m benchmarks.run_benchmark --depth standard --ids ai-1 health-1

Every number in the report is measured from an actual run. Nothing is estimated or filled in:
a question that fails is reported as failed, and metrics that could not be measured are "n/a".
Requires real API keys (it exits with an explanation if they are missing) and spends real money.

What is measured
  A. Citation accuracy   - citations map to references, references are real collected sources,
                           URLs resolve (live HTTP), claims supported (pipeline audit + an
                           independent judge that reads the source page text, not the extracted evidence)
  B. Research quality    - question coverage, source reliability, completeness, factual consistency
  C. Performance         - time, Claude calls, tokens, cost per report
  D. Agent performance   - planner (LLM-judged rubric), researcher (source quality), critic
                           (does it catch deliberately mismatched claim/quote pairs seeded into the evidence?)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, Field

from app.agents.critic import critique
from app.config import Settings, configure_logging, get_settings
from app.database import init_db
from app.graph.state import RunContext
from app.llm import UNTRUSTED_NOTICE, ClaudeLLM
from app.schemas import Evidence
from app.services.cost_tracker import CostTracker
from app.services.research_service import ResearchService
from app.tools.citation_validator import CITATION_RE, extract_cited_claims
from app.tools.webpage_extractor import make_client

HERE = Path(__file__).parent
SEEDED_CLAIMS = 3


# ---------------------------------------------------------------- judge schemas
class ClaimJudgement(BaseModel):
    claim_id: int
    verdict: Literal["supported", "partially_supported", "unsupported"]
    reason: str


class JudgeResult(BaseModel):
    judgements: list[ClaimJudgement]


class PlanScore(BaseModel):
    coverage: int = Field(description="0-100: do the questions cover the topic, including criticisms and limits")
    specificity: int = Field(description="0-100: are questions focused and answerable with evidence")
    query_quality: int = Field(description="0-100: would the search queries find authoritative sources")
    overall: int = Field(description="0-100 overall plan quality")
    comment: str


JUDGE_SYSTEM = f"""You are an independent fact-checker. For each numbered claim taken from a research report you are \
given the text of the web page(s) it cites. Decide from the page text alone whether the claim is supported, \
partially_supported (partly backed, or overstated) or unsupported. Give a one-sentence reason.

{UNTRUSTED_NOTICE}"""

PLAN_SYSTEM = """You review research plans. Score the plan for the given topic on coverage, specificity and search \
query quality (each 0-100), then an overall 0-100. Be strict: 80+ means a professional researcher would accept the \
plan as is."""


# ---------------------------------------------------------------- pure measurements
def ratio(part: float, whole: float) -> float | None:
    return round(part / whole, 3) if whole else None


def citation_integrity(report: dict, sources: list[dict]) -> dict:
    """A. Are citations correctly mapped to references, and is every reference a real collected source?"""
    body = "\n".join(s["content"] for s in report["sections"])
    cited = [int(n) for m in CITATION_RE.finditer(body) for n in re.findall(r"\d+", m.group(1))]
    numbers = {r["number"] for r in report["references"]}
    source_urls = {s["id"]: s["url"] for s in sources}
    real = sum(1 for r in report["references"] if source_urls.get(r["source_id"]) == r["url"])
    return {
        "citations": len(cited),
        "citations_mapped_to_reference": ratio(sum(1 for n in cited if n in numbers), len(cited)),
        "references": len(report["references"]),
        "references_that_are_collected_sources": ratio(real, len(report["references"])),
        "references_never_cited": len(numbers - set(cited)),
    }


def research_quality(state: dict[str, Any]) -> dict:
    """B + D(researcher): coverage, reliability and source quality, computed from the run itself."""
    plan, sources, evidence, report = state["plan"], state["sources"], state["evidence"], state["report"]
    verified = [e for e in evidence if e.quote_verified]
    covered = {e.question_id for e in verified}
    cited_ids = {r.source_id for r in report.references}
    cited = [s for s in sources if s.id in cited_ids]
    last = state["critiques"][-1] if state.get("critiques") else None
    return {
        "question_coverage": ratio(sum(1 for q in plan.questions if q.id in covered), len(plan.questions)),
        "critic_score": last.quality_score if last else None,
        "threshold_met": report.threshold_met,
        "critic_iterations": state.get("iteration", 0),
        "sections_complete": ratio(sum(1 for s in report.sections if s.content.strip()), len(report.sections)),
        "cited_sources_high_reliability": ratio(sum(1 for s in cited if s.reliability == "high"), len(cited)),
        "cited_sources_high_or_medium_reliability": ratio(sum(1 for s in cited if s.reliability != "unknown"), len(cited)),
        "sources_collected": len(sources),
        "sources_full_page_read": ratio(sum(1 for s in sources if s.content_status == "full"), len(sources)),
        "sources_mean_relevance": round(statistics.mean(s.relevance_score for s in sources), 3) if sources else None,
        "evidence_items": len(evidence),
        "evidence_quote_verified": ratio(len(verified), len(evidence)),
    }


def seed_unsupported_claims(evidence: list[Evidence], count: int, rng: random.Random) -> tuple[list[Evidence], list[str]]:
    """Critic probe: add evidence whose quote is real but whose claim was taken from a different
    source and question, so the quote cannot support it. Returns (evidence + seeded, seeded ids)."""
    verified = [e for e in evidence if e.quote_verified]
    seeded: list[Evidence] = []
    for host in rng.sample(verified, min(count, len(verified))):
        donors = [e for e in verified if e.source_id != host.source_id and e.question_id != host.question_id]
        if not donors:
            continue
        seeded.append(
            Evidence(
                id=f"E{len(evidence) + len(seeded) + 1}", source_id=host.source_id, question_id=host.question_id,
                claim=rng.choice(donors).claim, quote=host.quote, quote_verified=True,
            )
        )
    return evidence + seeded, [e.id for e in seeded]


# ---------------------------------------------------------------- measurements that call out
async def urls_resolve(report: dict, client: httpx.AsyncClient) -> dict:
    """A. Does each cited URL resolve right now? Bot-blocked responses are counted separately, not as OK."""

    async def check(url: str) -> str:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code < 400:
                    return "ok"
                return "blocked" if response.status_code in (401, 403, 429) else "broken"
        except Exception:  # noqa: BLE001 - any failure means the URL did not resolve for us
            return "broken"

    outcomes = await asyncio.gather(*(check(r["url"]) for r in report["references"]))
    total = len(outcomes)
    return {
        "urls_checked": total,
        "urls_resolve": ratio(outcomes.count("ok"), total),
        "urls_blocked_to_bots": outcomes.count("blocked"),
        "urls_broken": outcomes.count("broken"),
    }


async def judge_claims(llm: Any, state: dict[str, Any], sample_size: int, rng: random.Random) -> dict:
    """A/B. Independent check: sampled report claims vs. the cited pages' own text."""
    report = state["report"]
    claims = extract_cited_claims({s.heading: s.content for s in report.sections})
    sample = rng.sample(claims, min(sample_size, len(claims)))
    if not sample:
        return {"judged_claims": 0, "judge_supported": None}
    source_of = {r.number: r.source_id for r in report.references}
    blocks = []
    for claim in sample:
        pages = "\n".join(
            f'<source citation="{n}">\n{state["page_texts"].get(source_of.get(n, ""), "(page text unavailable)")}\n</source>'
            for n in claim["citations"]
        )
        blocks.append(f'<claim id="{claim["id"]}">\n{claim["claim"]}\n{pages}\n</claim>')
    result = await llm.structured(agent="benchmark_judge", system=JUDGE_SYSTEM, user="\n\n".join(blocks), schema=JudgeResult)
    verdicts = [j.verdict for j in result.judgements if j.claim_id in {c["id"] for c in sample}]
    return {
        "judged_claims": len(verdicts),
        "judge_supported": ratio(verdicts.count("supported"), len(verdicts)),
        "judge_partially_supported": verdicts.count("partially_supported"),
        "judge_unsupported": verdicts.count("unsupported"),
    }


async def score_plan(llm: Any, topic: str, state: dict[str, Any]) -> dict:
    """D. Planner quality, judged against a rubric."""
    score = await llm.structured(
        agent="benchmark_judge", system=PLAN_SYSTEM, schema=PlanScore,
        user=f"<topic>{topic}</topic>\n\n{state['plan'].model_dump_json(indent=1)}",
    )
    return {"planner_overall": score.overall, "planner_coverage": score.coverage, "planner_specificity": score.specificity, "planner_query_quality": score.query_quality}


async def probe_critic(ctx: RunContext, topic: str, state: dict[str, Any], rng: random.Random) -> dict:
    """D. Critic's ability to detect unsupported claims: seed known-bad evidence and see what it flags."""
    evidence, seeded_ids = seed_unsupported_claims(state["evidence"], SEEDED_CLAIMS, rng)
    if not seeded_ids:
        return {"critic_seeded": 0, "critic_detection_rate": None}
    result = await critique(ctx, topic, state["plan"], state["sources"], evidence, state.get("queries_run", []), 1, 0)
    flagged = {c.evidence_id for c in result.unsupported_claims if c.evidence_id}
    return {
        "critic_seeded": len(seeded_ids),
        "critic_detected": len(flagged & set(seeded_ids)),
        "critic_detection_rate": ratio(len(flagged & set(seeded_ids)), len(seeded_ids)),
        "critic_other_claims_flagged": len(flagged - set(seeded_ids)),
        "critic_score_with_seeded_claims": result.quality_score,
    }


# ---------------------------------------------------------------- runner
async def evaluate(question: dict, service: ResearchService, settings: Settings, provider: str, depth: str,
                   judge_factory: Callable[[Settings, CostTracker], Any], judge_sample: int) -> dict:
    row: dict[str, Any] = {"id": question["id"], "domain": question["domain"], "question": question["question"]}
    research_id, state = await service.run_and_wait(question["question"], depth, provider)
    row["research_id"] = research_id
    if state is None:
        from app.database import ResearchRun, SessionLocal

        with SessionLocal() as db:
            row.update(status="failed", error=db.get(ResearchRun, research_id).error)
        return row

    report = state["report"].model_dump()
    sources = [s.model_dump() for s in state["sources"]]
    metrics = state["metrics"]
    row.update(status="completed", title=report["title"])
    row["performance"] = {k: metrics[k] for k in ("elapsed_seconds", "llm_calls", "input_tokens", "output_tokens", "total_tokens", "search_requests", "page_fetches", "estimated_cost_usd", "unpriced")}
    row["citations"] = {**citation_integrity(report, sources), "pipeline_audit_accuracy": report["citation_stats"].get("citation_accuracy"), "invalid_citations_removed": report["citation_stats"].get("invalid_citations_removed")}
    row["quality"] = research_quality(state)

    # Evaluation overhead is tracked apart from the report's own cost.
    overhead = CostTracker(settings.load_pricing(), provider)
    judge = judge_factory(settings, overhead)
    rng = random.Random(question["id"])  # deterministic sampling per question
    async with make_client(settings.fetch_timeout_seconds, settings.fetch_user_agent) as client:
        row["citations"].update(await urls_resolve(report, client))
    ctx = RunContext(settings=settings, llm=judge, search=None, tracker=overhead, emit=_silent, preset=settings.preset(depth))  # type: ignore[arg-type]
    for name, measure in (
        ("citations", lambda: judge_claims(judge, state, judge_sample, rng)),
        ("agents", lambda: score_plan(judge, question["question"], state)),
        ("agents", lambda: probe_critic(ctx, question["question"], state, rng)),
    ):
        try:
            row.setdefault(name, {}).update(await measure())
        except Exception as exc:  # noqa: BLE001 - a failed measurement is recorded, never invented
            row.setdefault("evaluation_errors", []).append(f"{type(exc).__name__}: {exc}")
    row["evaluation_cost_usd"] = overhead.snapshot()["estimated_cost_usd"]
    return row


async def _silent(agent: str, type: str, message: str, **data: Any) -> None:
    return None


async def run_all(questions: list[dict], service: ResearchService, settings: Settings, provider: str, depth: str,
                  out_dir: Path, judge_factory: Callable[[Settings, CostTracker], Any] = ClaudeLLM, judge_sample: int = 5) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    result = {
        "started_at": datetime.now(timezone.utc).isoformat(), "depth": depth, "provider": provider,
        "model": settings.claude_model, "extraction_model": settings.extraction_model,
        "questions_requested": len(questions), "rows": [],
    }
    for index, question in enumerate(questions, start=1):
        print(f"[{index}/{len(questions)}] {question['id']}: {question['question']}", flush=True)
        try:
            row = await evaluate(question, service, settings, provider, depth, judge_factory, judge_sample)
        except Exception as exc:  # noqa: BLE001 - one broken question must not lose the others
            row = {"id": question["id"], "domain": question["domain"], "question": question["question"], "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        print(f"    -> {row['status']}" + (f" ({row.get('error')})" if row["status"] == "failed" else ""), flush=True)
        result["rows"].append(row)
        # Written after every question so an interrupted run still leaves real, partial results.
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        (out_dir / f"benchmark_{stamp}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        (out_dir / "BENCHMARK_REPORT.md").write_text(render_report(result), encoding="utf-8")
    return result


# ---------------------------------------------------------------- report
def _mean(rows: list[dict], group: str, key: str) -> float | None:
    values = [r[group][key] for r in rows if isinstance(r.get(group, {}).get(key), (int, float)) and not isinstance(r[group][key], bool)]
    return round(statistics.mean(values), 3) if values else None


def _fmt(value: Any, kind: str = "") -> str:
    if value is None:
        return "n/a"
    if kind == "pct":
        return f"{value * 100:.1f}%"
    if kind == "usd":
        return f"${value:.3f}"
    return f"{value:,.1f}" if isinstance(value, float) else str(value)


def render_report(result: dict) -> str:
    rows = result["rows"]
    done = [r for r in rows if r["status"] == "completed"]
    failed = [r for r in rows if r["status"] == "failed"]
    lines = [
        "# ResearchFlow AI - Benchmark Report", "",
        f"- Run: {result['started_at']} to {result.get('finished_at', 'in progress')}",
        f"- Depth: {result['depth']} | Search: {result['provider']} | Model: {result['model']} | Extraction model: {result['extraction_model']}",
        f"- Questions: {result['questions_requested']} requested, {len(rows)} attempted, **{len(done)} completed, {len(failed)} failed**",
        "", "All figures are measured from these runs. `n/a` means the metric could not be measured.", "",
    ]
    if not done:
        lines += ["No question completed, so there are no aggregate results.", ""]
    else:
        def table(title: str, items: list[tuple[str, str, str, str]]) -> None:
            lines.extend([f"## {title}", "", "| Metric | Mean over completed runs |", "|---|---|"])
            lines.extend(f"| {label} | {_fmt(_mean(done, group, key), kind)} |" for label, group, key, kind in items)
            lines.append("")

        table("A. Citation accuracy", [
            ("Citations that map to a reference", "citations", "citations_mapped_to_reference", "pct"),
            ("References that are real collected sources", "citations", "references_that_are_collected_sources", "pct"),
            ("Cited URLs that resolve (live HTTP < 400)", "citations", "urls_resolve", "pct"),
            ("Cited URLs blocking bots (401/403/429), per report", "citations", "urls_blocked_to_bots", ""),
            ("Claims supported - independent judge reading the source page", "citations", "judge_supported", "pct"),
            ("Claims supported - pipeline's own citation audit", "citations", "pipeline_audit_accuracy", "pct"),
            ("Invalid citation markers removed, per report", "citations", "invalid_citations_removed", ""),
        ])
        table("B. Research quality", [
            ("Research questions with verified evidence (coverage)", "quality", "question_coverage", "pct"),
            ("Critic quality score (0-100)", "quality", "critic_score", ""),
            ("Report sections present (completeness)", "quality", "sections_complete", "pct"),
            ("Cited sources rated high reliability", "quality", "cited_sources_high_reliability", "pct"),
            ("Cited sources rated high or medium reliability", "quality", "cited_sources_high_or_medium_reliability", "pct"),
            ("Evidence quotes found verbatim in the source (factual consistency)", "quality", "evidence_quote_verified", "pct"),
        ])
        met = sum(1 for r in done if r["quality"]["threshold_met"])
        lines += [f"Reports meeting the critic's quality threshold: **{met} of {len(done)}**.", ""]
        table("C. Performance (per report)", [
            ("Generation time (seconds)", "performance", "elapsed_seconds", ""),
            ("Claude API calls", "performance", "llm_calls", ""),
            ("Input tokens", "performance", "input_tokens", ""),
            ("Output tokens", "performance", "output_tokens", ""),
            ("Search API requests", "performance", "search_requests", ""),
            ("Estimated cost", "performance", "estimated_cost_usd", "usd"),
        ])
        table("D. Agent performance", [
            ("Planner - overall plan quality (judge, 0-100)", "agents", "planner_overall", ""),
            ("Planner - search query quality (judge, 0-100)", "agents", "planner_query_quality", ""),
            ("Researcher - sources read as full pages", "quality", "sources_full_page_read", "pct"),
            ("Researcher - mean source relevance (0-1)", "quality", "sources_mean_relevance", ""),
            ("Critic - seeded unsupported claims detected", "agents", "critic_detection_rate", "pct"),
            ("Critic - other claims flagged in the probe, per report", "agents", "critic_other_claims_flagged", ""),
        ])
        total_cost = sum(r["performance"]["estimated_cost_usd"] for r in done)
        eval_cost = sum(r.get("evaluation_cost_usd", 0) for r in done)
        lines += [f"Total estimated cost: reports ${total_cost:.2f}, evaluation overhead ${eval_cost:.2f}.", ""]

    lines += ["## Per-question results", "", "| ID | Domain | Status | Critic | Cites | URLs OK | Judge supported | Seeded caught | Time (s) | Cost |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["status"] != "completed":
            lines.append(f"| {r['id']} | {r['domain']} | failed | | | | | | | |")
            continue
        c, q, p, a = r["citations"], r["quality"], r["performance"], r.get("agents", {})
        lines.append(
            f"| {r['id']} | {r['domain']} | completed | {_fmt(q['critic_score'])} | {c['citations']} | {_fmt(c.get('urls_resolve'), 'pct')} "
            f"| {_fmt(c.get('judge_supported'), 'pct')} | {_fmt(a.get('critic_detected'))}/{_fmt(a.get('critic_seeded'))} | {_fmt(p['elapsed_seconds'])} | {_fmt(p['estimated_cost_usd'], 'usd')} |"
        )
    if failed:
        lines += ["", "## Failures", ""] + [f"- **{r['id']}**: {r.get('error')}" for r in failed]
    errors = [(r["id"], e) for r in done for e in r.get("evaluation_errors", [])]
    if errors:
        lines += ["", "## Measurements that could not be taken", ""] + [f"- **{rid}**: {e}" for rid, e in errors]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--depth", choices=["quick", "standard", "deep"], default="quick")
    parser.add_argument("--provider", choices=["tavily", "brave"])
    parser.add_argument("--limit", type=int, help="only run the first N questions")
    parser.add_argument("--ids", nargs="*", help="only run these question ids")
    parser.add_argument("--judge-sample", type=int, default=5, help="report claims the independent judge checks per report")
    parser.add_argument("--out", type=Path, default=HERE / "results")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging("WARNING")
    provider = args.provider or settings.search_provider
    missing = settings.missing_keys(provider)
    if missing:
        print(f"Cannot run the benchmark: missing {', '.join(missing)}. Set them in .env (see .env.example).\nNo results were produced.", file=sys.stderr)
        return 2

    questions = json.loads((HERE / "questions.json").read_text(encoding="utf-8"))
    if args.ids:
        questions = [q for q in questions if q["id"] in args.ids]
    questions = questions[: args.limit] if args.limit else questions
    init_db()
    result = asyncio.run(run_all(questions, ResearchService(settings), settings, provider, args.depth, args.out, judge_sample=args.judge_sample))
    completed = sum(1 for r in result["rows"] if r["status"] == "completed")
    print(f"\n{completed}/{len(result['rows'])} completed. Report: {args.out / 'BENCHMARK_REPORT.md'}")
    return 0 if completed else 1


if __name__ == "__main__":
    sys.exit(main())
