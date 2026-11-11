"""Critic agent: independent quality control over the collected research."""
from __future__ import annotations

from datetime import date

from ..graph.state import RunContext
from ..llm import UNTRUSTED_NOTICE
from ..schemas import Critique, Evidence, ResearchPlan, Source

SYSTEM = f"""You are the Critic in a three-agent research team: an independent quality-control reviewer. You did \
not plan or collect this research, and you gain nothing from it passing. Judge it the way a careful editor \
would before letting a report be written from it.

You receive the research plan, the list of sources, and every evidence item (a claim, the quote backing it, \
and whether the quote was verified as present in the fetched page). Evaluate:
1. Coverage - is every research question answered with enough evidence? High-priority questions weigh most.
2. Support - does each quote really support its claim? A claim that overstates its quote, or whose quote is \
marked verified="false", is unsupported: list it with its evidence id.
3. Reliability - are the sources credible for these claims (primary, academic, official, reputable press)? \
Is important evidence resting on weak or snippet-only sources?
4. Currency - is anything outdated for a topic where recency matters?
5. Contradictions - do sources disagree? Say which and on what.
6. Perspectives - are criticisms, risks or opposing views missing?
7. Logic - could the conclusions a writer would draw actually be justified from this evidence?

Score coverage, evidence and reliability from 0-100, then give an overall quality_score from 0-100 \
(roughly 40% coverage, 35% evidence support, 25% reliability and currency, lowered further for unaddressed \
contradictions or one-sidedness). 80 or more means a reader could rely on a report built only from this \
evidence. Score what is there, not what you would expect to exist.

If the research falls short, write additional_queries: specific new web searches, each tied to a research \
question id, that would close the most important gaps. Do not repeat queries already run.

{UNTRUSTED_NOTICE}"""


def _render(plan: ResearchPlan, sources: list[Source], evidence: list[Evidence]) -> str:
    by_question: dict[str, list[Evidence]] = {}
    for item in evidence:
        by_question.setdefault(item.question_id, []).append(item)
    lines = [f"Research objective: {plan.objective}", "", "<sources>"]
    for s in sources:
        lines.append(
            f"{s.id} | {s.title} | {s.publisher or 'unknown publisher'} | published: {s.published_date or 'unknown'} "
            f"| reliability: {s.reliability} ({s.source_type}) | content: {s.content_status}"
        )
    lines.append("</sources>")
    for q in plan.questions:
        lines += ["", f'<question id="{q.id}" priority="{q.priority}">', q.question, f"Evidence wanted: {q.required_evidence}"]
        items = by_question.get(q.id, [])
        if not items:
            lines.append("(no evidence collected)")
        for e in items:
            lines.append(
                f'<evidence id="{e.id}" source="{e.source_id}" verified="{str(e.quote_verified).lower()}">'
                f"\nclaim: {e.claim}\nquote: {e.quote}\n</evidence>"
            )
        lines.append("</question>")
    return "\n".join(lines)


async def critique(
    ctx: RunContext,
    topic: str,
    plan: ResearchPlan,
    sources: list[Source],
    evidence: list[Evidence],
    queries_run: list[str],
    iteration: int,
    max_followups: int,
) -> Critique:
    result = await ctx.llm.structured(
        agent="critic",
        system=SYSTEM,
        user=(
            f"<topic>{topic}</topic>\nToday's date: {date.today().isoformat()}\n"
            f"This is review {iteration} of at most {ctx.preset['max_iterations']}.\n\n"
            f"{_render(plan, sources, evidence)}\n\n"
            f"Queries already run:\n" + "\n".join(f"- {q}" for q in queries_run) + "\n\n"
            f"Return at most {max_followups} additional_queries (none if the research is good enough)."
        ),
        schema=Critique,
    )
    valid_ids = {q.id for q in plan.questions}
    result.unanswered_questions = [q for q in result.unanswered_questions if q in valid_ids]
    for followup in result.additional_queries:
        if followup.question_id not in valid_ids:
            followup.question_id = plan.questions[0].id
    return result
