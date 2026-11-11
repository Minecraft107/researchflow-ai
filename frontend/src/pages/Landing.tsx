import { useEffect, useState } from 'react'
import { navigate } from '../App'
import { api } from '../services/api'
import type { AppConfig, Depth, Provider, RunSummary } from '../services/api'

const EXAMPLES = [
  'Impact of artificial intelligence on healthcare',
  'How effective are carbon pricing policies at reducing emissions?',
  'State of post-quantum cryptography adoption',
  'Effects of remote work on productivity and wages',
]

const DEPTH_LABELS: Record<Depth, string> = { quick: 'Quick', standard: 'Standard', deep: 'Deep' }

const STATUS_STYLE: Record<RunSummary['status'], string> = {
  pending: 'text-zinc-400',
  running: 'text-accent',
  completed: 'text-emerald-400',
  failed: 'text-rose-400',
}

export default function Landing() {
  const [config, setConfig] = useState<AppConfig | null>(null)
  const [recent, setRecent] = useState<RunSummary[]>([])
  const [topic, setTopic] = useState('')
  const [depth, setDepth] = useState<Depth>('standard')
  const [provider, setProvider] = useState<Provider>('tavily')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .config()
      .then((c) => {
        setConfig(c)
        setProvider(c.default_provider)
      })
      .catch((e: Error) => setError(`Cannot reach the API: ${e.message}`))
    api.list().then(setRecent).catch(() => undefined)
  }, [])

  const missing = config
    ? [
        !config.anthropic_configured && 'ANTHROPIC_API_KEY',
        !config.providers[provider].configured && (provider === 'tavily' ? 'TAVILY_API_KEY' : 'BRAVE_API_KEY'),
      ].filter(Boolean)
    : []
  const canStart = topic.trim().length >= 5 && !busy && config !== null && missing.length === 0

  async function start(event: React.FormEvent) {
    event.preventDefault()
    if (!canStart) return
    setBusy(true)
    setError(null)
    try {
      const { research_id } = await api.start(topic.trim(), depth, provider)
      navigate(`/research/${research_id}`)
    } catch (e) {
      setError((e as Error).message)
      setBusy(false)
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col px-5 py-14">
      <header className="mb-10 text-center">
        <div className="mx-auto mb-5 flex h-11 w-11 items-center justify-center rounded-xl bg-accent text-lg font-bold text-ink">R</div>
        <h1 className="text-4xl font-semibold tracking-tight text-white">ResearchFlow AI</h1>
        <p className="mx-auto mt-3 max-w-xl text-[15px] leading-6 text-muted">
          A Planner, a Researcher and a Critic work through your question together and return a report in which every claim is tied to a
          source you can open.
        </p>
      </header>

      <form onSubmit={start} className="card p-4 shadow-2xl shadow-black/40">
        <label htmlFor="topic" className="sr-only">
          Research topic
        </label>
        <textarea
          id="topic"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) start(e)
          }}
          maxLength={500}
          rows={3}
          placeholder="What do you want researched?"
          className="w-full resize-none bg-transparent px-2 py-1 text-lg text-white placeholder:text-zinc-600 focus:outline-none"
        />
        <div className="mt-3 flex flex-wrap items-end justify-between gap-4 border-t border-line pt-4">
          <fieldset>
            <legend className="label mb-1.5">Research depth</legend>
            <div className="flex gap-1 rounded-lg border border-line bg-ink p-1">
              {(Object.keys(DEPTH_LABELS) as Depth[]).map((d) => (
                <button
                  key={d}
                  type="button"
                  aria-pressed={depth === d}
                  onClick={() => setDepth(d)}
                  className={`cursor-pointer rounded-md px-3 py-1.5 text-sm font-medium transition ${depth === d ? 'bg-raised text-white' : 'text-muted hover:text-zinc-200'}`}
                >
                  {DEPTH_LABELS[d]}
                </button>
              ))}
            </div>
          </fieldset>

          <fieldset>
            <legend className="label mb-1.5">Search provider</legend>
            <div className="flex gap-1 rounded-lg border border-line bg-ink p-1">
              {(['tavily', 'brave'] as Provider[]).map((p) => (
                <button
                  key={p}
                  type="button"
                  aria-pressed={provider === p}
                  onClick={() => setProvider(p)}
                  className={`flex cursor-pointer items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium capitalize transition ${provider === p ? 'bg-raised text-white' : 'text-muted hover:text-zinc-200'}`}
                >
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${config?.providers[p].configured ? 'bg-emerald-400' : 'bg-zinc-600'}`}
                    title={config?.providers[p].configured ? 'API key configured' : 'API key not configured'}
                  />
                  {p}
                </button>
              ))}
            </div>
          </fieldset>

          <button type="submit" disabled={!canStart} className="btn btn-primary ml-auto px-5 py-2.5">
            {busy ? 'Starting...' : 'Start research'}
          </button>
        </div>
        {config && (
          <p className="mt-3 px-1 text-xs text-muted">
            {DEPTH_LABELS[depth]}: {config.depths[depth].questions} research questions, {config.depths[depth].queries_per_question} search{' '}
            {config.depths[depth].queries_per_question === 1 ? 'query' : 'queries'} each, up to {config.depths[depth].max_iterations} critic{' '}
            {config.depths[depth].max_iterations === 1 ? 'review' : 'reviews'}. Model: {config.model}.
          </p>
        )}
      </form>

      {missing.length > 0 && (
        <div role="alert" className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
          Research can't start yet. Missing in <code className="font-mono">.env</code>:{' '}
          <span className="font-mono font-semibold">{missing.join(', ')}</span>. Add {missing.length === 1 ? 'it' : 'them'} and restart the
          backend.
        </div>
      )}
      {error && (
        <div role="alert" className="mt-4 rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
          {error}
        </div>
      )}

      <section className="mt-8">
        <h2 className="label mb-2.5">Try an example</h2>
        <div className="flex flex-wrap gap-2">
          {EXAMPLES.map((example) => (
            <button key={example} type="button" onClick={() => setTopic(example)} className="pill cursor-pointer py-1.5 text-zinc-300 transition hover:border-zinc-500 hover:text-white">
              {example}
            </button>
          ))}
        </div>
      </section>

      {recent.length > 0 && (
        <section className="mt-10">
          <h2 className="label mb-2.5">Recent research</h2>
          <ul className="card divide-y divide-line">
            {recent.slice(0, 8).map((run) => (
              <li key={run.research_id}>
                <a href={`#/research/${run.research_id}`} className="flex items-center justify-between gap-4 px-4 py-3 transition hover:bg-raised">
                  <span className="truncate text-sm text-zinc-200">{run.topic}</span>
                  <span className="flex shrink-0 items-center gap-3 text-xs">
                    {run.quality_score !== null && <span className="text-muted">score {run.quality_score}</span>}
                    <span className={`capitalize ${STATUS_STYLE[run.status]}`}>{run.status}</span>
                  </span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  )
}
