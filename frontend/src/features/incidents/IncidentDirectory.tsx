import { useEffect, useState } from 'react'
import { apiClient } from '../../api'
import type { IncidentListItem } from '../../api/types'
import { describeError, mergeBy } from './workbenchState'
import { incidentHref, taskLabel } from './workspaceNavigation'

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
  return <section className="content-panel incident-directory">
    <div className="directory-heading"><h2>历史事件</h2><button type="button" disabled={busy} onClick={() => void load()}>刷新列表</button></div>
    <form method="get" className="workbench-actions">
      <label>事件 ID <input name="incident_id" required pattern="[A-Za-z0-9-]+" maxLength={128} /></label>
      <button type="submit">打开事件</button>
    </form>
    {error && <p role="alert">{error}</p>}
    <ul className="workbench-list">{items.map(item => <li key={item.incident_id}>
      <a className="incident-list-link" href={incidentHref(item.incident_id)}>
        <strong>{item.namespace} / {item.service_name}</strong>
        <span className="phase-badge" data-status={item.run?.status}>{taskLabel(item.run?.status, item.phase)}</span>
        <small>最近更新 {new Date(item.updated_at).toLocaleString()} · {item.incident_id}</small>
      </a>
    </li>)}</ul>
    {cursor && <button disabled={busy} onClick={() => void load(cursor)}>更早事件</button>}
    {!busy && !error && !items.length && <p>暂无历史事件，从创建区创建第一个事件。</p>}
  </section>
}
