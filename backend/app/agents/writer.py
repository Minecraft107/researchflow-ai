"""Report generation: writes the report sections from verified evidence only."""
from __future__ import annotations

from datetime import date

from ..graph.state import RunContext
from ..llm import UNTRUSTED_NOTICE
from ..schemas import Critique, Evidence, ReportDraft, ResearchPlan, Source

SYSTEM = f"""You write the final report for a multi-agent research team. You are given the research plan, a \
numbered list of sources with the evidence extracted from each (claim + verbatim quote), the Critic's last \
review, and facts about how the research was run. The reader has none of this context - only your report.

Citation rules (the report is audited against them automatically):
- Every factual statement that comes from the research carries an inline citation like [3] or [2, 5], placed \
right after the statement, using only the source numbers you were given.
- Cite a source only for what its evidence actually says. Keep numbers, dates and qualifiers exactly as the \
evidence states them. If the evidence does not cover something, do not assert it - say it was not established.
- Do not use your own background knowledge for facts, and do not write a references list (it is generated \
from the cited sources).

Sections (each is Markdown without a top-level heading; use ### sub-headings, bullet lists and bold where \
they help):
- title: specific and descriptive.
- executive_summary: the main answer and the 3-5 most important findings, with citations.
- research_objectives: the objective and the research questions investigated.
- research_methodology: how the research was done, using only the run facts provided (agents, search \
provider, number of queries and sources, review iterations, quote verification, quality score).
- detailed_findings: organised by research question or theme; the evidence in depth, with citations.
- analysis_and_discussion: what the findings mean together - patterns, trade-offs, implications. Make clear \
which statements are your inference from the cited evidence.
- contradictory_evidence: where sources disagree or evidence cuts against the main findings, with citations. \
If none was found, say so plainly and note that absence of found contradiction is not proof of consensus.
- limitations: gaps, weak or unverified sources, unanswered questions and the Critic's unresolved concerns.
- conclusion: a direct answer to the research topic, justified by the findings.

{UNTRUSTED_NOTICE}"""


def select_citable(sources: list[Source], evidence: list[Evidence]) -> list[Source]:
    """Sources the writer may cite: only those with at least one quote verified in the page text."""
    verified = {e.source_id for e in evidence if e.quote_verified}
    return sorted((s for s in sources if s.id in verified), key=lambda s: s.relevance_score, reverse=True)


async def write_report(
    ctx: RunContext,
    topic: str,
    plan: ResearchPlan,
    citable: list[Source],
    evidence: list[Evidence],
    critique: Critique | None,
    run_facts: dict,
) -> ReportDraft:
    by_source: dict[str, list[Evidence]] = {}
    for item in evidence:
        if item.quote_verified:
            by_source.setdefault(item.source_id, []).append(item)

    lines = [f"<topic>{topic}</topic>", f"Today's date: {date.today().isoformat()}", "", f"Objective: {plan.objective}", "Research questions:"]
    lines += [f"- {q.id} ({q.priority}): {q.question}" for q in plan.questions]
    lines += ["", "Sources you may cite:"]
    for number, source in enumerate(citable, start=1):
        lines.append(
            f'<source number="{number}">\nTitle: {source.title}\nPublisher: {source.publisher or "unknown"} | '
            f"Published: {source.published_date or 'unknown'} | Reliability: {source.reliability}"
        )
        lines += [f'<evidence question="{e.question_id}">claim: {e.claim} | quote: "{e.quote}"</evidence>' for e in by_source[source.id]]
        lines.append("</source>")
    lines += ["", "Run facts (for the methodology section):"] + [f"- {k}: {v}" for k, v in run_facts.items()]
    if critique is not None:
        lines += [
            "", f"Critic's last review (score {critique.quality_score}/100): {critique.summary}",
            "Unanswered questions: " + (", ".join(critique.unanswered_questions) or "none"),
            "Contradictions noted: " + ("; ".join(critique.contradictions) or "none"),
            "Missing perspectives: " + ("; ".join(critique.missing_perspectives) or "none"),
            "Outdated information: " + ("; ".join(critique.outdated_information) or "none"),
            "Reliability concerns: " + ("; ".join(critique.reliability_concerns) or "none"),
        ]
    return await ctx.llm.structured(agent="writer", system=SYSTEM, user="\n".join(lines), schema=ReportDraft)
