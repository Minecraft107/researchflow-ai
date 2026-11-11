import { useEffect, useMemo, useState } from 'react'
import { api, safeUrl } from '../services/api'
import type { Plan, SourcesResponse } from '../services/api'

const RELIABILITY_STYLE = {
  high: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
  medium: 'border-sky-500/30 bg-sky-500/10 text-sky-300',
  unknown: 'text-zinc-400',
}
const CONTENT_LABEL = {
  full: 'Full page read',
  search_provider_content: 'Text from search provider',
  snippet_only: 'Snippet only - not citable',
}

export default function SourceExplorer({ id, plan, refreshKey }: { id: string; plan: Plan | null; refreshKey: number }) {
  const [data, setData] = useState<SourcesResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [minRelevance, setMinRelevance] = useState(0)
  const [citedOnly, setCitedOnly] = useState(false)
  const [open, setOpen] = useState<string | null>(null)

  useEffect(() => {
    api
      .sources(id)
      .then(setData)
      .catch((e: Error) => setError(e.message))
  }, [id, refreshKey])

  const questions = useMemo(() => Object.fromEntries((plan?.questions ?? []).map((q) => [q.id, q.question])), [plan])
  const visible = useMemo(
    () =>
      (data?.sources ?? [])
        .filter((s) => s.relevance_score >= minRelevance && (!citedOnly || s.citation_number !== null))
        .sort((a, b) => b.relevance_score - a.relevance_score),
    [data, minRelevance, citedOnly],
  )

  if (error) return <p className="card p-6 text-sm text-rose-300">{error}</p>
  if (!data) return <p className="card p-6 text-sm text-muted">Loading sources...</p>
  if (data.sources.length === 0) return <p className="card p-6 text-sm text-muted">No sources collected yet.</p>

  return (
    <div>
      <div className="card mb-4 flex flex-wrap items-center gap-x-8 gap-y-3 px-4 py-3">
        <label className="flex items-center gap-3 text-sm text-zinc-300">
          Minimum relevance
          <input type="range" min={0} max={1} step={0.05} value={minRelevance} onChange={(e) => setMinRelevance(Number(e.target.value))} className="accent-accent" />
          <span className="w-9 font-mono text-xs text-muted">{minRelevance.toFixed(2)}</span>
        </label>
        <label className="flex cursor-pointer items-center gap-2 text-sm text-zinc-300">
          <input type="checkbox" checked={citedOnly} onChange={(e) => setCitedOnly(e.target.checked)} className="accent-accent" />
          Cited in report only
        </label>
        <span className="ml-auto text-xs text-muted">
          Showing {visible.length} of {data.sources.length} sources
        </span>
      </div>

      <ul className="space-y-3">
        {visible.map((source) => {
          const evidence = data.evidence.filter((e) => e.source_id === source.id)
          const expanded = open === source.id
          return (
            <li key={source.id} className="card overflow-hidden">
              <div className="flex items-start gap-4 p-4">
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    {source.citation_number !== null && <span className="pill border-accent/40 bg-accent/10 text-accent">Cited as [{source.citation_number}]</span>}
                    <span className={`pill capitalize ${RELIABILITY_STYLE[source.reliability]}`}>
                      {source.reliability === 'unknown' ? 'Unrated source' : `${source.reliability} reliability`}
                    </span>
                    <span className={`pill ${source.content_status === 'snippet_only' ? 'border-amber-500/30 text-amber-300' : 'text-zinc-400'}`}>
                      {CONTENT_LABEL[source.content_status]}
                    </span>
                  </div>
                  <a
                    href={safeUrl(source.url)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-2 block font-medium text-zinc-100 underline decoration-transparent underline-offset-2 transition hover:decoration-accent"
                  >
                    {source.title}
                  </a>
                  <p className="mt-0.5 truncate text-xs text-muted">
                    {[source.publisher, source.published_date ? `published ${source.published_date}` : 'publication date not stated', `retrieved ${source.retrieved_date}`]
                      .filter(Boolean)
                      .join(' · ')}
                  </p>
                </div>
                <div className="w-24 shrink-0 text-right">
                  <div className="font-mono text-sm text-white">{source.relevance_score.toFixed(2)}</div>
                  <div className="mt-1 h-1 overflow-hidden rounded-full bg-line" role="img" aria-label={`Relevance ${source.relevance_score.toFixed(2)}`}>
                    <div className="h-full rounded-full bg-accent" style={{ width: `${source.relevance_score * 100}%` }} />
                  </div>
                  <div className="mt-1 text-[11px] text-muted">relevance</div>
                </div>
              </div>

              <button
                type="button"
                aria-expanded={expanded}
                onClick={() => setOpen(expanded ? null : source.id)}
                className="flex w-full cursor-pointer items-center justify-between border-t border-line px-4 py-2 text-xs text-muted transition hover:bg-raised hover:text-zinc-200"
              >
                <span>
                  {source.key_findings.length} findings · {evidence.length} evidence items · supports {source.question_ids.join(', ')}
                </span>
                <span>{expanded ? 'Hide' : 'Show'}</span>
              </button>

              {expanded && (
                <div className="space-y-5 border-t border-line bg-ink/40 p-4 text-sm">
                  {source.fetch_error && <p className="text-xs text-amber-300">Page could not be fetched directly: {source.fetch_error}</p>}
                  {source.key_findings.length > 0 && (
                    <div>
                      <h4 className="label mb-1.5">Key findings</h4>
                      <ul className="list-disc space-y-1 pl-5 text-zinc-300">
                        {source.key_findings.map((finding, i) => (
                          <li key={i}>{finding}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <div>
                    <h4 className="label mb-1.5">Claims this source supports</h4>
                    {evidence.length === 0 && <p className="text-muted">No evidence was extracted from this source.</p>}
                    <ul className="space-y-3">
                      {evidence.map((item) => (
                        <li key={item.id} className="rounded-lg border border-line bg-panel p-3">
                          <div className="flex flex-wrap items-center gap-2 text-xs">
                            <span className="font-mono text-muted">{item.id}</span>
                            <span className={`pill ${item.quote_verified ? 'border-emerald-500/30 text-emerald-300' : 'border-amber-500/30 text-amber-300'}`}>
                              {item.quote_verified ? 'Quote found in source' : 'Quote not verified - excluded from report'}
                            </span>
                            <span className="text-muted" title={questions[item.question_id]}>
                              for {item.question_id}
                            </span>
                          </div>
                          <p className="mt-2 text-zinc-200">{item.claim}</p>
                          <blockquote className="mt-2 border-l-2 border-line pl-3 text-zinc-400 italic">"{item.quote}"</blockquote>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}
            </li>
          )
        })}
      </ul>

      {data.failed_sources.length > 0 && (
        <details className="card mt-4 px-4 py-3 text-sm">
          <summary className="cursor-pointer text-zinc-300">{data.failed_sources.length} pages could not be fetched</summary>
          <ul className="mt-3 space-y-1.5 text-xs text-muted">
            {data.failed_sources.map((failed, i) => (
              <li key={i} className="truncate">
                <span className="text-amber-300">{failed.error}</span> · {failed.url}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
