"""Final report retrieval and export."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response

from ..database import ResearchRun
from ..schemas import Report
from ..services import report_service
from .research import get_run

router = APIRouter(prefix="/api/research/{research_id}")


def get_report(run: ResearchRun = Depends(get_run)) -> Report:
    if run.report is None:
        detail = f"Research failed: {run.error}" if run.status == "failed" else "The report is not ready yet"
        raise HTTPException(409, detail)
    return Report.model_validate(run.report)


def _download(content: bytes, media_type: str, report: Report, extension: str) -> Response:
    slug = re.sub(r"[^a-z0-9]+", "-", report.title.lower()).strip("-")[:60] or "research-report"
    return Response(content, media_type=media_type, headers={"Content-Disposition": f'attachment; filename="{slug}.{extension}"'})


@router.get("/report")
def report(report: Report = Depends(get_report)) -> Report:
    return report


@router.get("/export/markdown")
def export_markdown(report: Report = Depends(get_report)) -> Response:
    return _download(report.markdown.encode("utf-8"), "text/markdown; charset=utf-8", report, "md")


@router.get("/export/pdf")
async def export_pdf(report: Report = Depends(get_report)) -> Response:
    return _download(await run_in_threadpool(report_service.to_pdf, report), "application/pdf", report, "pdf")


@router.get("/export/docx")
async def export_docx(report: Report = Depends(get_report)) -> Response:
    content = await run_in_threadpool(report_service.to_docx, report)
    return _download(content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document", report, "docx")
