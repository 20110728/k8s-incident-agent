import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { ApiClient, ApiClientError } from '../../api'
import type { IncidentStatusResponse, InteractionResult, Recheck, RunSummary } from '../../api/types'
import { IncidentWorkbench, InteractionCard, RecheckCard } from './IncidentWorkbench'
import { commandPermissions, createClientKey, mergeBy, pendingKey, postCommand, readPending, recoverCommand, type PendingCommand } from './workbenchState'
import App from '../../App'
import { IncidentAnalysis } from './IncidentAnalysis'
import { ObservationPanel } from './ObservationPanel'
import { conversationEntries, incidentHref, initialStage, taskLabel, workspaceRoute } from './workspaceNavigation'

const row = (status = 'succeeded', kind = 'diagnosis'): RunSummary => ({ run_id: 'run-1', status, run_kind: kind,
  created_at: '2026-10-07T01:00:00Z', updated_at: '2026-10-07T01:00:10Z', finished_at: null, attempt: 1, last_error_code: null })
const incident = (status = 'succeeded'): IncidentStatusResponse => ({ incident_id: 'incident-1', thread_id: 'thread-1',
  execution_mode: 'queued', worker_available: true, run: row(status), phase: 'remediation_skipped', waiting_for_approval: false,
  request: { namespace: 'demo', service_name: 'service', description: 'original incident' }, valid: true, error_count: 0,
  collection_plan: [], evidence: [], retrieval_query: null, retrieved_runbooks: [], diagnosis: null, llm_model: null,
  llm_usage: {}, diagnosis_retry_count: 0, remediation_plan: null, risk_level: null, remediation_llm_model: null,
  remediation_llm_usage: {}, requires_approval: false, approved: null, approval_status: null, approval_request: null,
  approval_record: null, action_result: null, verification_result: null, errors: [], trace: [] })
const command: PendingCommand = { kind: 'interaction', incidentId: 'incident-1', body: {
  client_message_id: 'stable-key', content: '人工处理后重新检查', intent: 'recheck' } }
