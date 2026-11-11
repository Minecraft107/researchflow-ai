"""Research endpoints: start a run, read status, agent activity and sources."""
from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select

from ..config import DEPTH_PRESETS, get_settings
from ..database import AgentEvent, ResearchRun, SessionLocal, events_after
from ..schemas import StartResearchRequest, StartResearchResponse
from ..services.research_service import ResearchService

router = APIRouter(prefix="/api")

# ponytail: in-memory sliding window keyed by client IP, per process. Behind a reverse proxy or
# with several workers, move this to the proxy or a Redis-backed limiter.
_hits: dict[str, deque[float]] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    limit = get_settings().rate_limit_per_minute
    now = time.monotonic()
    hits = _hits[request.client.host if request.client else "unknown"]
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= limit:
        raise HTTPException(429, "Too many requests - try again in a minute", headers={"Retry-After": "60"})
    hits.append(now)


def get_service(request: Request) -> ResearchService:
    return request.app.state.service


def get_run(research_id: uuid.UUID) -> ResearchRun:
    with SessionLocal() as db:
        run = db.get(ResearchRun, str(research_id))
    if run is None:
        raise HTTPException(404, "Research not found")
    return run


def iso(value: datetime | None) -> str | None:
    """SQLite drops tzinfo; everything is stored in UTC, so say so explicitly for the browser."""
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def event_dict(event: AgentEvent) -> dict:
    return {
        "id": event.id, "agent": event.agent, "type": event.type, "message": event.message,
        "data": event.data, "created_at": iso(event.created_at),
    }


def run_summary(run: ResearchRun) -> dict:
    return {
        "research_id": run.id, "topic": run.topic, "depth": run.depth, "provider": run.provider,
        "status": run.status, "stage": run.stage, "iteration": run.iteration, "quality_score": run.quality_score,
        "sources_count": len(run.sources or []), "error": run.error,
        "created_at": iso(run.created_at), "completed_at": iso(run.completed_at),
    }


@router.get("/config")
def config() -> dict:
    """What the UI needs to render the landing page. Only booleans about keys - never the keys."""
    settings = get_settings()
    return {
        "app_name": "ResearchFlow AI",
        "default_provider": settings.search_provider,
        "providers": {name: {"configured": bool(settings.search_key(name))} for name in ("tavily", "brave")},
        "anthropic_configured": bool(settings.anthropic_api_key),
        "model": settings.claude_model,
        "extraction_model": settings.extraction_model,
        "depths": {name: settings.preset(name) for name in DEPTH_PRESETS},
        "quality_threshold": settings.quality_threshold,
        "max_search_queries": settings.max_search_queries,
    }


@router.post("/research/start", response_model=StartResearchResponse, status_code=202, dependencies=[Depends(rate_limit)])
async def start_research(body: StartResearchRequest, service: ResearchService = Depends(get_service)) -> StartResearchResponse:
    settings = get_settings()
    provider = body.provider or settings.search_provider
    missing = settings.missing_keys(provider)
    if missing:
        raise HTTPException(503, f"Missing API key(s): {', '.join(missing)}. Add them to the .env file and restart the server.")
    if service.active_runs() >= settings.max_concurrent_runs:
        raise HTTPException(429, "Too many research jobs are running - wait for one to finish")
    research_id = service.start(body.topic, body.depth, provider)
    return StartResearchResponse(research_id=research_id, status="pending")


@router.get("/research")
def list_research(limit: int = Query(20, ge=1, le=100)) -> list[dict]:
    with SessionLocal() as db:
        runs = db.scalars(select(ResearchRun).order_by(ResearchRun.created_at.desc()).limit(limit)).all()
    return [run_summary(run) for run in runs]


@router.get("/research/{research_id}")
def research_status(run: ResearchRun = Depends(get_run)) -> dict:
    """Status and results: plan, critiques, metrics and (once finished) the report."""
    return {
        **run_summary(run), "plan": run.plan, "critiques": run.critiques or [], "metrics": run.metrics or {},
        "failed_sources_count": len(run.failed_sources or []), "evidence_count": len(run.evidence or []), "report": run.report,
    }


@router.get("/research/{research_id}/agents")
def agent_activity(run: ResearchRun = Depends(get_run)) -> dict:
    """Per-agent activity log and outputs."""
    agents: dict[str, dict] = {name: {"status": "idle", "events": []} for name in ("planner", "researcher", "critic", "writer", "system")}
    for event in events_after(run.id, 0):
        agent = agents.setdefault(event.agent, {"status": "idle", "events": []})
        agent["events"].append(event_dict(event))
        agent["status"] = {"started": "working", "progress": "working", "completed": "done", "failed": "failed"}.get(event.type, agent["status"])
    agents["planner"]["output"] = run.plan
    agents["researcher"]["output"] = {"sources_count": len(run.sources or []), "evidence_count": len(run.evidence or []), "failed_sources": run.failed_sources or []}
    agents["critic"]["output"] = {"critiques": run.critiques or [], "citation_checks": (run.report or {}).get("citation_checks", [])}
    agents["writer"]["output"] = {"title": run.report["title"]} if run.report else None
    return {"research_id": run.id, "status": run.status, "agents": agents}


@router.get("/research/{research_id}/sources")
def research_sources(run: ResearchRun = Depends(get_run), min_relevance: float = Query(0.0, ge=0.0, le=1.0)) -> dict:
    sources = [s for s in (run.sources or []) if s.get("relevance_score", 0.0) >= min_relevance]
    kept = {s["id"] for s in sources}
    cited = {r["source_id"]: r["number"] for r in (run.report or {}).get("references", [])}
    return {
        "sources": [{**s, "citation_number": cited.get(s["id"])} for s in sources],
        "evidence": [e for e in (run.evidence or []) if e["source_id"] in kept],
        "failed_sources": run.failed_sources or [],
    }
