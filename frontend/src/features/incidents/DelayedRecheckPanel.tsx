import type { DelayedRecheck } from '../../api/types'

const labels: Record<DelayedRecheck['status'], string> = { pending: '等待延时复查', running: '延时复查排队或采样中',
  passed: '延时复查通过', relapsed: '初次通过后复发', unknown: '延时结果未知', invalidated: '依据或目标变化，已失效', expired: '已过期，未验证' }
const time = (value: string) => new Date(value).toLocaleString()

export function DelayedRecheckPanel({ item }: { item: DelayedRecheck }) {
  return <article className="workbench-record">
    <h4>{labels[item.status]}</h4>
    <p>初次连续观察：通过 · {time(item.initial_result.finished_at)} · 连续 {item.initial_result.consecutive} 次</p>
    <p>计划复查：{time(item.due_at)}；最晚有效时间：{time(item.expires_at)}</p>
    {item.status === 'pending' && <p>到点后由 worker 执行；事件繁忙时延期，等待期间不占任务名额。</p>}
    {item.result && <>
      <p>实际采样：{time(item.result.started_at)} → {time(item.result.finished_at)}</p>
      <p>采样资源：{item.result.resource_status}；登记业务：{item.result.business_status}</p>
      <p>未验证：{item.result.unverified_scope.join('；')}</p>
    </>}
    <p>延时结果单独记录，不覆盖初次结论，不会自动修复或回滚。</p>
    {item.reason && <details><summary>记录原因</summary><code>{item.reason}</code></details>}
  </article>
}
