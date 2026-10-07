import { useEffect, useState } from 'react'
import { apiClient } from '../../api'
import type { IncidentListItem } from '../../api/types'
import { describeError, mergeBy } from './workbenchState'

export function IncidentDirectory() {
  const [items, setItems] = useState<IncidentListItem[]>([])
  const [cursor, setCursor] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function load(next?: string) {
    setBusy(true)
    try {
      const page = await apiClient.listIncidents(next)
      setItems(old => next ? mergeBy(old, page.items, x => x.incident_id) : page.items)
      setCursor(page.next_cursor)
      setError('')
    } catch (e) { setError(describeError(e)) }
    finally { setBusy(false) }
  }
  useEffect(() => { void load() }, [])
  return <details className="content-panel" open={!new URLSearchParams(location.search).has('incident_id')}>
    <summary>事件列表与找回</summary>
    <form method="get" className="workbench-actions">
      <label>事件 ID <input name="incident_id" required pattern="[A-Za-z0-9-]+" maxLength={128} /></label>
      <button type="submit">打开事件</button>
    </form>
    <button type="button" disabled={busy} onClick={() => void load()}>刷新列表</button>
    {error && <p role="alert">{error}</p>}
    <ul className="workbench-list">{items.map(item => <li key={item.incident_id}>
      <a href={`?incident_id=${encodeURIComponent(item.incident_id)}`}>{item.namespace} / {item.service_name}</a>
      {' · '}{item.run?.status ?? item.phase} · {new Date(item.updated_at).toLocaleString()}
      <small>{item.incident_id}</small>
    </li>)}</ul>
    {cursor && <button disabled={busy} onClick={() => void load(cursor)}>更早事件</button>}
  </details>
}
