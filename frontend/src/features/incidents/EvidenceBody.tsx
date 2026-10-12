import { useState } from 'react'
import { apiClient, type EvidenceItem } from '../../api'

export function EvidenceBody({ item, incidentId, runId }: { item: EvidenceItem; incidentId: string; runId?: string }) {
  const [full, setFull] = useState<EvidenceItem | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function load() {
    setBusy(true); setError('')
    try { setFull((await apiClient.getEvidence(incidentId, item.evidence_id, runId)).evidence) }
    catch { setError('读取失败，请重试。该操作只读取已保存记录，不重新采集。') }
    finally { setBusy(false) }
  }
  return <details className="structured-data">
    <summary>查看证据内容</summary>
    {item.body_preview?.truncated && !full && <>
      <p>这里只显示日志末尾预览，不是完整采样。</p>
      <button type="button" disabled={busy} onClick={() => void load()}>{busy ? '正在读取…' : '读取完整已保存日志'}</button>
    </>}
    {full && <p>已载入保存的日志正文；仍受原采样范围限制，不代表全部历史。</p>}
    {error && <p role="alert">{error}</p>}
    <pre style={{ maxHeight: 480, overflow: 'auto', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
      {JSON.stringify((full ?? item).data, null, 2)}
    </pre>
  </details>
}