const receipt = { run: row('queued', 'interaction'), output: null, calls: [] }
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status })

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('4C separated home and stage workspace', () => {
  it('offers stable observation only when single recheck is permitted', () => {
    for (const status of ['succeeded', 'running', 'waiting_approval', 'waiting_user']) {
      const permissions = commandPermissions(incident(status), [], [])
      expect(permissions.observe).toBe(permissions.recheck)
    }
    expect(commandPermissions(incident(), [], []).observe).toBe(true)
    expect(commandPermissions(incident(), [row('running', 'interaction')], []).observe).toBe(false)
  })
  it('shows failed window separately from a passing last sample and preserves legacy results', () => {
    const html = renderToStaticMarkup(<ObservationPanel observation={{ status: 'invalidated', consecutive: 0,
      policy: { version: 'recovery-window-v1', required_consecutive: 3 }, samples: [
        { sequence: 1, status: 'passed', resource_status: 'ready', business_status: 'passed', started_at: 'start', finished_at: 'finish' },
      ] }} />)
    expect(html).toContain('目标变化，观察失效')
    expect(html).toContain('不代表所有副本')
    expect(html).not.toContain('连续观察通过')
    expect(renderToStaticMarkup(<ObservationPanel />)).toBe('')
  })
  it('keeps the bare URL on home even when a previous event is stored', () => {
    vi.stubGlobal('location', { search: '', pathname: '/' })
    vi.stubGlobal('localStorage', { getItem: () => 'previous-incident' })
    const html = renderToStaticMarkup(<App />)
    expect(html).toContain('创建并进入事件')
    expect(html).toContain('历史事件')
    expect(html).not.toContain('event-conversation')
    expect(workspaceRoute('')).toEqual({ incidentId: null, invalid: false })
  })
  it('opens an event without rendering creation or the history directory', () => {
    vi.stubGlobal('location', { search: '?incident_id=incident-1', pathname: '/' })
    const html = renderToStaticMarkup(<App />)
    expect(html).toContain('正在找回事件')
    expect(html).not.toContain('创建并进入事件')
    expect(html).not.toContain('incident-directory')
  })
  it('rejects invalid IDs and starts another event without the previous round or stage', () => {
    expect(workspaceRoute('?incident_id=bad%2Fid').invalid).toBe(true)
    expect(incidentHref('incident-2')).toBe('?incident_id=incident-2')
    expect(() => incidentHref('../bad')).toThrow()
    expect(initialStage('?stage=plan', incident())).toBe('plan')
    expect(initialStage('?stage=missing', incident())).toBe('overview')
    expect(initialStage('', { ...incident(), waiting_for_approval: true })).toBe('approval')
  })
  it('mounts only the requested analysis section', () => {
    const html = renderToStaticMarkup(<IncidentAnalysis incident={incident()} section="diagnosis" />)
    expect(html).toContain('Diagnosis not available')
    expect(html).not.toContain('No evidence available')
    expect(html).not.toContain('No Runbooks retrieved')
    vi.stubGlobal('location', { search: '?stage=evidence&chat=closed', pathname: '/' })
    const workspace = renderToStaticMarkup(<IncidentWorkbench incident={incident()} onCurrent={() => {}}
      onApproval={async () => {}} approving={false} approvalError={null} />)
    expect(workspace).toContain('No evidence available')
    expect(workspace).not.toContain('Diagnosis not available')
    expect(workspace).toContain('data-chat="closed"')
    expect(workspace).toContain('aria-current="page"')
  })
  it('does not equate completed work with verified recovery', () => {
    expect(taskLabel('succeeded', 'remediation_skipped')).toContain('未确认恢复')
    expect(taskLabel('succeeded', 'verification_succeeded')).toContain('本轮验证通过')
    expect(taskLabel('waiting_user')).toBe('等待回答')
  })
  it('merges replies with task cards without losing unhydrated or older messages', () => {
    const message = (id: string, role: string, run: string | null, created: string) => ({
      message_id: id, role, related_run_id: run, created_at: created, sequence: 1,
      source: 'user_supplied', content: id, evidence_refs: [], adopted_by_run_ids: [],
    })
    const messages = [message('older', 'assistant', 'old-run', '2026-10-07T00:00:00Z'),
      message('question', 'user', 'run-1', '2026-10-07T01:00:00Z'),
      message('reply', 'assistant', 'run-1', '2026-10-07T01:00:10Z')]
    const result: InteractionResult = { ...receipt, output: { intent: 'explain', answer: 'answer' } }
    expect(conversationEntries(messages, [result]).map(e => e.key)).toEqual([
      'message:older', 'message:question', 'interaction:run-1',
    ])
    expect(conversationEntries(messages, []).map(e => e.key)).toContain('message:reply')
  })
})

