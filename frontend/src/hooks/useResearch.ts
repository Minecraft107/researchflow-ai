import { useEffect, useState } from 'react'
import { api } from '../services/api'
import type { AgentEvent, Run } from '../services/api'

/** Loads a research run and follows its live event stream (SSE) until it finishes. */
export function useResearch(id: string) {
  const [run, setRun] = useState<Run | null>(null)
  const [events, setEvents] = useState<AgentEvent[]>([])
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let closed = false

    const refresh = () =>
      api
        .run(id)
        .then((r) => !closed && setRun(r))
        .catch((e: Error) => !closed && setError(e.message))
    refresh()

    // EventSource reconnects by itself and resends Last-Event-ID, so no events are lost or repeated.
    const source = new EventSource(api.streamUrl(id))
    source.addEventListener('agent', (message) => {
      const event = JSON.parse((message as MessageEvent).data) as AgentEvent
      setEvents((prev) => (prev.some((e) => e.id === event.id) ? prev : [...prev, event]))
      if (event.type === 'completed' || event.type === 'failed') refresh() // a node finished: pull its output
    })
    source.addEventListener('done', () => {
      source.close()
      refresh()
    })
    return () => {
      closed = true
      source.close()
    }
  }, [id])

  return { run, events, error }
}

/** Seconds since `startIso`, ticking once a second while `active`. */
export function useElapsed(startIso: string | undefined, active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active])
  return startIso ? Math.max(0, (now - new Date(startIso).getTime()) / 1000) : 0
}
