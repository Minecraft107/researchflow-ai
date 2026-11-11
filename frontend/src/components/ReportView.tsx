import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, safeUrl } from '../services/api'
import type { Report } from '../services/api'

// Turn "[1, 3]" into one link per number so each citation can jump to its reference.
function linkCitations(markdown: string): string {
  return markdown.replace(/\[(\d+(?:\s*,\s*\d+)*)\](?!\()/g, (_, numbers: string) =>
    numbers
      .split(',')
      .map((n) => `[${n.trim()}](#ref-${n.trim()})`)
      .join(' '),
  )
}

function jumpToReference(number: string) {
  const element = document.getElementById(`ref-${number}`)
  if (!element) return
  element.scrollIntoView({ behavior: 'smooth', block: 'center' })
  element.classList.remove('flash')
  void element.offsetWidth // restart the highlight animation
  element.classList.add('flash')
}

function ReportMarkdown({ content }: { content: string }) {
  return (
    <div className="prose-report">
      <Markdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children }) => {
            if (href?.startsWith('#ref-')) {
              const number = href.slice(5)
              return (
                <button
                  type="button"
                  onClick={() => jumpToReference(number)}
                  title={`Go to reference ${number}`}
                  className="mx-0.5 inline-flex h-[18px] min-w-[18px] cursor-pointer items-center justify-center rounded bg-accent/15 px-1 align-text-top text-[11px] font-semibold text-accent no-underline transition hover:bg-accent/30"
                >
                  {children}
                </button>
              )
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            )
          },
        }}
      >
        {linkCitations(content)}
      </Markdown>
    </div>
  )
}

export default function ReportView({ id, report }: { id: string; report: Report }) {
  return (
    <article className="card px-6 py-7 sm:px-10">
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-line pb-6">
        <h2 className="max-w-2xl text-2xl font-semibold tracking-tight text-white">{report.title}</h2>
        <div className="flex gap-2" aria-label="Export report">
          {(['pdf', 'docx', 'markdown'] as const).map((format) => (
            <a key={format} href={api.exportUrl(id, format)} download className="btn text-xs uppercase">
              {format === 'markdown' ? 'MD' : format}
            </a>
          ))}
        </div>
      </div>

      {!report.threshold_met && (
        <p className="mt-5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
          The Critic's final score was {report.quality_score ?? 'n/a'}/100, below the quality target. Read the Limitations section before relying
          on these findings.
        </p>
      )}

      {report.sections.map((section) => (
        <section key={section.heading} className="mt-8">
          <h3 className="mb-2 text-lg font-semibold text-white">{section.heading}</h3>
          <ReportMarkdown content={section.content} />
        </section>
      ))}

      <section className="mt-10 border-t border-line pt-6">
        <h3 className="mb-3 text-lg font-semibold text-white">References</h3>
        {report.references.length === 0 && <p className="text-sm text-muted">No sources were cited.</p>}
        <ol className="space-y-1">
          {report.references.map((ref) => (
            <li key={ref.number} id={`ref-${ref.number}`} className="flex gap-3 rounded-md px-2 py-1.5 text-sm">
              <span className="w-7 shrink-0 text-right font-semibold text-accent">[{ref.number}]</span>
              <span className="min-w-0">
                <a href={safeUrl(ref.url)} target="_blank" rel="noopener noreferrer" className="text-zinc-100 underline decoration-zinc-600 underline-offset-2 hover:decoration-accent">
                  {ref.title}
                </a>
                <span className="block truncate text-xs text-muted">
                  {[ref.publisher, ref.published_date, `retrieved ${ref.retrieved_date}`].filter(Boolean).join(' · ')}
                </span>
              </span>
            </li>
          ))}
        </ol>
      </section>
    </article>
  )
}
