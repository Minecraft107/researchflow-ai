"""LangGraph node functions. Each takes the shared state and returns a partial state update."""
from __future__ import annotations

import logging

from ..agents.critic import critique
from ..agents.planner import create_plan
from ..agents.researcher import run_research
from ..agents.writer import select_citable, write_report
from ..schemas import Report, ReportSection
from ..services.report_service import SECTION_HEADINGS, build_markdown
from ..tools.citation_validator import citation_stats, extract_cited_claims, normalize_citations, review_citations
from .state import ResearchState, RunContext

log = logging.getLogger(__name__)

MAX_FOLLOWUPS_PER_ITERATION = 6


class ResearchError(RuntimeError):
    """The research cannot continue (e.g. nothing verifiable was collected)."""


class Nodes:
    def __init__(self, ctx: RunContext):
        self.ctx = ctx

    # ------------------------------------------------------------ planner
    async def planner(self, state: ResearchState) -> dict:
        ctx = self.ctx
        await ctx.emit("planner", "started", "Planning research", step="planning", stage="planning")
        plan = await create_plan(ctx, state["topic"])
        await ctx.emit("planner", "progress", f"Generated {len(plan.subtopics)} subtopics and {len(plan.questions)} research questions", step="subtopics")
        # Interleave by rank so that, if the query budget cuts the list, every question keeps its best query.
        queries = [
            {"question_id": q.id, "query": q.search_queries[i]}
            for i in range(ctx.preset["queries_per_question"])
            for q in plan.questions
            if i < len(q.search_queries)
        ][: ctx.settings.max_search_queries]
        await ctx.emit("planner", "completed", f"Created {len(queries)} search queries", step="queries", plan=plan.model_dump())
        return {"plan": plan, "pending_queries": queries}

    # ------------------------------------------------------------ researcher
    async def _research(self, state: ResearchState, label: str) -> dict:
        ctx = self.ctx
        queries = state["pending_queries"]
        await ctx.emit("researcher", "started", label, step="searching", stage="researching")
        update = await run_research(
            ctx, state["plan"], queries,
            state.get("sources", []), state.get("evidence", []), state.get("page_texts", {}), state.get("failed_sources", []),
        )
        await ctx.emit(
            "researcher", "completed",
            f"Research pass complete: {len(update['sources'])} sources, {len(update['evidence'])} evidence items",
            sources_count=len(update["sources"]),
        )
        return {**update, "queries_run": state.get("queries_run", []) + [q["query"] for q in queries], "pending_queries": []}

    async def researcher(self, state: ResearchState) -> dict:
        return await self._research(state, "Executing the research plan")

    async def additional_research(self, state: ResearchState) -> dict:
        return await self._research(state, f"Additional research requested by the Critic ({len(state['pending_queries'])} queries)")

    # ------------------------------------------------------------ critic
    async def critic(self, state: ResearchState) -> dict:
        ctx = self.ctx
        iteration = state.get("iteration", 0) + 1
        max_iterations = ctx.preset["max_iterations"]
        queries_run = state.get("queries_run", [])
        await ctx.emit("critic", "started", f"Reviewing evidence (review {iteration} of {max_iterations})", step="reviewing", stage="critiquing", iteration=iteration)

        budget_left = ctx.settings.max_search_queries - len(queries_run)
        max_followups = max(0, min(MAX_FOLLOWUPS_PER_ITERATION, budget_left)) if iteration < max_iterations else 0
        result = await critique(ctx, state["topic"], state["plan"], state["sources"], state["evidence"], queries_run, iteration, max_followups)

        threshold = ctx.settings.quality_threshold
        pending: list[dict] = []
        if result.quality_score < threshold and max_followups:
            seen = {q.lower() for q in queries_run}
            for followup in result.additional_queries:
                query = followup.query.strip()
                if query and query.lower() not in seen:
                    seen.add(query.lower())
                    pending.append({"question_id": followup.question_id, "query": query})
            pending = pending[:max_followups]

        await ctx.emit("critic", "progress", f"{len(result.unsupported_claims)} unsupported claims, {len(result.contradictions)} contradictions flagged", step="citations")
        gaps = len(result.unanswered_questions) + len(result.missing_perspectives)
        if result.quality_score >= threshold:
            verdict = "meets the quality bar"
        elif pending:
            verdict = f"below {threshold} - requesting {len(pending)} more searches"
        elif iteration >= max_iterations:
            verdict = f"below {threshold} after the final review - the report will state its limitations"
        else:
            verdict = f"below {threshold} - no further searches available (no new queries or query budget used)"
        await ctx.emit(
            "critic", "completed", f"Quality score {result.quality_score}/100: {verdict}. {gaps} gaps identified",
            step="gaps", iteration=iteration, quality_score=result.quality_score, critique=result.model_dump(),
        )
        return {"critiques": state.get("critiques", []) + [result], "iteration": iteration, "pending_queries": pending}

    def route_after_critic(self, state: ResearchState) -> str:
        """Conditional edge: loop back for more research, or move on to the report."""
        return "additional_research" if state["pending_queries"] else "report_generation"

    # ------------------------------------------------------------ report
    async def report_generation(self, state: ResearchState) -> dict:
        ctx = self.ctx
        sources, evidence = state["sources"], state["evidence"]
        citable = select_citable(sources, evidence)
        if not citable:
            raise ResearchError(
                "No verifiable evidence was collected (no quote could be confirmed in any fetched source), "
                "so a cited report cannot be written. Try a different topic, depth or search provider."
            )
        await ctx.emit("writer", "started", f"Writing the report from {len(citable)} citable sources", step="writing", stage="writing")
        last = state["critiques"][-1] if state.get("critiques") else None
        run_facts = {
            "agents": "Planner, Researcher, Critic (orchestrated with LangGraph), then report writing and a citation audit",
            "search provider": ctx.search.name,
            "search queries run": len(state.get("queries_run", [])),
            "sources collected": len(sources),
            "sources with full page content retrieved": sum(1 for s in sources if s.content_status == "full"),
            "sources that could not be fetched directly": len(state.get("failed_sources", [])),
            "evidence items extracted": len(evidence),
            "evidence items whose quote was verified in the source text": sum(1 for e in evidence if e.quote_verified),
            "sources eligible for citation (verified evidence only)": len(citable),
            "critic review iterations": state.get("iteration", 0),
            "final critic quality score": f"{last.quality_score}/100 (target {ctx.settings.quality_threshold})" if last else "n/a",
        }
        draft = await write_report(ctx, state["topic"], state["plan"], citable, evidence, last, run_facts)
        sections, references, stats = normalize_citations({key: getattr(draft, key) for key, _ in SECTION_HEADINGS}, citable)
        await ctx.emit("writer", "completed", f"Report drafted with {stats['total_citations']} citations to {len(references)} sources", step="writing")
        return {"report_title": draft.title.strip(), "report_sections": sections, "references": references, "citation_stats": stats}

    # ------------------------------------------------------------ citation validation
    async def citation_validation(self, state: ResearchState) -> dict:
        ctx = self.ctx
        claims = extract_cited_claims(state["report_sections"])
        await ctx.emit("critic", "started", f"Checking {len(claims)} cited claims against the collected evidence", step="citations", stage="validating")
        checks = await review_citations(ctx.llm, claims, state["references"], state["evidence"])
        stats = {**state["citation_stats"], **citation_stats(checks)}
        flagged = stats["partially_supported"] + stats["unsupported"]
        await ctx.emit("critic", "completed", f"Citation audit: {stats['supported']} of {stats['claims_checked']} claims fully supported, {flagged} flagged", step="citations")
        return {"citation_checks": checks, "citation_stats": stats}

    # ------------------------------------------------------------ final report
    async def final_report(self, state: ResearchState) -> dict:
        ctx = self.ctx
        threshold = ctx.settings.quality_threshold
        last = state["critiques"][-1] if state.get("critiques") else None
        score = last.quality_score if last else None
        threshold_met = score is not None and score >= threshold
        sections = dict(state["report_sections"])

        # Limitations that must be stated regardless of what the writer chose to say.
        notes: list[str] = []
        if last and not threshold_met:
            questions = {q.id: q.question for q in state["plan"].questions}
            issues = [f"Not adequately answered: {questions[q]}" for q in last.unanswered_questions if q in questions]
            issues += [f"Missing perspective: {p}" for p in last.missing_perspectives]
            issues += [f"Weakly supported: {c.claim} ({c.reason})" for c in last.unsupported_claims[:5]]
            notes.append(
                f"**Quality notice.** The Critic scored this research {score}/100 after {state['iteration']} review "
                f"iteration(s), below the target of {threshold}. Treat the findings as provisional."
                + "".join(f"\n- {issue}" for issue in issues)
            )
        flagged = [c for c in state.get("citation_checks", []) if c.verdict in ("partially_supported", "unsupported")]
        if flagged:
            notes.append(
                f"**Citation audit.** {len(flagged)} of {len(state['citation_checks'])} cited claims were not fully "
                "supported by the evidence collected from their sources:"
                + "".join(f"\n- ({c.verdict.replace('_', ' ')}) {c.claim} - {c.reason}" for c in flagged)
            )
        failed = state.get("failed_sources", [])
        if failed:
            notes.append(f"**Source access.** {len(failed)} source page(s) could not be fetched directly; evidence from search snippets alone was excluded from the report.")
        if notes:
            sections["limitations"] = sections["limitations"].rstrip() + "\n\n" + "\n\n".join(notes)

        section_list = [ReportSection(heading=heading, content=sections[key]) for key, heading in SECTION_HEADINGS]
        ctx.tracker.stop()
        report = Report(
            title=state["report_title"],
            sections=section_list,
            references=state["references"],
            markdown=build_markdown(state["report_title"], section_list, state["references"]),
            citation_checks=state.get("citation_checks", []),
            citation_stats=state["citation_stats"],
            quality_score=score,
            threshold_met=threshold_met,
        )
        await ctx.emit("system", "completed", "Final report ready", stage="completed", quality_score=score)
        return {"report": report, "metrics": ctx.tracker.snapshot()}
