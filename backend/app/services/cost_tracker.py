"""Per-report token, request and cost accounting. Prices come from pricing.json, never from code."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any


class CostTracker:
    def __init__(self, pricing: dict, search_provider: str):
        self.pricing = pricing
        self.search_provider = search_provider
        self.started = time.monotonic()
        self.elapsed_final: float | None = None
        self.llm_calls = 0
        self.search_requests = 0
        self.search_cache_hits = 0
        self.page_fetches = 0
        self.page_cache_hits = 0
        # model -> token counters; agent -> {calls, input, output}
        self.by_model: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.by_agent: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def record_llm(self, agent: str, model: str, usage: Any) -> None:
        """`usage` is the Anthropic response.usage object."""
        counts = {
            "input": getattr(usage, "input_tokens", 0) or 0,
            "output": getattr(usage, "output_tokens", 0) or 0,
            "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }
        self.llm_calls += 1
        for key, value in counts.items():
            self.by_model[model][key] += value
        self.by_agent[agent]["calls"] += 1
        self.by_agent[agent]["input_tokens"] += counts["input"] + counts["cache_read"] + counts["cache_write"]
        self.by_agent[agent]["output_tokens"] += counts["output"]

    def stop(self) -> None:
        self.elapsed_final = time.monotonic() - self.started

    def _llm_cost(self) -> tuple[float, list[str]]:
        total, unpriced = 0.0, []
        for model, counts in self.by_model.items():
            price = self.pricing.get("models", {}).get(model)
            if price is None:
                unpriced.append(model)
                continue
            total += sum(counts[k] * price.get(k, 0.0) for k in ("input", "output", "cache_read", "cache_write")) / 1e6
        return total, unpriced

    def snapshot(self) -> dict:
        llm_cost, unpriced = self._llm_cost()
        search_price = self.pricing.get("search", {}).get(self.search_provider)
        search_cost = self.search_requests * search_price if search_price is not None else None
        input_tokens = sum(c["input"] + c["cache_read"] + c["cache_write"] for c in self.by_model.values())
        output_tokens = sum(c["output"] for c in self.by_model.values())
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "llm_calls": self.llm_calls,
            "search_requests": self.search_requests,
            "search_cache_hits": self.search_cache_hits,
            "page_fetches": self.page_fetches,
            "page_cache_hits": self.page_cache_hits,
            "llm_cost_usd": round(llm_cost, 4),
            "search_cost_usd": round(search_cost, 4) if search_cost is not None else None,
            "estimated_cost_usd": round(llm_cost + (search_cost or 0.0), 4),
            # Models/providers with no entry in pricing.json are listed so the estimate is never silently low.
            "unpriced": unpriced + ([] if search_price is not None else [self.search_provider]),
            "elapsed_seconds": round(self.elapsed_final if self.elapsed_final is not None else time.monotonic() - self.started, 1),
            "by_model": {m: dict(c) for m, c in self.by_model.items()},
            "by_agent": {a: dict(c) for a, c in self.by_agent.items()},
        }
