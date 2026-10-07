import { useEffect, useRef, useState } from 'react'
import { apiClient, type IncidentRequest, type IncidentStatusResponse, type SubmitApprovalRequest } from './api'
import { IncidentCreateForm } from './features/incidents/IncidentCreateForm'
import { IncidentDirectory } from './features/incidents/IncidentDirectory'
import { IncidentWorkbench } from './features/incidents/IncidentWorkbench'
import { LAST_INCIDENT_STORAGE_KEY } from './features/incidents/incidentSession'
import { describeError } from './features/incidents/workbenchState'
import { incidentHref, workspaceRoute } from './features/incidents/workspaceNavigation'
import './App.css'
import './features/incidents/workbench.css'

export default function App() {
  // A bare URL is always the home page. Last-event storage must not hijack it.
  const [route] = useState(() => workspaceRoute(location.search))
  const [incident, setIncident] = useState<IncidentStatusResponse | null>(null)
  const [loading, setLoading] = useState(!!route.incidentId)
  const [error, setError] = useState('')
  const [creating, setCreating] = useState(false)
  const createBusy = useRef(false)
  const [approving, setApproving] = useState(false)
  const approveBusy = useRef(false)
  const [approvalError, setApprovalError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)

  useEffect(() => {
    if (!route.incidentId) return
    let cancelled = false
    setLoading(true); setError('')
    void apiClient.getIncident(route.incidentId).then(value => {
      if (cancelled) return
      setIncident(value)
      try { localStorage.setItem(LAST_INCIDENT_STORAGE_KEY, value.incident_id) } catch { /* URL is sufficient. */ }
    }).catch(e => { if (!cancelled) setError(describeError(e)) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [route.incidentId, reload])

  async function create(request: IncidentRequest) {
    if (createBusy.current) return
    createBusy.current = true; setCreating(true); setError('')
    try {
      const value = await apiClient.createIncident(request)
      try { localStorage.setItem(LAST_INCIDENT_STORAGE_KEY, value.incident_id) } catch { /* URL is sufficient. */ }
      location.assign(incidentHref(value.incident_id))
    } catch (e) { setError(`创建未完成或结果未确认：${describeError(e)}。请先刷新历史列表检查，避免重复创建。`) }
    finally { createBusy.current = false; setCreating(false) }
  }
  async function approve(request: SubmitApprovalRequest) {
    if (!incident || approveBusy.current) return
    approveBusy.current = true; setApproving(true); setApprovalError(null)
    try { setIncident(await apiClient.submitApproval(incident.incident_id, request)) }
    catch (e) { setApprovalError(describeError(e)) }
    finally { approveBusy.current = false; setApproving(false) }
  }

  return <div className="app-shell workspace-app">
    <header className="topbar">
      <a className="brand" href={location.pathname || '/'}><span className="brand-mark">K8s</span>
        <span><strong>Kubernetes Incident Agent</strong><small>事件调查与处置</small></span></a>
      <span className="environment-badge">{route.incidentId ? '事件工作台' : '事件首页'}</span>
    </header>
    {!route.incidentId ? <main className="home-page">
      <div className="page-heading"><div><h1>事件中心</h1><p>创建一个新事件，或继续处理历史事件。</p></div></div>
      {route.invalid && <p className="api-error" role="alert">事件链接无效，请从列表打开或输入正确的事件 ID。</p>}
      {error && <p className="api-error" role="alert">{error}</p>}
      <div className="home-columns"><IncidentCreateForm submitting={creating} onSubmit={create} /><IncidentDirectory /></div>
    </main> : <main className="event-page">
      {loading && <p className="content-panel" role="status">正在找回事件……</p>}
      {error && <section className="content-panel" role="alert"><p>{error}</p>
        <button onClick={() => setReload(x => x + 1)}>重新读取</button> <a href={location.pathname || '/'}>返回事件列表</a></section>}
      {incident && !loading && <IncidentWorkbench key={incident.incident_id} incident={incident} onCurrent={setIncident}
        onApproval={approve} approving={approving} approvalError={approvalError} />}
    </main>}
  </div>
}
