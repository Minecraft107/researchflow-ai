# ResearchFlow AI

A multi-agent research assistant. You give it a topic; a **Planner**, a **Researcher** and a **Critic**
(orchestrated with LangGraph, powered by Claude) plan the research, search the web, read the pages,
audit the evidence, and write a structured report in which every citation points to a source that was
actually fetched.

> **Status.** The code is complete and covered by 39 automated tests, but it has **not yet been run
> against the real Claude or search APIs**: no API keys were available when it was built. The
> 20-question benchmark exists and is tested, but has no results yet. See [Test results](#test-results).

## Quick start

You need Python 3.11+, Node 20+, a Claude API key and a Tavily (or Brave) API key.

```bash
cp .env.example .env        # then put your keys in .env
```

Backend (terminal 1):

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Frontend (terminal 2):

```bash
cd frontend
npm install
npm run dev                        # http://localhost:5173, proxies /api to :8000
```

Or build the frontend once (`npm run build`) and the backend serves it at http://localhost:8000.

### API keys

| Variable | Required | Where to get it |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | https://console.anthropic.com |
| `TAVILY_API_KEY` | yes, if `SEARCH_PROVIDER=tavily` (default) | https://tavily.com |
| `BRAVE_API_KEY` | only if you use Brave | https://brave.com/search/api |

Keys are read from `.env` by the backend only. The frontend never sees them: `/api/config` returns
booleans ("configured or not"). If a key is missing the app says exactly which one and refuses to
start research; there is no mock mode.

## Architecture

```
React + TypeScript + Tailwind (Vite)
        |  REST + Server-Sent Events
FastAPI  (api/research.py, api/reports.py, api/stream.py)
        |
ResearchService  - one asyncio task per run, persists state + events, feeds SSE
        |
LangGraph workflow (graph/workflow.py, graph/nodes.py, graph/state.py)
   |            |             |             |
 Planner    Researcher      Critic     Writer + citation audit
   |            |             |             |
   +------- ClaudeLLM (llm.py): structured output, usage tracking -------+
                |
   tools/search.py (Tavily | Brave)   tools/webpage_extractor.py   tools/citation_validator.py
                |
SQLAlchemy -> SQLite (runs, agent events, cache)      services/cost_tracker.py + pricing.json
```

### How the agents communicate

The agents never talk to each other directly. Each is a LangGraph node that reads the shared, typed
`ResearchState` and returns an update to it:

| Agent | Reads | Writes |
|---|---|---|
| Planner | topic, depth | `plan` (objective, subtopics, prioritised questions, search queries), `pending_queries` |
| Researcher | `plan`, `pending_queries`, existing sources | `sources`, `evidence` (claim + verbatim quote), `page_texts`, `failed_sources` |
| Critic | `plan`, `sources`, `evidence`, `queries_run` | a `Critique` (score 0-100, gaps, unsupported claims) and new `pending_queries` |
| Writer | `plan`, verified evidence, last critique | report sections with `[n]` citations |
| Citation audit | report sections, evidence | per-claim verdicts and citation statistics |

Every agent output is a Pydantic model enforced by Claude structured outputs, so nodes exchange
validated data rather than free text.

### The LangGraph workflow

```
START -> planner -> researcher -> critic --+-- score < 80, iterations and query budget left --> additional_research --+
                                    ^      |                                                                         |
                                    +------|-------------------------------------------------------------------------+
                                           +-- otherwise --> report_generation -> citation_validation -> final_report -> END
```

- **Conditional edge** after the critic: loop back for more research, or write the report.
- **Limits**: at most `MAX_CRITIQUE_ITERATIONS` critic reviews (default 3) and `MAX_SEARCH_QUERIES`
  searches per report. If the score is still below the threshold when a limit is hit, the report is
  still written and its Limitations section states the score and the unresolved issues.
- **Parallelism**: all search queries run concurrently, pages are fetched concurrently, and evidence
  is extracted with one Claude call per research question, in parallel.
- **Retries**: every node has a `RetryPolicy` for transient failures (rate limits, 5xx, network).
  Deterministic failures (bad key, refusal, no evidence) fail the run with a clear message.
- **Checkpoints**: an in-memory checkpointer; after an unexpected error the run resumes once from
  the last completed node instead of starting over.

### Why the citations can be trusted

1. The Researcher must return a **verbatim quote** for each claim. Code checks that the quote is
   really in the fetched page text. Unverified quotes, and anything from a search snippet alone, are
   never offered to the writer.
2. The writer may only cite the numbered sources it is given. The **reference list is built by code**
   from collected sources, so a reference cannot be invented. Citation numbers that match no source
   are removed.
3. A final **citation audit** checks each cited sentence against the evidence from its sources.
   Claims that are not fully supported are listed in the report's Limitations section and in the UI.
4. Publication dates come only from page metadata or the search API. If neither has one, the date is
   shown as not stated.

## Configuration

All settings are environment variables (see `.env.example`).

| Setting | Default | Purpose |
|---|---|---|
| `CLAUDE_MODEL` | `claude-opus-5-5` | planning, critique, writing, citation audit |
| `CLAUDE_EXTRACTION_MODEL` | same as above | evidence extraction (the most calls); set a cheaper model to cut cost |
| `CLAUDE_EFFORT` | `medium` | reasoning effort |
| `SEARCH_PROVIDER` | `tavily` | `tavily` or `brave` (also selectable per run in the UI) |
| `QUALITY_THRESHOLD` | `80` | critic score needed to stop researching |
| `MAX_CRITIQUE_ITERATIONS` | `3` | cap on critic reviews |
| `MAX_SEARCH_QUERIES` | `36` | cap on searches per report |
| `MAX_PAGE_CHARS` | `12000` | page text sent to Claude per source |
| `CACHE_TTL_SECONDS` | `86400` | search/page cache lifetime; `0` disables |
| `RATE_LIMIT_PER_MINUTE` / `MAX_CONCURRENT_RUNS` | `10` / `3` | API protection |
| `DATABASE_URL` | SQLite file | any SQLAlchemy URL, e.g. PostgreSQL |

Depth presets (`backend/app/config.py`): quick = 3 questions x 1 query, 1 review; standard = 5 x 2,
up to 2 reviews; deep = 7 x 3, up to 3 reviews.

**Prices are not in code.** `backend/pricing.json` holds per-token and per-search prices. The values
shipped are defaults to verify against the providers' pricing pages; edit the file when prices
change. A model with no price entry is flagged in the UI instead of being counted as free.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/research/start` | start a run: `{"topic", "depth", "provider"}` -> `{"research_id"}` |
| GET | `/api/research/{id}` | status, plan, critiques, metrics, report |
| GET | `/api/research/{id}/agents` | per-agent activity log and outputs |
| GET | `/api/research/{id}/sources` | sources, evidence, failed fetches (`?min_relevance=`) |
| GET | `/api/research/{id}/report` | final report |
| GET | `/api/research/{id}/export/{pdf\|docx\|markdown}` | download the report |
| GET | `/api/research/{id}/stream` | Server-Sent Events: live agent progress |
| GET | `/api/research`, `/api/config`, `/health` | history, UI configuration, health check |

Interactive docs: http://localhost:8000/docs

## Cost and performance

Tracked per report and shown in the "Cost & usage" tab: input and output tokens, Claude calls (per
agent and per model), search requests, page fetches, cache hits, estimated cost, execution time.

Controls: parallel search and fetch, URL de-duplication, DB-backed cache for searches and pages, one
extraction call per question rather than per source, a hard query budget, an iteration cap, no third
critique when nothing new was researched, and a separately configurable extraction model.

## Security

- API keys only in the backend environment; never returned by any endpoint.
- Input validation (topic length and characters, enums, UUID ids) and rate limiting on run creation.
- Page fetching accepts only `http(s)`, refuses private, loopback and link-local addresses, re-checks
  every redirect, caps response size and parses only HTML or text.
- Retrieved web text is treated as untrusted data: it is wrapped in tags, every agent prompt says not
  to follow instructions inside it, outputs are schema-constrained, and quotes are verified in code.
- Raw HTML and images in report text are neutralised before PDF export; the UI renders Markdown
  without raw HTML and only makes `http(s)` links clickable.
- Unhandled errors return a generic message; details go to the server log.

## Deployment

Docker (one container; FastAPI serves the built frontend):

```bash
cp .env.example .env        # add keys
docker compose up --build   # http://localhost:8000
```

The SQLite database lives in the `researchflow-data` volume. For a public deployment put the
container behind a TLS-terminating reverse proxy with response buffering off for
`/api/research/*/stream`, add authentication at the proxy (the app has none), keep **one** uvicorn
worker (see limitations), and set `DATABASE_URL` to PostgreSQL if you need more than one writer.

The Docker image has not been built yet (Docker was not running when this was written); the compose
file passes `docker compose config`.

## Tests and benchmark

```bash
cd backend
python -m pytest -q
```

The tests replace Claude and the search API with doubles in `tests/fakes.py`; everything else is the
real code: the LangGraph loop, iteration and budget caps, de-duplication, quote verification, citation
clean-up, SSRF protection (including redirects), provider request and response parsing, cost maths,
exports, every HTTP endpoint including SSE, and the benchmark runner.

Benchmark (real APIs, costs money; start small):

```bash
cd backend
python -m benchmarks.run_benchmark --limit 2            # smoke run
python -m benchmarks.run_benchmark                      # all 20 questions, quick depth
python -m benchmarks.run_benchmark --depth standard
```

It runs the real pipeline on 20 questions across 10 domains (`benchmarks/questions.json`) and writes
`benchmarks/results/BENCHMARK_REPORT.md` plus raw JSON, updated after every question. It measures:

- **Citation accuracy**: citations map to references; references are real collected sources; URLs
  resolve (live HTTP); claims supported, both by the pipeline's own audit and by an independent judge
  that reads the cited page text.
- **Research quality**: question coverage, source reliability, completeness, quote verification rate.
- **Performance**: time, Claude calls, tokens, cost per report.
- **Agents**: planner (rubric-scored), researcher (source quality), critic (how many deliberately
  mismatched claim/quote pairs it catches when they are seeded into the evidence).

### Test results

- `pytest`: **39 passed** (Python 3.11, Windows).
- Frontend: `npm run build` passes (TypeScript strict). The landing page, live dashboard, report,
  source explorer, critic evaluation and cost views were checked in a browser against a server driven
  by the test doubles.
- Live checks without keys: real pages were fetched and parsed (text, publisher, publication date);
  invalid keys for Claude, Tavily and Brave each produce a clear error.
- **Benchmark: not run.** It needs API keys. No benchmark numbers exist, and none are claimed.

## Known limitations

- **Not yet run end to end with real APIs** (see Status). Prompts are untuned against real output.
- **PDF sources are not parsed.** With Tavily the provider's extracted text is used instead;
  otherwise only the snippet is kept, and snippets are never cited.
- **Pages are truncated** to `MAX_PAGE_CHARS`; evidence further down a long page is not seen.
- **Some sites block bots.** Wikipedia, for example, requires a contact URL or email in the user
  agent; set `FETCH_USER_AGENT`. `robots.txt` is not consulted.
- **Source reliability is a domain heuristic** (`agents/researcher.py`), not a fact-check.
- **The citation audit and the critic are Claude judging Claude's work.** Quote verification and
  reference construction are done in code; whether a quote truly supports a claim is a model judgement.
- **Single process.** Runs, the SSE feed, the rate limiter and checkpoints live in memory: use one
  worker, and a run in progress is marked failed if the server restarts.
- **No authentication or per-user data.** Anyone who can reach the API can start runs and read reports.
- **DOCX export** handles headings, lists, bold, italic and links; Markdown tables come through as text.
- **Cost figures are estimates** from `pricing.json`, not billing data.

## Project structure

```
backend/
  app/
    agents/      planner.py  researcher.py  critic.py  writer.py
    graph/       state.py  nodes.py  workflow.py
    tools/       search.py (Tavily, Brave)  webpage_extractor.py  citation_validator.py
    services/    research_service.py  report_service.py  cost_tracker.py
    api/         research.py  reports.py  stream.py (SSE)
    config.py  database.py  llm.py  schemas.py  main.py
  benchmarks/    questions.json  run_benchmark.py
  tests/
  pricing.json  requirements.txt
frontend/
  src/           pages/  components/  hooks/  services/
Dockerfile  docker-compose.yml  .env.example
```

Differences from the originally suggested layout: `database/` and `schemas/` are single files,
`api/websocket.py` is `api/stream.py` (SSE fits a one-way feed and reconnects on its own),
`tavily_search.py` and `brave_search.py` share `tools/search.py` behind one interface, and the
report writer has its own `agents/writer.py`.