describe('4C request recovery and API contracts', () => {
  it('creates a valid key on HTTP pages without randomUUID', () => {
    vi.stubGlobal('crypto', { getRandomValues: (bytes: Uint8Array) => bytes.fill(171) })
    expect(createClientKey()).toBe('web-' + 'ab'.repeat(16))
  })
  it('finds a committed recheck after a lost POST response without submitting twice', async () => {
    let committed = false
    const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method === 'POST') { committed = true; throw new TypeError('connection lost after commit') }
      return response(committed ? receipt : {}, committed ? 200 : 404)
    })
    const api = new ApiClient({ fetcher })
    await expect(postCommand(api, command)).rejects.toBeInstanceOf(ApiClientError)
    expect(await recoverCommand(api, command)).toEqual(receipt)
    expect(fetcher.mock.calls.map(x => x[1]?.method)).toEqual(['POST', 'GET'])
    expect(String(fetcher.mock.calls[1][0])).toContain('/interactions?client_message_id=stable-key')
  })
  it('refresh and reopen only query, even when the server has not stored the request', async () => {
    const storage = { getItem: (key: string) => key === pendingKey('incident-1') ? JSON.stringify(command) : null }
    const restored = readPending(storage, 'incident-1')!
    const fetcher = vi.fn(async () => response({}, 404))
    const api = new ApiClient({ fetcher })
    await expect(recoverCommand(api, restored)).rejects.toMatchObject({ status: 404 })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(readPending(storage, 'another-incident')).toBeNull()
  })
  it('an explicit retry checks first and retains the exact body and idempotency key', async () => {
    const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => init?.method === 'GET'
      ? response({}, 404) : response(receipt, 202))
    expect(await recoverCommand(new ApiClient({ fetcher }), command, true)).toEqual(receipt)
    expect(fetcher.mock.calls.map(x => x[1]?.method)).toEqual(['GET', 'POST'])
    expect(JSON.parse(fetcher.mock.calls[1][1]!.body as string)).toEqual(command.body)
  })
  it('a lookup outage cannot cause a POST, even on explicit retry', async () => {
    const fetcher = vi.fn(async () => response({}, 503))
    await expect(recoverCommand(new ApiClient({ fetcher }), command, true)).rejects.toMatchObject({ status: 503 })
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
  it('question recovery uses controls, and answer keeps question version and original run', async () => {
    const answer: PendingCommand = { incidentId: 'incident-1', kind: 'answer', runId: 'run-1', body: {
      client_message_id: 'reply-1', content: '回答', question_id: 'q-one', version: 2, answers: { onset: '十分钟前' }, skip: false } }
    const fetcher = vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => init?.method === 'GET'
      ? response({}, 404) : response({ control_id: 'control-1', status: 'saved', message_id: 'message-1' }, 202))
    await recoverCommand(new ApiClient({ fetcher }), answer, true)
    expect(String(fetcher.mock.calls[0][0])).toContain('/controls?client_message_id=reply-1')
    expect(String(fetcher.mock.calls[1][0])).toContain('/runs/run-1/answers')
    expect(JSON.parse(fetcher.mock.calls[1][1]!.body as string)).toEqual(answer.body)
  })
  it('rejects response shapes that could confuse historical results with current state', async () => {
    const api = new ApiClient({ fetcher: async () => response(receipt) })
    await expect(api.getRound('incident-1', 'run-1')).rejects.toMatchObject({ code: 'INVALID_API_RESPONSE' })
  })
  it('uses read-only cursors for all history pages and never the legacy POST recheck route', async () => {
    const fetcher = vi.fn(async () => response({ items: [], next_cursor: null, next_before_sequence: null }))
    const api = new ApiClient({ fetcher })
    await api.listIncidents('opaque+a/b')
    await api.listRuns('incident-1', 'cursor+one')
    await api.listMessages('incident-1', 21)
    await api.listRechecks('incident-1', 11)
    const urls = fetcher.mock.calls.map(x => String((x as unknown[])[0]))
    expect(urls[0]).toContain('cursor=opaque%2Ba%2Fb')
    expect(urls[1]).toContain('cursor=cursor%2Bone')
    expect(urls[2]).toContain('before_sequence=21')
    expect(urls[3]).toContain('before_sequence=11')
  })
})

