import type { AgentEvent } from '../services/api'

export interface AgentSpec {
  key: string
  name: string
  role: string
  steps: { key: string; label: string }[]
}

export const AGENTS: AgentSpec[] = [
  {
    key: 'planner',
    name: 'Planner',
    role: 'Breaks the topic into research questions',
    steps: [
      { key: 'planning', label: 'Planning research' },
      { key: 'subtopics', label: 'Generating subtopics' },
      { key: 'queries', label: 'Creating search queries' },
    ],
  },
  {
    key: 'researcher',
    name: 'Researcher',
    role: 'Searches the web and extracts quoted evidence',
    steps: [
      { key: 'searching', label: 'Searching the web' },
      { key: 'collecting', label: 'Collecting sources' },
      { key: 'extracting', label: 'Extracting evidence' },
    ],
  },
  {
    key: 'critic',
    name: 'Critic',
    role: 'Audits evidence and citations independently',
    steps: [
      { key: 'reviewing', label: 'Reviewing evidence' },
      { key: 'citations', label: 'Checking citations' },
      { key: 'gaps', label: 'Identifying missing information' },
    ],
  },
]

type Status = 'waiting' | 'working' | 'done' | 'failed'

const STATUS_LABEL: Record<Status, string> = { waiting: 'Waiting', working: 'Working', done: 'Done', failed: 'Stopped' }
const STATUS_STYLE: Record<Status, string> = {
  waiting: 'text-zinc-500',
  working: 'border-accent/40 bg-accent/10 text-accent',
  done: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300',
  failed: 'border-rose-500/30 bg-rose-500/10 text-rose-300',
}

export default function AgentCard({ spec, events, runFailed }: { spec: AgentSpec; events: AgentEvent[]; runFailed: boolean }) {
  const mine = events.filter((e) => e.agent === spec.key)
  const last = mine[mine.length - 1]
  let status: Status = !last ? 'waiting' : last.type === 'completed' ? 'done' : 'working'
  if (runFailed && status === 'working') status = 'failed'

  // Steps since the agent's most recent start: the current one is active, earlier ones are done.
  const lastStart = mine.map((e) => e.type).lastIndexOf('started')
  const reached = new Set(mine.slice(Math.max(lastStart, 0)).map((e) => e.data.step))
  const activeStep = status === 'working' ? last?.data.step : undefined
  const passes = mine.filter((e) => e.type === 'started').length

  return (
    <section className={`card p-4 transition ${status === 'working' ? 'border-accent/50 shadow-lg shadow-accent/5' : ''}`}>
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="font-semibold text-white">{spec.name}</h3>
          <p className="mt-0.5 text-xs text-muted">{spec.role}</p>
        </div>
        <span className={`pill ${STATUS_STYLE[status]}`}>
          {status === 'working' && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" />}
          {STATUS_LABEL[status]}
          {passes > 1 && ` (pass ${passes})`}
        </span>
      </div>

      <ol className="mt-4 space-y-2">
        {spec.steps.map((step) => {
          const active = step.key === activeStep
          const done = status === 'done' || (reached.has(step.key) && !active)
          return (
            <li key={step.key} className={`flex items-center gap-2.5 text-sm ${active ? 'text-white' : done ? 'text-zinc-400' : 'text-zinc-600'}`}>
              <span
                aria-hidden
                className={`flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] ${
                  active ? 'animate-pulse border-accent bg-accent/20' : done ? 'border-emerald-500/50 bg-emerald-500/15 text-emerald-300' : 'border-line'
                }`}
              >
                {done ? '✓' : ''}
              </span>
              {step.label}
            </li>
          )
        })}
      </ol>

      <p className="mt-4 min-h-10 border-t border-line pt-3 text-xs leading-5 text-muted" aria-live="polite">
        {last ? last.message : 'Not started yet'}
      </p>
    </section>
  )
}
