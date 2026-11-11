"""HTTP API tests through FastAPI's TestClient."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.services.research_service import ResearchService

from .fakes import FakeLLM, FakeSearch, fake_fetch_page


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def ready_client(client, monkeypatch):
    """Client whose service uses fake Claude/search, with keys 'configured'."""
    settings = get_settings()
    monkeypatch.setattr(settings, "anthropic_api_key", "test")
    monkeypatch.setattr(settings, "tavily_api_key", "test")
    monkeypatch.setattr("app.agents.researcher.fetch_page", fake_fetch_page)
    app.state.service = ResearchService(settings, llm_factory=lambda s, t: FakeLLM(t, [60, 90]), search_factory=lambda s, p: FakeSearch())
    return client


def test_config_reports_key_status_without_leaking_keys(client):
    body = client.get("/api/config").json()
    assert body["anthropic_configured"] is False and body["providers"] == {"tavily": {"configured": False}, "brave": {"configured": False}}
    assert set(body["depths"]) == {"quick", "standard", "deep"}
    assert "key" not in json.dumps(body).lower().replace("api_key", "")


def test_start_without_keys_is_refused_with_the_exact_missing_keys(client):
    response = client.post("/api/research/start", json={"topic": "Impact of AI on healthcare"})
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"] and "TAVILY_API_KEY" in response.json()["detail"]
    response = client.post("/api/research/start", json={"topic": "Impact of AI on healthcare", "provider": "brave"})
    assert "BRAVE_API_KEY" in response.json()["detail"]


def test_input_validation_and_unknown_ids(client):
    assert client.post("/api/research/start", json={"topic": "hi"}).status_code == 422
    assert client.post("/api/research/start", json={"topic": "valid topic", "depth": "x"}).status_code == 422
    assert client.get("/api/research/not-a-uuid").status_code == 422
    assert client.get("/api/research/00000000-0000-0000-0000-000000000000").status_code == 404
    assert client.get("/api/research/00000000-0000-0000-0000-000000000000/export/pdf").status_code == 404


def test_full_run_over_http(ready_client):
    client = ready_client
    started = client.post("/api/research/start", json={"topic": "Impact of AI on healthcare", "depth": "standard"})
    assert started.status_code == 202
    rid = started.json()["research_id"]

    deadline = time.time() + 30
    while (status := client.get(f"/api/research/{rid}").json())["status"] not in ("completed", "failed"):
        assert time.time() < deadline, "run did not finish"
        time.sleep(0.05)
    assert status["status"] == "completed", status["error"]
    assert status["iteration"] == 2 and status["quality_score"] == 90 and status["plan"]["questions"]
    assert status["metrics"]["llm_calls"] > 0 and status["report"]["title"] == "Test Report"

    agents = client.get(f"/api/research/{rid}/agents").json()["agents"]
    assert agents["planner"]["status"] == "done" and agents["planner"]["output"]["objective"]
    assert len(agents["critic"]["output"]["critiques"]) == 2 and agents["researcher"]["events"]

    sources = client.get(f"/api/research/{rid}/sources").json()
    assert sources["sources"] and sources["evidence"] and any(s["citation_number"] == 1 for s in sources["sources"])
    assert client.get(f"/api/research/{rid}/sources", params={"min_relevance": 0.95}).json()["sources"] == []

    assert client.get(f"/api/research/{rid}/report").json()["references"]
    markdown = client.get(f"/api/research/{rid}/export/markdown")
    assert markdown.text.startswith("# Test Report") and "attachment" in markdown.headers["content-disposition"]
    assert client.get(f"/api/research/{rid}/export/pdf").content.startswith(b"%PDF")
    assert client.get(f"/api/research/{rid}/export/docx").content.startswith(b"PK")

    # SSE replays the whole run for a late subscriber and then signals completion.
    stream = client.get(f"/api/research/{rid}/stream").text
    assert "event: agent" in stream and stream.rstrip().endswith('data: {"status": "completed"}')
    resumed = client.get(f"/api/research/{rid}/stream", headers={"Last-Event-ID": "999999"}).text
    assert "event: agent" not in resumed and "event: done" in resumed

    assert any(r["research_id"] == rid for r in client.get("/api/research").json())


def test_report_endpoints_before_completion_return_409(ready_client):
    from app.database import ResearchRun, SessionLocal

    with SessionLocal() as db:
        db.add(ResearchRun(id="11111111-1111-1111-1111-111111111111", topic="t", depth="quick", provider="tavily", status="running"))
        db.merge(ResearchRun(id="11111111-1111-1111-1111-111111111111", topic="t", depth="quick", provider="tavily", status="running"))
        db.commit()
    assert ready_client.get("/api/research/11111111-1111-1111-1111-111111111111/report").status_code == 409


def test_rate_limit(ready_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_per_minute", 2)
    from app.api import research

    research._hits.clear()
    codes = [ready_client.post("/api/research/start", json={"topic": "hi"}).status_code for _ in range(4)]
    assert codes == [422, 422, 429, 429]
    research._hits.clear()
