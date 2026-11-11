import { useSyncExternalStore } from 'react'
import Landing from './pages/Landing'
import Research from './pages/Research'

// Hash routing (#/research/<id>): works from any static host and needs no server fallback route.
const subscribe = (onChange: () => void) => {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

export function navigate(path: string) {
  window.location.hash = path
}

export default function App() {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash)
  const match = hash.match(/^#\/research\/([0-9a-f-]{36})$/i)
  return match ? <Research key={match[1]} id={match[1]} /> : <Landing />
}
