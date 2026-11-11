"""The LangGraph workflow.

START -> planner -> researcher -> critic --(score < threshold, iterations left)--> additional_research -> critic
                                         \\--(otherwise)--> report_generation -> citation_validation -> final_report -> END
"""
from __future__ import annotations

import anthropic
import httpx
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from .nodes import Nodes
from .state import ResearchState, RunContext


def _is_transient(exc: Exception) -> bool:
    """Retry whole nodes only on failures that can succeed next time (after the SDK's own retries)."""
    if isinstance(exc, (anthropic.RateLimitError, anthropic.APIConnectionError, httpx.TransportError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):  # search provider 5xx
        return exc.response.status_code >= 500
    return isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500


RETRY = RetryPolicy(max_attempts=3, initial_interval=2.0, backoff_factor=2.0, retry_on=_is_transient)


def build_workflow(ctx: RunContext):
    nodes = Nodes(ctx)
    graph = StateGraph(ResearchState)
    for name in ("planner", "researcher", "additional_research", "critic", "report_generation", "citation_validation", "final_report"):
        graph.add_node(name, getattr(nodes, name), retry_policy=RETRY)

    graph.add_edge(START, "planner")
    graph.add_edge("planner", "researcher")
    graph.add_edge("researcher", "critic")
    graph.add_conditional_edges("critic", nodes.route_after_critic, ["additional_research", "report_generation"])
    graph.add_edge("additional_research", "critic")
    graph.add_edge("report_generation", "citation_validation")
    graph.add_edge("citation_validation", "final_report")
    graph.add_edge("final_report", END)

    # ponytail: in-memory checkpoints - enough to resume a run from its last completed node
    # within this process. Use langgraph-checkpoint-sqlite/postgres to survive restarts.
    return graph.compile(checkpointer=InMemorySaver())
