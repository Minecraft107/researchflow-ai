"""Server-Sent Events: live agent activity for one research run.

SSE (not WebSocket) because the feed is one-way, and EventSource reconnects on its own and
resends Last-Event-ID, so a dropped connection resumes exactly where it left off.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from ..database import ResearchRun, SessionLocal, events_after
from ..services.research_service import TERMINAL, ResearchService
from .research import event_dict, get_run, get_service

router = APIRouter(prefix="/api/research/{research_id}")


def _status(research_id: str) -> str:
    with SessionLocal() as db:
        return db.get(ResearchRun, research_id).status


@router.get("/stream")
async def stream(
    request: Request,
    run: ResearchRun = Depends(get_run),
    service: ResearchService = Depends(get_service),
    last_event_id: str | None = Header(default=None),
) -> StreamingResponse:
    research_id = run.id
    cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

    async def events():
        nonlocal cursor
        while True:
            # Read status BEFORE the events: a run only turns terminal after its last event is
            # stored, so "terminal + everything up to now sent" means nothing can be missed.
            finished = _status(research_id) in TERMINAL
            batch = events_after(research_id, cursor)
            for event in batch:
                cursor = event.id
                yield f"id: {event.id}\nevent: agent\ndata: {json.dumps(event_dict(event))}\n\n"
            if finished:
                yield f"event: done\ndata: {json.dumps({'status': _status(research_id)})}\n\n"
                return
            if await request.is_disconnected():
                return
            if not batch:
                yield ": keepalive\n\n"
            await service.wait_for_event(research_id, timeout=10)

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