describe('4C controls reflect server state', () => {
  it('allows explaining an approval wait but not a recheck or ambiguous new request', () => {
    const state = incident('waiting_approval')
    const actions = commandPermissions(state, [], [])
    expect(actions.explain).toBe(true)
    expect(actions.recheck).toBe(false)
    expect(actions.auto).toBe(false)
    expect(actions.stop).toBe(true)
    expect(actions.supplement).toBe(true)
  })
  it('only answers a current, non-stopped question', () => {
    const state = incident('waiting_user')
    state.run!.question = { question_id: 'q-1', version: 1, questions: [{ slot: 'onset', text: '什么时候开始？' }], reason: '缺少时间', evidence_revision: 'hash' }
    expect(commandPermissions(state, [], []).answer).toBe(true)
    state.run!.stop_requested = true
    expect(commandPermissions(state, [], []).answer).toBe(false)
    state.run!.status = 'cancelled'
    expect(commandPermissions(state, [], []).answer).toBe(false)
  })
  it('blocks recheck during another interaction, unresolved writes and sync mode', () => {
    const state = incident()
    expect(commandPermissions(state, [], []).recheck).toBe(true)
    expect(commandPermissions(state, [row('running', 'interaction')], []).recheck).toBe(false)
    expect(commandPermissions(state, [row('running', 'interaction')], []).investigate).toBe(false)
    expect(commandPermissions(state, [], [{ operation_id: 'op', run_id: 'run', state: 'manual_required', error_code: null }]).recheck).toBe(false)
    state.execution_mode = 'sync'
    expect(commandPermissions(state, [], []).investigate).toBe(false)
  })
  it('can stop a saved investigation while the worker is offline and the parent is cancelled', () => {
    const state = incident('cancelled')
    state.worker_available = false
    expect(commandPermissions(state, [], [], true).stop).toBe(true)
  })
  it('merges pagination and polling without duplicates or losing older rows', () => {
    const rows = mergeBy([{ id: 'old', value: 1 }, { id: 'new', value: 1 }], [{ id: 'new', value: 2 }, { id: 'latest', value: 3 }], x => x.id)
    expect(rows).toEqual([{ id: 'old', value: 1 }, { id: 'new', value: 2 }, { id: 'latest', value: 3 }])
  })
})

describe('4C rendered evidence boundaries', () => {
  it('shows failed/unknown rechecks, separate times and target changes without claiming recovery', () => {
    const item: Recheck = { recheck_id: 'check-1', started_at: '2026-10-07T01:00:00Z', finished_at: '2026-10-07T01:00:20Z',
      note: '<script>claim recovery</script>', status: 'unknown', resource_status: 'ready', business_status: 'unknown',
      target_comparison: { status: 'changed', changes: { deployment_uid: { before: 'old', after: 'new' } } },
      unverified_scope: ['未验证数据库'], error_code: 'COLLECTION_FAILED', collection_errors: [] }
    const html = renderToStaticMarkup(<RecheckCard item={item} />)
    expect(html).toContain('独立复查 · unknown')
    expect(html).toContain('不覆盖原结论')
    expect(html).toContain('changed')
    expect(html).toContain('COLLECTION_FAILED')
    expect(html).toContain('未验证数据库')
    expect(html).not.toContain('<script>')
    expect(item.status).toBe('unknown')
  })
  it('renders historical citations, unknown scope and missing model usage explicitly', () => {
    const item: InteractionResult = { run: row(), output: { intent: 'explain', answer: '历史证据不足', historical_only: true,
      unknowns: ['未采集外部依赖'], citations: [{ citation_id: 'run-1:evidence-1', run_id: 'run-1', collected_at: null, snapshot_at: '2026-10-07T01:00:00Z' }] },
      calls: [{ purpose: 'explain', status: 'failed_or_unknown' }] }
    const html = renderToStaticMarkup(<InteractionCard item={item} />)
    expect(html).toContain('没有重新采集现场')
    expect(html).toContain('run-1:evidence-1')
    expect(html).toContain('未采集外部依赖')
    expect(html).toContain('不能按零计算')
  })
  it('opening a historical URL never renders a live approval card or triggers API calls', () => {
    vi.stubGlobal('location', { search: '?incident_id=incident-1&run_id=old-run' })
    const fetcher = vi.spyOn(globalThis, 'fetch')
    const state = incident('waiting_approval')
    state.waiting_for_approval = true
    const html = renderToStaticMarkup(<IncidentWorkbench incident={state} onCurrent={() => {}} onApproval={async () => {}}
      approving={false} approvalError={null} />)
    expect(html).toContain('正在查看历史快照')
    expect(html).not.toContain('Approval data unavailable')
    expect(fetcher).not.toHaveBeenCalled()
  })
})
