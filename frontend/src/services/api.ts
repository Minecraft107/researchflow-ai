// Typed client for the FastAPI backend. All requests are same-origin (/api); no keys live here.

export type Depth = 'quick' | 'standard' | 'deep'
export type Provider = 'tavily' | 'brave'

export interface DepthPreset {
  questions: number
  queries_per_question: number
  sources_per_question: number
  max_iterations: number
}

export interface AppConfig {
  app_name: string
  default_provider: Provider
  providers: Record<Provider, { configured: boolean }>
  anthropic_configured: boolean
  model: string
  extraction_model: string
  depths: Record<Depth, DepthPreset>
  quality_threshold: number
  max_search_queries: number
}

export interface Metrics {
  input_tokens: number
  output_tokens: number
  total_tokens: number
  llm_calls: number
  search_requests: number
  search_cache_hits: number
  page_fetches: number
  page_cache_hits: number
  llm_cost_usd: number
  search_cost_usd: number | null
  estimated_cost_usd: number
  unpriced: string[]
  elapsed_seconds: number
  by_model: Record<string, Record<string, number>>
  by_agent: Record<string, { calls: number; input_tokens: number; output_tokens: number }>
}

export interface ResearchQuestion {
  id: string
  subtopic: string
  question: string
  priority: 'high' | 'medium' | 'low'
  required_evidence: string
  search_queries: string[]
}

export interface Plan {
  objective: string
  subtopics: string[]
  questions: ResearchQuestion[]
  information_gaps: string[]
}

export interface Critique {
  quality_score: number
  coverage_score: number
  evidence_score: number
  reliability_score: number
  summary: string
  unanswered_questions: string[]
  unsupported_claims: { evidence_id: string | null; claim: string; reason: string }[]
  contradictions: string[]
  missing_perspectives: string[]
  outdated_information: string[]
  reliability_concerns: string[]
  additional_queries: { question_id: string; query: string }[]
}

export interface Reference {
  number: number
  source_id: string
  title: string
  url: string
  publisher: string | null
  published_date: string | null
  retrieved_date: string
}

export interface CitationCheck {
  claim: string
  citations: number[]
  verdict: 'supported' | 'partially_supported' | 'unsupported' | 'not_checked'
  reason: string
}

export interface Report {
  title: string
  sections: { heading: string; content: string }[]
  references: Reference[]
  markdown: string
  citation_checks: CitationCheck[]
  citation_stats: Record<string, number | null>
  quality_score: number | null
  threshold_met: boolean
}

export interface RunSummary {
  research_id: string
  topic: string
  depth: Depth
  provider: Provider
  status: 'pending' | 'running' | 'completed' | 'failed'
  stage: string
  iteration: number
  quality_score: number | null
  sources_count: number
  error: string | null
  created_at: string
  completed_at: string | null
}

export interface Run extends RunSummary {
  plan: Plan | null
  critiques: Critique[]
  metrics: Partial<Metrics>
  evidence_count: number
  failed_sources_count: number
  report: Report | null
}

export interface Snapshot {
  stage: string
  iteration: number
  sources_count: number
  quality_score: number | null
  metrics: Metrics
}

export interface AgentEvent {
  id: number
  agent: string
  type: 'started' | 'progress' | 'completed' | 'failed'
  message: string
  data: { step?: string; snapshot?: Snapshot }
  created_at: string
}

export interface Source {
  id: string
  title: string
  url: string
  publisher: string | null
  published_date: string | null
  retrieved_date: string
  relevance_score: number
  reliability: 'high' | 'medium' | 'unknown'
  source_type: string
  content_status: 'full' | 'search_provider_content' | 'snippet_only'
  fetch_error: string | null
  snippet: string
  question_ids: string[]
  key_findings: string[]
  supporting_claims: string[]
  citation_number: number | null
}

export interface Evidence {
  id: string
  source_id: string
  question_id: string
  claim: string
  quote: string
  quote_verified: boolean
}

export interface SourcesResponse {
  sources: Source[]
  evidence: Evidence[]
  failed_sources: { url: string; title: string; error: string; fallback: string }[]
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init)
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try {
      const body = await response.json()
      if (typeof body.detail === 'string') detail = body.detail
      else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg: string }) => d.msg).join('; ')
    } catch {
      /* non-JSON error body: keep the generic message */
    }
    throw new Error(detail)
  }
  return response.json() as Promise<T>
}

export const api = {
  config: () => request<AppConfig>('/config'),
  list: () => request<RunSummary[]>('/research'),
  start: (topic: string, depth: Depth, provider: Provider) =>
    request<{ research_id: string }>('/research/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ topic, depth, provider }),
    }),
  run: (id: string) => request<Run>(`/research/${id}`),
  sources: (id: string) => request<SourcesResponse>(`/research/${id}/sources`),
  streamUrl: (id: string) => `/api/research/${id}/stream`,
  exportUrl: (id: string, format: 'pdf' | 'docx' | 'markdown') => `/api/research/${id}/export/${format}`,
}

/** Only http(s) links are ever rendered as clickable. */
export function safeUrl(url: string): string | undefined {
  return /^https?:\/\//i.test(url) ? url : undefined
}
