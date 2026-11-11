import { useEffect, useRef, useState } from 'react'
import AgentCard, { AGENTS } from '../components/AgentCard'
import CriticPanel from '../components/CriticPanel'
import MetricsPanel, { fmtCost, fmtDuration, fmtNumber } from '../components/MetricsPanel'
import ReportView from '../components/ReportView'
import SourceExplorer from '../components/SourceExplorer'
import { useElapsed, useResearch } from '../hooks/useResearch'
import { api } from '../services/api'
import type { AppConfig } from '../services/api'

const STAGES = [
  { key: 'planning', label: 'Plan' },
  { key: 'researching', label: 'Research' },
  { key: 'critiquing', label: 'Critique' },
  { key: 'writing', label: 'Write report' },
  { key: 'validating', label: 'Validate citations' },
  { key: 'completed', label: 'Final report' },
]

type Tab = 'progress' | 'report' | 'sources' | 'evaluation' | 'metrics'
const TABS: { key: Tab; label: string }[] = [
  { key: 'progress', label: 'Progress' },
  { key: 'report', label: 'Report' },
  { key: 'sources', label: 'Sources' },
  { key: 'evaluation', label: "Critic's evaluation" },
  { key: 'metrics', label: 'Cost & usage' },
]

function Stat({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="card px-4 py-3">
      <div className="label">{label}</div>
      <div className={`mt-1 text-xl font-semibold tabular-nums ${tone ?? 'text-white'}`}>{value}</div>
    </div>
  )
}

