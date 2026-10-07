import type { IncidentStatusResponse, InteractionResult, Message } from '../../api/types'
import { normalizeIncidentId } from './incidentSession'

export const WORKSPACE_STAGES = [
  { id: 'overview', label: '事件概览' },
  { id: 'evidence', label: '采集与证据' },
  { id: 'diagnosis', label: 'Diagnosis' },
  { id: 'plan', label: 'Remediation plan' },
  { id: 'approval', label: '审批' },
  { id: 'results', label: '执行与复查' },
  { id: 'debug', label: '调试详情' },
] as const
export type WorkspaceStage = typeof WORKSPACE_STAGES[number]['id']

export function workspaceRoute(search: string) {
  const params = new URLSearchParams(search)
  return { incidentId: normalizeIncidentId(params.get('incident_id')), invalid: params.has('incident_id') && !normalizeIncidentId(params.get('incident_id')) }
}
export function initialStage(search: string, incident: IncidentStatusResponse): WorkspaceStage {
  const stage = new URLSearchParams(search).get('stage')
  if (WORKSPACE_STAGES.some(s => s.id === stage)) return stage as WorkspaceStage
  if (incident.waiting_for_approval) return 'approval'
  return incident.diagnosis ? 'diagnosis' : 'overview'
}
export function incidentHref(id: string) {
  const valid = normalizeIncidentId(id)
  if (!valid) throw new Error('无效的事件 ID')
  return `?incident_id=${encodeURIComponent(valid)}`
}
export function taskLabel(status?: string, phase?: string) {
  const labels: Record<string, string> = { queued: '已排队', running: '处理中', waiting_user: '等待回答',
    waiting_approval: '待审批', retry_scheduled: '等待重试', reconciling: '待核对', cancelled: '已停止', failed: '任务失败' }
  if (status && labels[status]) return labels[status]
  if (phase === 'verification_succeeded') return '已结束 · 本轮验证通过'
  if (status === 'succeeded') return '已结束 · 未确认恢复'
  return phase ? phase.replaceAll('_', ' ') : '状态未记录'
}

export type ConversationEntry = { key: string; at: string } & (
  | { kind: 'message'; message: Message }
  | { kind: 'interaction'; interaction: InteractionResult }
)
export function conversationEntries(messages: Message[], interactions: InteractionResult[]): ConversationEntry[] {
  const answered = new Set(interactions.filter(i => i.output?.answer).map(i => i.run.run_id))
  const entries: ConversationEntry[] = messages.filter(m => !(m.role === 'assistant' && m.related_run_id && answered.has(m.related_run_id)))
    .map(message => ({ kind: 'message', message, key: 'message:' + message.message_id, at: message.created_at }))
  for (const interaction of interactions) {
    const reply = messages.find(m => m.role === 'assistant' && m.related_run_id === interaction.run.run_id)
    entries.push({ kind: 'interaction', interaction, key: 'interaction:' + interaction.run.run_id,
      at: reply?.created_at ?? interaction.run.created_at })
  }
  return entries.sort((a, b) => a.at.localeCompare(b.at) || a.key.localeCompare(b.key))
}
