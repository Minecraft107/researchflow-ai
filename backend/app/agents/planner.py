"""Planner agent: turns a topic into a structured research plan."""
from __future__ import annotations

from datetime import date

from ..graph.state import RunContext
from ..schemas import ResearchPlan

SYSTEM = """You are the Planner in a three-agent research team (Planner, Researcher, Critic). The Researcher \
will run your search queries on the web and extract evidence; the Critic will then audit that evidence. \
Nobody can ask you follow-up questions, so the plan must stand on its own.

Design a research strategy for the topic:
- State the research objective in one or two sentences.
- Break the topic into distinct subtopics that together cover it: current state, mechanisms or applications, \
measurable benefits, risks and criticisms, limitations, and outlook - adapted to what this topic needs.
- Write one focused research question per subtopic. Give each a priority (high = the report fails without it).
- For each question say what evidence would answer it (statistics, studies, official reports, dated events).
- For each question write search queries a search engine handles well: specific keywords, named entities, \
and a year when recency matters. Make the queries for one question approach it from different angles, and \
aim some at primary sources (government, academic, official reports).
- List information gaps: aspects likely to be contested, fast-changing, or poorly documented.

Include at least one question that looks for criticism, counter-evidence or limitations, so the final \
report is not one-sided."""


async def create_plan(ctx: RunContext, topic: str) -> ResearchPlan:
    n_questions, n_queries = ctx.preset["questions"], ctx.preset["queries_per_question"]
    plan = await ctx.llm.structured(
        agent="planner",
        system=SYSTEM,
        user=(
            f"<topic>{topic}</topic>\n\nToday's date: {date.today().isoformat()}\n"
            f"Produce exactly {n_questions} research questions with exactly {n_queries} search "
            f"quer{'y' if n_queries == 1 else 'ies'} each."
        ),
        schema=ResearchPlan,
    )
    # Enforce the budget in code rather than trusting the model's count.
    plan.questions = plan.questions[:n_questions]
    for i, question in enumerate(plan.questions, start=1):
        question.id = f"Q{i}"
        question.search_queries = [q.strip() for q in question.search_queries if q.strip()][:n_queries]
    if not plan.questions or not any(q.search_queries for q in plan.questions):
        raise ValueError("planner returned no research questions or search queries")
    return plan
