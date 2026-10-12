import { describe, expect, it, vi } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import type { InvestigationView, Question } from '../../api/types'
import { ApiClient } from '../../api'
import { InvestigationPanel } from './InvestigationPanel'
import { changedResourceRefs, QuestionForm } from './IncidentWorkbench'
import { BudgetDetails } from './RunBudgetPanel'
import { postCommand, readPending } from './workbenchState'

const question: Question = { question_id: 'q', version: 1, reason: 'what changed', evidence_revision: 'rev',
  questions: [{ slot: 'changes', text: 'What changed?' }],
  change_candidates: [{ resource_ref: 'ref-1', kind: 'pod', name: 'pod-a', container: 'app' }] }
const view: InvestigationView = { version: 1, steps: [{ step: 1, action: 'collect', reason: '<script>unsafe</script>',
  missing_fact: 'current logs', evidence_ids: ['old'], results: [{ tool: 'pod_logs', coverage: 'unknown', error_code: 'READ_FAILED', evidence_ids: ['new'], generation: 1 }] }],
  observations: [{ evidence_id: 'old', origin: 'baseline', resource_type: 'PodLogs', resource_name: 'pod-a', collected_at: null,
    coverage: 'partial', error: null, truncated: true, current: false }],
  omitted_observations: 0, outcome: 'handoff', stop_reason: 'MODEL_TOKEN_LIMIT', question_count: 1, answer_count: 1 }

describe('6B-3a investigation presentation', () => {
  it('shows rejected attempts without inventing executed actions or retroactive details', () => {
    const failed = { ...view, steps: [], stop_reason: 'DECISION_VALIDATION_FAILED', validation_failures: [
      { attempt: 1, stage: 'schema', action: null, detail: 'decision.collect.requests: missing' }] }
    const html = renderToStaticMarkup(<InvestigationPanel value={failed} evidenceIds={[]} />)
    expect(html).toContain('字段结构')
    expect(html).toContain('decision.collect.requests: missing')
    expect(html).toContain('未执行的模型决定')
    const old = renderToStaticMarkup(<InvestigationPanel value={{ ...failed, validation_failures: [] }} evidenceIds={[]} />)
    expect(old).toContain('旧记录没有保存具体校验原因')
  })
  it('distinguishes old evidence and failed sampling, escapes prose, links only current citations', () => {
    const html = renderToStaticMarkup(<InvestigationPanel value={view} evidenceIds={['new']} />)
    expect(html).toContain('READ_FAILED')
    expect(html).toContain('已替换或失效')
    expect(html).toContain('第 1 次复采')
    expect(html).toContain('MODEL_TOKEN_LIMIT')
    expect(html).toContain('时间未记录')
    expect(html).toContain('&lt;script&gt;')
    expect(html).not.toContain('<script>')
    expect(html).toContain('href="#evidence-new"')
    expect(html).not.toContain('href="#evidence-old"')
  })
  it('does not invent completion or zero usage for older or incomplete records', () => {
    expect(renderToStaticMarkup(<InvestigationPanel evidenceIds={[]} />)).toContain('没有调查过程记录')
    const pending = { ...view, outcome: null, steps: [{ ...view.steps[0], results: [] }] }
    expect(renderToStaticMarkup(<InvestigationPanel value={pending} evidenceIds={[]} />)).toContain('尚无整批完成记录')
  })
  it('offers unchecked resources only for a changes question, never submits selections when skipping', () => {
    const html = renderToStaticMarkup(<QuestionForm question={question} disabled={false} submit={() => {}} />)
    expect(html).toContain('type="checkbox"')
    expect(html).not.toContain('checked=""')
    expect(html).toContain('不授权修复')
    expect(changedResourceRefs(question, ['ref-1', 'foreign', 'ref-1'], false)).toEqual(['ref-1'])
    expect(changedResourceRefs(question, ['ref-1'], true)).toEqual([])
    expect(changedResourceRefs({ ...question, questions: [{ slot: 'impact', text: '?' }] }, ['ref-1'], false)).toEqual([])
    expect(renderToStaticMarkup(<QuestionForm question={{ ...question, change_candidates: undefined }} disabled submit={() => {}} />)).not.toContain('type="checkbox"')
  })
  it('preserves explicit resource choices and the original message ID across uncertain-request recovery', async () => {
    const command = { kind: 'answer' as const, incidentId: 'event', runId: 'run', body: { client_message_id: 'stable-id',
      content: 'changed', question_id: 'q', version: 1, answers: { changes: 'updated' }, skip: false, changed_resource_refs: ['ref-1'] } }
    const restored = readPending({ getItem: () => JSON.stringify(command) }, 'event')
    expect(restored).toEqual(command)
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ control_id: 'receipt', status: 'queued' })))
    const api = new ApiClient({ fetcher })
    await postCommand(api, restored!)
    expect(fetcher).toHaveBeenCalledWith(expect.stringContaining('/runs/run/answers'), expect.objectContaining({ body: JSON.stringify(command.body) }))
  })
  it('separates reported usage from unknown reservations', () => {
    const html = renderToStaticMarkup(<BudgetDetails value={{ available: true, run_id: 'r',
      policy: { active_seconds: 300, extra_seconds: 90, total_tokens: 40000, tools: 6, decisions: 3 },
      used: { active_seconds: 3, extra_seconds: 1, tokens: 9600, tools: 1, decisions: 1 },
      generation: { attempts: 2, reported_tokens: 100, unreported_attempts: 1 },
      accounting: { reported_charge: 100, estimated_or_reserved_charge: 9500, embedding_charge: 1000, remaining: 30400 } }} />)
    expect(html).toContain('生成调用尝试 2 次')
    expect(html).toContain('未报告用量 1 次')
    expect(html).toContain('不代表精确费用')
    expect(html).toContain('估算或未结算预留 9500')
    expect(html).toContain('剩余预算 30400')
    expect(html).toContain('不重复相加')
  })
})
