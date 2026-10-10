import { useEffect, useState } from 'react'
import { ApiClient } from '../../api'
import type { RunBudget } from '../../api/types'

const api = new ApiClient({ timeoutMs: 15_000 })

export function BudgetDetails({ value }: { value: RunBudget }) {
  if (!value.available || !value.used || !value.policy) return <p>本轮尚无预算记录：可能还没开始，或属于旧版任务。</p>
  const { used, policy } = value
  return <>
    <p>活动时间 {used.active_seconds.toFixed(1)} / {policy.active_seconds ?? '不限'} 秒；追加调查 {used.extra_seconds.toFixed(1)} / {policy.extra_seconds ?? '不限'} 秒</p>
    <p>调查决策 {used.decisions} / {policy.decisions}；追加工具 {used.tools} / {policy.tools ?? '不限'}；Token 记账 {used.tokens} / {policy.total_tokens ?? '不限'}</p>
    {value.accounting && <p>已报告用量对应记账 {value.accounting.reported_charge}；估算或未结算预留 {value.accounting.estimated_or_reserved_charge}；
      剩余预算 {value.accounting.remaining ?? '不限'}。其中 Embedding 记账 {value.accounting.embedding_charge}（已包含在总额，不重复相加）。</p>}
    {value.generation && <p>生成调用尝试 {value.generation.attempts} 次（含失败或中断）；供应商已报告 {value.generation.reported_tokens} Token；
      未报告用量 {value.generation.unreported_attempts} 次。此处不含 Embedding，预算总记账不等于此处已报告量。</p>}
    <p>人工等待不计时。未返回用量不代表零消耗；Token 记账不代表精确费用。</p>
    {value.handoff && <div role="status"><p>预算限制：{value.handoff.reason}</p><p>{value.handoff.known}</p><p>{value.handoff.unknown}</p><p>{value.handoff.next_step}</p></div>}
    <details><summary>预算与只读工具记录</summary><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 400, overflow: 'auto' }}>{JSON.stringify(value.calls, null, 2)}</pre></details>
  </>
}

export function RunBudgetPanel({ incidentId, runId }: { incidentId: string; runId?: string }) {
  const [value, setValue] = useState<RunBudget | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined
    setValue(null); setError('')
    async function load() {
      try {
        const result = await api.getRunBudget(incidentId, runId!)
        if (active) { setValue(result); setError('') }
      } catch { if (active) setError('预算记录读取失败，请稍后重试；不能据此认为用量为零。') }
      finally { if (active) timer = setTimeout(() => void load(), 5000) }
    }
    if (runId) void load()
    return () => { active = false; if (timer) clearTimeout(timer) }
  }, [incidentId, runId])
  return <section className="content-panel"><h3>本轮预算</h3>
    {error && <p role="alert">{error}</p>}
    {!runId ? <p>旧事件没有独立运行预算。</p> : value ? <BudgetDetails value={value} /> : !error && <p>正在读取预算…</p>}
  </section>
}
