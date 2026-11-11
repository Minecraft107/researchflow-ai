import type { Metrics } from '../services/api'

export const fmtNumber = (n: number | undefined) => (n ?? 0).toLocaleString('en-US')
export const fmtCost = (n: number | undefined | null) => (n === undefined || n === null ? 'n/a' : `$${n.toFixed(n < 1 ? 3 : 2)}`)
export function fmtDuration(seconds: number | undefined) {
  const s = Math.round(seconds ?? 0)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="card p-4">
      <div className="label">{label}</div>
      <div className="mt-1.5 text-2xl font-semibold text-white tabular-nums">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-muted">{hint}</div>}
    </div>
  )
}

const AGENT_NAMES: Record<string, string> = {
  planner: 'Planner',
  researcher: 'Researcher (evidence extraction)',
  critic: 'Critic',
  writer: 'Report writer',
  citation_validator: 'Citation audit',
}

export default function MetricsPanel({ metrics }: { metrics: Partial<Metrics> }) {
  if (metrics.llm_calls === undefined) return <p className="card p-6 text-sm text-muted">No usage recorded yet.</p>
  const agents = Object.entries(metrics.by_agent ?? {})
  const models = Object.entries(metrics.by_model ?? {})

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile label="Estimated cost" value={fmtCost(metrics.estimated_cost_usd)} hint={`Claude ${fmtCost(metrics.llm_cost_usd)} · search ${fmtCost(metrics.search_cost_usd)}`} />
        <Tile label="Execution time" value={fmtDuration(metrics.elapsed_seconds)} />
        <Tile label="Input tokens" value={fmtNumber(metrics.input_tokens)} />
        <Tile label="Output tokens" value={fmtNumber(metrics.output_tokens)} />
        <Tile label="Claude API calls" value={fmtNumber(metrics.llm_calls)} />
        <Tile label="Search requests" value={fmtNumber(metrics.search_requests)} hint={`${fmtNumber(metrics.search_cache_hits)} served from cache`} />
        <Tile label="Pages fetched" value={fmtNumber(metrics.page_fetches)} hint={`${fmtNumber(metrics.page_cache_hits)} served from cache`} />
        <Tile label="Total tokens" value={fmtNumber(metrics.total_tokens)} />
      </div>

      {(metrics.unpriced?.length ?? 0) > 0 && (
        <p className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
          No price is configured for {metrics.unpriced!.join(', ')}, so the cost estimate excludes it. Add it to <code className="font-mono">backend/pricing.json</code>.
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card overflow-hidden">
          <h3 className="border-b border-line px-4 py-3 text-sm font-semibold text-white">Usage by agent</h3>
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted">
              <tr>
                <th className="px-4 py-2 font-medium">Agent</th>
                <th className="px-4 py-2 text-right font-medium">Calls</th>
                <th className="px-4 py-2 text-right font-medium">Input</th>
                <th className="px-4 py-2 text-right font-medium">Output</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line tabular-nums">
              {agents.map(([agent, usage]) => (
                <tr key={agent}>
                  <td className="px-4 py-2 text-zinc-200">{AGENT_NAMES[agent] ?? agent}</td>
                  <td className="px-4 py-2 text-right text-zinc-300">{fmtNumber(usage.calls)}</td>
                  <td className="px-4 py-2 text-right text-zinc-300">{fmtNumber(usage.input_tokens)}</td>
                  <td className="px-4 py-2 text-right text-zinc-300">{fmtNumber(usage.output_tokens)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="card overflow-hidden">
          <h3 className="border-b border-line px-4 py-3 text-sm font-semibold text-white">Usage by model</h3>
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted">
              <tr>
                <th className="px-4 py-2 font-medium">Model</th>
                <th className="px-4 py-2 text-right font-medium">Input</th>
                <th className="px-4 py-2 text-right font-medium">Output</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line tabular-nums">
              {models.map(([model, usage]) => (
                <tr key={model}>
                  <td className="px-4 py-2 font-mono text-xs text-zinc-200">{model}</td>
                  <td className="px-4 py-2 text-right text-zinc-300">{fmtNumber((usage.input ?? 0) + (usage.cache_read ?? 0) + (usage.cache_write ?? 0))}</td>
                  <td className="px-4 py-2 text-right text-zinc-300">{fmtNumber(usage.output)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
      <p className="text-xs text-muted">Costs are estimates from the per-token and per-request prices in backend/pricing.json, not billing data.</p>
    </div>
  )
}
