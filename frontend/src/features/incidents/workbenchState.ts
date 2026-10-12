import { ApiClientError, type ApiClient } from '../../api'
import type { AnswerRequest, CommandReceipt, IncidentStatusResponse, InteractionIntent, InteractionRequest, Operation, RunSummary } from '../../api/types'

export type PendingCommand = { incidentId: string } & (
  | { kind: 'interaction'; body: InteractionRequest }
  | { kind: 'answer'; runId: string; body: AnswerRequest }
)
export const pendingKey = (id: string) => `incident-agent.command.v1.${id}`

export function createClientKey() {
  // getRandomValues remains available on the HTTP ECS workbench, where
  // randomUUID may be absent because the page is not a secure context.
  if (typeof globalThis.crypto.randomUUID === 'function') return globalThis.crypto.randomUUID()
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16))
  return 'web-' + Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('')
}

export function readPending(storage: Pick<Storage, 'getItem'>, id: string): PendingCommand | null {
  const raw = storage.getItem(pendingKey(id))
  if (!raw) return null
  const value = JSON.parse(raw) as PendingCommand
  if (value.incidentId !== id || !value.body?.client_message_id || typeof value.body.content !== 'string'
    || !['interaction', 'answer'].includes(value.kind)
    || (value.kind === 'answer' && (!value.runId || !value.body.question_id))) {
    throw new Error('待确认请求记录损坏，请保留记录并检查服务端消息，勿直接重复提交。')
  }
  return value
}

export function isMissing(error: unknown) {
  return error instanceof ApiClientError && error.status === 404
}
export function definiteRejection(error: unknown) {
  return error instanceof ApiClientError && [400, 409, 422].includes(error.status ?? 0)
}
export function describeError(error: unknown) {
  return error instanceof Error ? error.message : '请求失败，请查询状态后再操作。'
}

export function postCommand(api: ApiClient, command: PendingCommand): Promise<CommandReceipt> {
  return command.kind === 'answer'
    ? api.answer(command.incidentId, command.runId, command.body)
    : api.interact(command.incidentId, command.body)
}

/** Every retry starts with a GET. Only an explicit retry after a 404 may POST,
 * with the original body and key; refresh/polling never repeats a mutation. */
export async function recoverCommand(api: ApiClient, command: PendingCommand, retry = false): Promise<CommandReceipt> {
  try {
    return await api.findCommand(command.incidentId, command.body.client_message_id, command.kind === 'answer')
  } catch (error) {
    if (!retry || !isMissing(error)) throw error
    return postCommand(api, command)
  }
}

export function mergeBy<T>(old: T[], next: T[], key: (row: T) => string): T[] {
  return [...new Map([...old, ...next].map(row => [key(row), row])).values()]
}
export const terminalRun = (run: RunSummary) => ['succeeded', 'failed', 'cancelled'].includes(run.status)
const recheckPhases = new Set(['diagnosis_failed', 'remediation_failed', 'remediation_skipped', 'approval_rejected',
  'approval_failed', 'remediation_execution_failed', 'remediation_execution_conflict', 'verification_failed',
  'verification_succeeded', 'verification_skipped', 'failed'])

export function commandPermissions(incident: IncidentStatusResponse, runs: RunSummary[], operations: Operation[], pendingInvestigation = false) {
  const run = incident.run
  const interactionBusy = runs.some(r => r.run_kind === 'interaction' && !terminalRun(r))
  const unresolved = operations.some(o => ['prepared', 'dispatching', 'outcome_unknown', 'manual_required'].includes(o.state))
  const active = !!run && !terminalRun(run)
  const readable = !active || ['waiting_user', 'waiting_approval'].includes(run.status)
  const enabled = incident.execution_mode === 'queued'
  const recheckPhase = recheckPhases.has(incident.phase)
    || (incident.phase === 'remediation_planned' && !incident.requires_approval)
  const canControl = !!run && (active || !!run.invalidated_at)
  const canAddFacts = canControl || (!interactionBusy && recheckPhase)
  return {
    enabled, unresolved,
    explain: enabled && readable && !interactionBusy,
    compare: enabled && readable && !interactionBusy,
    auto: enabled && !active && !interactionBusy && !unresolved && recheckPhase,
    supplement: enabled && canAddFacts,
    investigate: enabled && canAddFacts,
    recheck: enabled && !active && !interactionBusy && !unresolved && recheckPhase && incident.approval_status !== 'pending',
    observe: enabled && !active && !interactionBusy && !unresolved && recheckPhase && incident.approval_status !== 'pending',
    stop: enabled && (active || interactionBusy || pendingInvestigation),
    answer: enabled && run?.status === 'waiting_user' && !run.stop_requested && !!run.question,
  }
}

export function defaultContent(intent: InteractionIntent) {
  return { auto: '', explain: '请解释这一轮的诊断依据与尚未验证的内容。', compare: '请对比这两个轮次的证据与结论。',
    supplement: '', investigate: '请继续调查，结合已保存的信息重新采集证据。', recheck: '请重新检查当前资源与登记业务，不执行修复。', observe: '请连续观察当前资源与登记业务的稳定性，不执行修复。', stop: '先别查了' }[intent]
}
