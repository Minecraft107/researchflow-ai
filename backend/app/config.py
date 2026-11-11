"""Application configuration. Everything comes from environment variables / .env."""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent

Depth = Literal["quick", "standard", "deep"]
ProviderName = Literal["tavily", "brave"]

# Research depth presets: how much work each depth level is allowed to do.
DEPTH_PRESETS: dict[str, dict[str, int]] = {
    "quick": {"questions": 3, "queries_per_question": 1, "sources_per_question": 3, "max_iterations": 1},
    "standard": {"questions": 5, "queries_per_question": 2, "sources_per_question": 4, "max_iterations": 2},
    "deep": {"questions": 7, "queries_per_question": 3, "sources_per_question": 5, "max_iterations": 3},
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(ROOT_DIR / ".env", BACKEND_DIR / ".env"), extra="ignore")

    # --- API keys (never sent to the frontend) ---
    anthropic_api_key: str = ""
    tavily_api_key: str = ""
    brave_api_key: str = ""

    # --- Models ---
    claude_model: str = "claude-opus-5-5"
    claude_extraction_model: str = ""  # evidence extraction (highest volume); empty = claude_model
    claude_effort: str = "medium"  # low|medium|high|xhigh|max; empty = omit (for models without effort)
    claude_refusal_fallback: bool = True  # server-side fallback when a safety classifier declines

    # --- Search ---
    search_provider: ProviderName = "tavily"
    max_search_queries: int = 36  # hard cap per report, across all iterations
    max_results_per_query: int = 6

    # --- Workflow limits ---
    quality_threshold: int = 80
    max_critique_iterations: int = 3
    max_page_chars: int = 12000  # per-source text sent to Claude; the rest of the page is not read
    llm_concurrency: int = 4
    fetch_concurrency: int = 8
    fetch_timeout_seconds: float = 15.0
    # Identify the bot honestly; some sites (e.g. Wikipedia) reject browser-like or anonymous agents.
    # For production add a contact URL or email, e.g. "MyOrgResearch/1.0 (+https://myorg.example/bot)".
    fetch_user_agent: str = "ResearchFlowAI/1.0 (research assistant bot; python-httpx)"

    # --- Storage / cache ---
    database_url: str = f"sqlite:///{(BACKEND_DIR / 'researchflow.db').as_posix()}"
    cache_ttl_seconds: int = 86400
    pricing_file: str = str(BACKEND_DIR / "pricing.json")

    # --- API hardening ---
    rate_limit_per_minute: int = 10
    max_concurrent_runs: int = 3
    cors_origins: str = "http://localhost:5173"
    log_level: str = "INFO"

    @property
    def extraction_model(self) -> str:
        return self.claude_extraction_model or self.claude_model

    def search_key(self, provider: str) -> str:
        return self.tavily_api_key if provider == "tavily" else self.brave_api_key

    def missing_keys(self, provider: str) -> list[str]:
        missing = []
        if not self.anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if not self.search_key(provider):
            missing.append("TAVILY_API_KEY" if provider == "tavily" else "BRAVE_API_KEY")
        return missing

    def preset(self, depth: str) -> dict[str, int]:
        preset = dict(DEPTH_PRESETS[depth])
        preset["max_iterations"] = min(preset["max_iterations"], self.max_critique_iterations)
        return preset

    def load_pricing(self) -> dict:
        """Prices live in pricing.json so they can be updated without touching code."""
        try:
            return json.loads(Path(self.pricing_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).warning("Could not read pricing file %s: %s", self.pricing_file, exc)
            return {"models": {}, "search": {}}


@lru_cache
def get_settings() -> Settings:
    return Settings()


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.ERROR)
