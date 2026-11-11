import type { Critique, Report } from '../services/api'

const VERDICT_STYLE = {
  supported: 'border-emerald-500/30 text-emerald-300',
  partially_supported: 'border-amber-500/30 text-amber-300',
  unsupported: 'border-rose-500/30 text-rose-300',
  not_checked: 'text-zinc-400',
}

function Score({ label, value }: { label: string; value: number }) {
  return (
    <div>
      <div className="flex justify-between text-xs text-muted">
        <span>{label}</span>
        <span className="font-mono text-zinc-200">{value}</span>
      </div>
      <div className="mt-1 h-1 overflow-hidden rounded-full bg-line">
        <div className="h-full rounded-full bg-accent" style={{ width: `${value}%` }} />
      </div>
    </div>
  )
}

function Issues({ title, items }: { title: string; items: string[] }) {
  if (items.length === 0) return null
  return (
    <div>
      <h4 className="label mb-1.5">{title}</h4>
      <ul className="list-disc space-y-1 pl-5 text-sm text-zinc-300">
        {items.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ul>
    </div>
  )
}

export default function CriticPanel({ critiques, report, threshold }: { critiques: Critique[]; report: Report | null; threshold: number }) {
  if (critiques.length === 0) return <p className="card p-6 text-sm text-muted">The Critic has not reviewed the research yet.</p>
  const stats = report?.citation_stats
  const accuracy = stats?.citation_accuracy

  return (
    <div className="space-y-4">
      {critiques.map((c, i) => (
        <section key={i} className="card p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 className="font-semibold text-white">
              Review {i + 1} of {critiques.length}
            </h3>
            <span className={`pill text-sm ${c.quality_score >= threshold ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : 'border-amber-500/30 bg-amber-500/10 text-amber-300'}`}>
              Quality {c.quality_score}/100 · target {threshold}
            </span>
          </div>
          <p className="mt-3 text-sm leading-6 text-zinc-300">{c.summary}</p>
          <div className="mt-4 grid gap-4 sm:grid-cols-3">
            <Score label="Coverage" value={c.coverage_score} />
            <Score label="Evidence support" value={c.evidence_score} />
            <Score label="Source reliability" value={c.reliability_score} />
          </div>
          <div className="mt-5 grid gap-5 sm:grid-cols-2">
            <Issues title="Unanswered questions" items={c.unanswered_questions} />
            <Issues title="Unsupported claims" items={c.unsupported_claims.map((u) => `${u.evidence_id ? `${u.evidence_id}: ` : ''}${u.claim} - ${u.reason}`)} />
            <Issues title="Contradictions" items={c.contradictions} />
            <Issues title="Missing perspectives" items={c.missing_perspectives} />
            <Issues title="Outdated information" items={c.outdated_information} />
            <Issues title="Reliability concerns" items={c.reliability_concerns} />
            <Issues title="Follow-up searches requested" items={c.additional_queries.map((q) => `${q.question_id}: ${q.query}`)} />
          </div>
        </section>
      ))}

      {report && stats && (
        <section className="card p-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h3 className="font-semibold text-white">Citation audit of the final report</h3>
            <span className="pill text-sm text-zinc-200">
              {typeof accuracy === 'number' ? `${Math.round(accuracy * 100)}% of cited claims fully supported` : 'No cited claims to check'}
            </span>
          </div>
          <p className="mt-2 text-xs text-muted">
            {stats.total_citations} citations to {report.references.length} sources · {stats.invalid_citations_removed} invalid citation markers
            removed · {stats.claims_checked} claims checked against the evidence collected from their cited sources
          </p>
          <ul className="mt-4 space-y-2">
            {report.citation_checks.map((check, i) => (
              <li key={i} className="rounded-lg border border-line bg-ink/40 p-3 text-sm">
                <div className="flex items-start gap-3">
                  <span className={`pill mt-0.5 capitalize ${VERDICT_STYLE[check.verdict]}`}>{check.verdict.replace('_', ' ')}</span>
                  <p className="text-zinc-300">{check.claim}</p>
                </div>
                {check.reason && <p className="mt-1.5 text-xs text-muted">{check.reason}</p>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