export default function Research({ id }: { id: string }) {
  const { run, events, error } = useResearch(id)
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [tab, setTab] = useState<Tab>('progress')
  const autoSwitched = useRef(false)
  const logRef = useRef<HTMLOListElement>(null)

  useEffect(() => {
    api.config().then(setConfig).catch(() => undefined)
  }, [])

  // Show the report once, the moment it is ready; after that the user controls the tabs.
  useEffect(() => {
    if (run?.report && !autoSwitched.current) {
      autoSwitched.current = true
      setTab('report')
    }
  }, [run?.report])

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [events.length])

  const running = run?.status === 'pending' || run?.status === 'running'
  const liveElapsed = useElapsed(run?.created_at, running)

  if (error) {
    return (
      <main className="mx-auto max-w-2xl px-5 py-20 text-center">
        <p className="text-rose-300">{error}</p>
        <a href="#/" className="btn mt-6">
          Back to start
        </a>
      </main>
    )
  }
  if (!run) return <main className="px-5 py-20 text-center text-sm text-muted">Loading research...</main>

  // Live numbers come from the newest event; the stored run is the fallback (e.g. after a reload).
  const snapshot = [...events].reverse().find((e) => e.data.snapshot)?.data.snapshot
  const metrics = (running ? snapshot?.metrics : undefined) ?? run.metrics
  const stage = (running ? snapshot?.stage : undefined) ?? run.stage
  const iteration = (running ? snapshot?.iteration : undefined) ?? run.iteration
  const sourcesCount = (running ? snapshot?.sources_count : undefined) ?? run.sources_count
  const quality = (running ? snapshot?.quality_score : undefined) ?? run.quality_score
  const threshold = config?.quality_threshold ?? 80
  const maxIterations = config?.depths[run.depth].max_iterations
  const stageIndex = STAGES.findIndex((s) => s.key === stage)
  const failed = run.status === 'failed'
  const progress = run.status === 'completed' ? 100 : Math.round((Math.max(stageIndex, 0) / (STAGES.length - 1)) * 100)

  return (
    <main className="mx-auto max-w-6xl px-5 py-8">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <a href="#/" className="text-xs text-muted transition hover:text-zinc-200">
            ← ResearchFlow AI
          </a>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight text-white">{run.topic}</h1>
          <p className="mt-1 text-xs text-muted">
            <span className="capitalize">{run.depth}</span> depth · <span className="capitalize">{run.provider}</span> search
            {config && ` · ${config.model}`}
          </p>
        </div>
        <span
          className={`pill px-3 py-1 text-sm capitalize ${
            failed ? 'border-rose-500/30 bg-rose-500/10 text-rose-300' : run.status === 'completed' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : 'border-accent/40 bg-accent/10 text-accent'
          }`}
        >
          {running && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />}
          {run.status}
        </span>
      </header>

      {failed && (
        <div role="alert" className="mb-6 rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
          <strong className="font-semibold">Research failed.</strong> {run.error}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Sources" value={fmtNumber(sourcesCount)} />
        <Stat label="Iteration" value={maxIterations ? `${iteration} / ${maxIterations}` : String(iteration)} />
        <Stat label="Quality score" value={quality === null ? '-' : `${quality}/100`} tone={quality === null ? 'text-zinc-500' : quality >= threshold ? 'text-emerald-300' : 'text-amber-300'} />
        <Stat label="Elapsed" value={fmtDuration(running ? liveElapsed : (run.metrics.elapsed_seconds ?? liveElapsed))} />
        <Stat label="Tokens" value={fmtNumber(metrics.total_tokens)} />
        <Stat label="Est. cost" value={fmtCost(metrics.estimated_cost_usd)} />
      </div>

      <div className="card mt-3 px-4 py-3">
        <div className="h-1 overflow-hidden rounded-full bg-line" role="progressbar" aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100} aria-label="Research progress">
          <div className={`h-full rounded-full transition-all duration-700 ${failed ? 'bg-rose-400' : 'bg-accent'}`} style={{ width: `${progress}%` }} />
        </div>
        <ol className="mt-3 flex flex-wrap justify-between gap-x-4 gap-y-1 text-xs">
          {STAGES.map((s, i) => (
            <li key={s.key} className={i < stageIndex || run.status === 'completed' ? 'text-zinc-400' : i === stageIndex ? 'font-semibold text-accent' : 'text-zinc-600'}>
              {s.label}
            </li>
          ))}
        </ol>
      </div>

      <nav className="mt-6 flex gap-1 overflow-x-auto border-b border-line" aria-label="Research views">
        {TABS.map((t) => {
          const disabled = t.key === 'report' && !run.report
          return (
            <button
              key={t.key}
              type="button"
              disabled={disabled}
              aria-current={tab === t.key ? 'page' : undefined}
              onClick={() => setTab(t.key)}
              className={`-mb-px cursor-pointer border-b-2 px-3.5 py-2.5 text-sm font-medium whitespace-nowrap transition disabled:cursor-not-allowed disabled:opacity-40 ${
                tab === t.key ? 'border-accent text-white' : 'border-transparent text-muted hover:text-zinc-200'
              }`}
            >
              {t.label}
            </button>
          )
        })}
      </nav>

      <div className="mt-5">
        {tab === 'progress' && (
          <div className="space-y-4">
            <div className="grid gap-4 md:grid-cols-3">
              {AGENTS.map((spec) => (
                <AgentCard key={spec.key} spec={spec} events={events} runFailed={failed} />
              ))}
            </div>

            <section className="card">
              <h2 className="border-b border-line px-4 py-3 text-sm font-semibold text-white">Activity</h2>
              <ol ref={logRef} className="max-h-80 space-y-1.5 overflow-y-auto px-4 py-3 font-mono text-xs leading-5">
                {events.length === 0 && <li className="text-muted">Waiting for the first event...</li>}
                {events.map((event) => (
                  <li key={event.id} className="flex gap-3">
                    <time className="shrink-0 text-zinc-600">{new Date(event.created_at).toLocaleTimeString()}</time>
                    <span className="w-24 shrink-0 text-accent">{event.agent}</span>
                    <span className={event.type === 'failed' ? 'text-rose-300' : event.type === 'completed' ? 'text-zinc-200' : 'text-zinc-400'}>{event.message}</span>
                  </li>
                ))}
              </ol>
            </section>

            {run.plan && (
              <section className="card p-5">
                <h2 className="text-sm font-semibold text-white">Research plan</h2>
                <p className="mt-2 text-sm leading-6 text-zinc-300">{run.plan.objective}</p>
                <ol className="mt-4 space-y-3">
                  {run.plan.questions.map((q) => (
                    <li key={q.id} className="rounded-lg border border-line bg-ink/40 p-3">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        <span className="font-mono text-accent">{q.id}</span>
                        <span className="pill capitalize text-zinc-400">{q.priority} priority</span>
                        <span className="text-muted">{q.subtopic}</span>
                      </div>
                      <p className="mt-1.5 text-sm text-zinc-200">{q.question}</p>
                      <p className="mt-1 text-xs text-muted">Evidence needed: {q.required_evidence}</p>
                      <p className="mt-1 text-xs text-zinc-500">Searches: {q.search_queries.join(' · ')}</p>
                    </li>
                  ))}
                </ol>
                {run.plan.information_gaps.length > 0 && <p className="mt-4 text-xs text-muted">Anticipated gaps: {run.plan.information_gaps.join('; ')}</p>}
              </section>
            )}
          </div>
        )}
        {tab === 'report' && run.report && <ReportView id={id} report={run.report} />}
        {tab === 'sources' && <SourceExplorer id={id} plan={run.plan} refreshKey={sourcesCount + (run.report ? 1 : 0)} />}
        {tab === 'evaluation' && <CriticPanel critiques={run.critiques} report={run.report} threshold={threshold} />}
        {tab === 'metrics' && <MetricsPanel metrics={metrics} />}
      </div>
    </main>
  )
}
