"""Search provider abstraction. Add a provider = subclass SearchProvider + register in PROVIDERS."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel

from ..config import Settings
from ..database import cache_get, cache_set
from ..services.cost_tracker import CostTracker
from . import tls_context

log = logging.getLogger(__name__)


class SearchError(RuntimeError):
    pass


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""
    score: float | None = None
    published_date: str | None = None
    raw_content: str | None = None  # page text supplied by the search provider, if any


class SearchProvider(ABC):
    name: str

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=30.0, verify=tls_context())

    @abstractmethod
    async def search(self, query: str, max_results: int) -> list[SearchResult]: ...

    def _check(self, response: httpx.Response) -> dict:
        status = response.status_code
        # Brave answers a malformed/invalid token with 422 SUBSCRIPTION_TOKEN_INVALID rather than 401.
        if status in (401, 403) or (status == 422 and "token" in response.text.lower()):
            raise SearchError(f"{self.name} rejected the API key - check {self.name.upper()}_API_KEY")
        if status == 429:
            raise SearchError(f"{self.name} rate limit or quota exceeded")
        if 400 <= status < 500:
            raise SearchError(f"{self.name} rejected the search request (HTTP {status})")
        response.raise_for_status()  # 5xx -> httpx.HTTPStatusError, retried by the graph's RetryPolicy
        return response.json()


class TavilySearch(SearchProvider):
    name = "tavily"

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        response = await self.client.post(
            "https://api.tavily.com/search",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"query": query, "max_results": max_results, "search_depth": "basic", "include_raw_content": "text"},
        )
        return [
            SearchResult(
                title=r.get("title") or r["url"],
                url=r["url"],
                snippet=r.get("content") or "",
                score=r.get("score"),
                published_date=r.get("published_date"),
                raw_content=r.get("raw_content"),
            )
            for r in self._check(response).get("results", [])
            if r.get("url")
        ]


class BraveSearch(SearchProvider):
    name = "brave"

    async def search(self, query: str, max_results: int) -> list[SearchResult]:
        response = await self.client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            params={"q": query, "count": min(max_results, 20)},
        )
        return [
            SearchResult(
                title=r.get("title") or r["url"],
                url=r["url"],
                snippet=r.get("description") or "",
                published_date=r.get("page_age"),
            )
            for r in (self._check(response).get("web") or {}).get("results", [])
            if r.get("url")
        ]


PROVIDERS: dict[str, type[SearchProvider]] = {"tavily": TavilySearch, "brave": BraveSearch}


def get_search_provider(settings: Settings, name: str) -> SearchProvider:
    return PROVIDERS[name](settings.search_key(name))


async def cached_search(
    provider: SearchProvider, query: str, max_results: int, tracker: CostTracker, ttl: int
) -> list[SearchResult]:
    """Search with a DB-backed cache so repeated queries cost nothing."""
    key = f"{provider.name}|{max_results}|{query.strip().lower()}"
    cached = cache_get("search", key, ttl)
    if cached is not None:
        tracker.search_cache_hits += 1
        return [SearchResult(**r) for r in cached["results"]]
    results = await provider.search(query, max_results)
    tracker.search_requests += 1
    cache_set("search", key, {"results": [r.model_dump() for r in results]}, ttl)
    return results
