"""Runs research jobs: owns the LangGraph execution, persistence and the live event feed."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable

from ..config import Settings
from ..database import AgentEvent, ResearchRun, SessionLocal, utcnow
from ..graph.nodes import ResearchError
from ..graph.state import RunContext
from ..graph.workflow import build_workflow
from ..llm import ClaudeLLM, LLMError
from ..tools.search import SearchError, SearchProvider, get_search_provider
from .cost_tracker import CostTracker

log = logging.getLogger(__name__)

TERMINAL = ("completed", "failed")
# State keys copied onto the run row whenever a node finishes.
_PERSISTED = ("plan", "sources", "evidence", "failed_sources", "critiques")


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_dump(v) for v in value]
    return value


class ResearchService:
    def __init__(
        self,
        settings: Settings,
        llm_factory: Callable[[Settings, CostTracker], Any] = ClaudeLLM,
        search_factory: Callable[[Settings, str], SearchProvider] = get_search_provider,
    ):
        self.settings = settings
        self.llm_factory = llm_factory
        self.search_factory = search_factory
        self._tasks: dict[str, asyncio.Task] = {}
        self._signals: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------ public API
    def active_runs(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    def _create(self, topic: str, depth: str, provider: str) -> str:
        research_id = str(uuid.uuid4())
        with SessionLocal() as db:
            db.add(ResearchRun(id=research_id, topic=topic, depth=depth, provider=provider, status="pending"))
            db.commit()
        self._signals[research_id] = asyncio.Event()
        return research_id

    async def run_and_wait(self, topic: str, depth: str, provider: str) -> tuple[str, dict | None]:
        """Run one job in the caller's task and return (id, final state). Used by the benchmark."""
        research_id = self._create(topic, depth, provider)
        return research_id, await self.run(research_id, topic, depth, provider)

    def start(self, topic: str, depth: str, provider: str) -> str:
        research_id = self._create(topic, depth, provider)
        task = asyncio.create_task(self.run(research_id, topic, depth, provider))
        self._tasks[research_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(research_id, None))
        return research_id

    async def wait_for_event(self, research_id: str, timeout: float) -> None:
        """Block until the run emits a new event (or `timeout` passes) - used by the SSE stream."""
        signal = self._signals.get(research_id)
        if signal is None:
            await asyncio.sleep(timeout)
            return
        try:
            await asyncio.wait_for(signal.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def mark_interrupted(self) -> None:
        """On startup: runs left 'running' by a previous process can never finish - say so."""
        with SessionLocal() as db:
            for run in db.query(ResearchRun).filter(ResearchRun.status.in_(("pending", "running"))):
                run.status, run.stage, run.error = "failed", "failed", "Interrupted by a server restart"
                run.completed_at = utcnow()
            db.commit()

    # ------------------------------------------------------------ execution
    async def run(self, research_id: str, topic: str, depth: str, provider: str) -> dict | None:
        """Execute one research job end to end. Returns the final graph state (None on failure)."""
        settings = self.settings
        tracker = CostTracker(settings.load_pricing(), provider)
        live = {"stage": "queued", "iteration": 0, "sources_count": 0, "quality_score": None}

        async def emit(agent: str, type: str, message: str, **data: Any) -> None:
            for key in live:
                if key in data:
                    live[key] = data[key]
            payload = {**data, "snapshot": {**live, "metrics": tracker.snapshot()}}
            with SessionLocal() as db:
                db.add(AgentEvent(research_id=research_id, agent=agent, type=type, message=message, data=payload))
                run = db.get(ResearchRun, research_id)
                run.stage, run.iteration, run.quality_score = live["stage"], live["iteration"], live["quality_score"]
                run.metrics = payload["snapshot"]["metrics"]
                db.commit()
            log.info("[%s] %s/%s: %s", research_id[:8], agent, type, message)
            self._notify(research_id)

        self._update(research_id, status="running")
        try:
            ctx = RunContext(
                settings=settings,
                llm=self.llm_factory(settings, tracker),
                search=self.search_factory(settings, provider),
                tracker=tracker,
                emit=emit,
                preset=settings.preset(depth),
            )
            graph = build_workflow(ctx)
            config = {"configurable": {"thread_id": research_id}}
            await emit("system", "started", f"Research started ({depth} depth, {provider} search)", stage="planning")

            state: dict | None = None
            graph_input: dict | None = {"topic": topic, "depth": depth}
            for attempt in (1, 2):
                try:
                    async for update in graph.astream(graph_input, config, stream_mode="updates"):
                        for node_update in update.values():
                            if isinstance(node_update, dict):
                                self._update(research_id, **{k: _dump(node_update[k]) for k in _PERSISTED if k in node_update})
                    state = (await graph.aget_state(config)).values
                    break
                except (LLMError, SearchError, ResearchError, ValueError):
                    raise  # deterministic failures: resuming would fail the same way
                except Exception as exc:  # noqa: BLE001 - unexpected failure: resume once from the last checkpoint
                    if attempt == 2:
                        raise
                    log.warning("[%s] run failed (%s); resuming from last checkpoint", research_id[:8], exc)
                    await emit("system", "progress", f"Recovering from an error ({type(exc).__name__}); resuming from the last checkpoint")
                    graph_input = None  # None = continue from the checkpoint instead of restarting

            self._update(
                research_id, status="completed", stage="completed", report=_dump(state["report"]),
                metrics=state["metrics"], completed_at=utcnow(),
            )
            return state
        except Exception as exc:  # noqa: BLE001 - record every failure on the run; never crash the server
            log.exception("[%s] research failed", research_id[:8])
            tracker.stop()
            # Known error types carry messages written for users; anything else stays generic.
            message = str(exc) if isinstance(exc, (LLMError, SearchError, ResearchError)) else f"Unexpected error ({type(exc).__name__}). See server logs."
            await emit("system", "failed", message, stage="failed")
            self._update(research_id, status="failed", stage="failed", error=message, metrics=tracker.snapshot(), completed_at=utcnow())
            return None
        finally:
            self._notify(research_id)
            self._signals.pop(research_id, None)

    # ------------------------------------------------------------ helpers
    def _notify(self, research_id: str) -> None:
        signal = self._signals.get(research_id)
        if signal is not None:
            signal.set()  # wake current SSE waiters...
            signal.clear()  # ...and re-arm for the next event

    @staticmethod
    def _update(research_id: str, **fields: Any) -> None:
        if not fields:
            return
        with SessionLocal() as db:
            run = db.get(ResearchRun, research_id)
            for key, value in fields.items():
                setattr(run, key, value)
            db.commit()
