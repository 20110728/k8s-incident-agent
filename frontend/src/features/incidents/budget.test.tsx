import { describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ApiClient } from '../../api'
import { BudgetDetails } from './RunBudgetPanel'

describe('6A budget visibility', () => {
  it('does not present legacy records as zero usage', () => {
    expect(renderToStaticMarkup(<BudgetDetails value={{ available: false, run_id: 'old' }} />)).toContain('尚无预算记录')
  })
  it('labels conservative charges and handoff instead of claiming recovery', () => {
    const text = renderToStaticMarkup(<BudgetDetails value={{ available: true, run_id: 'r',
      policy: { active_seconds: 300, extra_seconds: 90, total_tokens: 40000, tools: 6, decisions: 3 },
      used: { active_seconds: 20, extra_seconds: 0, tokens: 28000, tools: 0, decisions: 1 },
      handoff: { reason: 'MODEL_TOKEN_LIMIT', known: '已采集', unknown: '未完成', next_step: '人工排查' }, calls: [] }} />)
    expect(text).toContain('MODEL_TOKEN_LIMIT')
    expect(text).toContain('不代表精确费用')
    expect(text).toContain('人工排查')
  })
  it('uses read-only incident/run scoped API and rejects malformed counters', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ available: false, run_id: 'run' })))
    const api = new ApiClient({ fetcher })
    await api.getRunBudget('incident', 'run')
    expect(fetcher.mock.calls[0]).toBeDefined()
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining('/incidents/incident/runs/run/budget'), expect.objectContaining({ method: 'GET' }))
    fetcher.mockImplementation(async () => new Response(JSON.stringify({ available: true, run_id: 'run', used: {}, policy: {} })))
    await expect(api.getRunBudget('incident', 'run')).rejects.toThrow()
  })
})
