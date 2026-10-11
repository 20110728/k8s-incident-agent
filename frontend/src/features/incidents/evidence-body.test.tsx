import { describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ApiClient, apiClient, type EvidenceItem } from '../../api'
import { EvidenceBody } from './EvidenceBody'

describe('saved evidence body', () => {
  it('renders a preview without fetching the full log', () => {
    const fetch = vi.spyOn(apiClient, 'getEvidence')
    const item = { evidence_id: 'ev-log', data: { content: 'preview-only' },
      body_preview: { truncated: true, fields: ['content'], scope: 'preview' } } as EvidenceItem
    try {
      const html = renderToStaticMarkup(<EvidenceBody item={item} incidentId="incident" runId="run" />)
      expect(html).toContain('preview-only')
      expect(html).toContain('读取完整已保存日志')
      expect(fetch).not.toHaveBeenCalled()
    } finally { fetch.mockRestore() }
  })
  it('loads only requested evidence and rejects mismatched responses', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ evidence: { evidence_id: 'ev-log', data: { content: 'saved' } } })))
    const api = new ApiClient({ fetcher })
    expect((await api.getEvidence('incident', 'ev-log', 'run')).evidence.data.content).toBe('saved')
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining('/incidents/incident/evidence/ev-log?run_id=run'), expect.objectContaining({ method: 'GET' }))
    fetcher.mockImplementation(async () => new Response(JSON.stringify({ evidence: { evidence_id: 'wrong', data: {} } })))
    await expect(api.getEvidence('incident', 'ev-log', 'run')).rejects.toThrow()
  })
})
