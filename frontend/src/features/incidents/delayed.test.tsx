import { describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ApiClient } from '../../api'
import type { DelayedRecheck } from '../../api/types'
import { DelayedRecheckPanel } from './DelayedRecheckPanel'

const item = (status: DelayedRecheck['status']): DelayedRecheck => ({ delayed_id: 'delayed-1', sequence: 1, status,
  due_at: '2026-10-07T01:00:30Z', expires_at: '2026-10-07T01:05:30Z', finished_at: null, reason: null,
  initial_result: { status: 'passed', finished_at: '2026-10-07T01:00:00Z', consecutive: 3 }, result: null })

describe('5B independent delayed observations', () => {
  it('keeps the initial pass while separately showing relapse and no automatic repair', () => {
    const html = renderToStaticMarkup(<DelayedRecheckPanel item={item('relapsed')} />)
    expect(html).toContain('初次通过后复发')
    expect(html).toContain('初次连续观察：通过')
    expect(html).toContain('不会自动修复或回滚')
    expect(html).not.toContain('延时复查通过')
  })
  it.each(['unknown', 'invalidated', 'expired'] as const)('never presents %s as a delayed pass', status => {
    const html = renderToStaticMarkup(<DelayedRecheckPanel item={item(status)} />)
    expect(html).not.toContain('延时复查通过')
    expect(html).toContain('初次连续观察：通过')
  })
  it('explains that pending work leaves the event available', () => {
    expect(renderToStaticMarkup(<DelayedRecheckPanel item={item('pending')} />)).toContain('等待期间不占任务名额')
  })
  it('loads history and older pages through GET only', async () => {
    const fetcher = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ items: [], next_before_sequence: null })))
    const api = new ApiClient({ fetcher })
    await api.listDelayedRechecks('incident-1')
    await api.listDelayedRechecks('incident-1', 21)
    expect(fetcher.mock.calls.map(call => call[1]?.method)).toEqual(['GET', 'GET'])
    expect(String(fetcher.mock.calls[1][0])).toContain('/delayed-rechecks?limit=20&before_sequence=21')
  })
})
