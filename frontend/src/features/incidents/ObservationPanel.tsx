import type { ObservationSummary } from '../../api/types'

export function ObservationPanel({ observation }: { observation?: ObservationSummary | null }) {
  if (!observation) return null
  const labels: Record<string, string> = { passed: '连续观察通过', unknown: '稳定性尚未确认', invalidated: '目标变化，观察失效', running: '观察中' }
  return <section className="workbench-record">
    <h4>{labels[observation.status] ?? observation.status}</h4>
    <p>策略 {observation.policy.version} · 连续通过 {observation.consecutive} / {observation.policy.required_consecutive} 次</p>
    <p>仅覆盖登记资源与业务接口，不代表所有副本或后续持续正常。重启会重新计连续次数。</p>
    <details><summary>采样记录（{observation.samples.length} 次）</summary>
      <ul>{observation.samples.map(sample => <li key={sample.sequence}>
        #{sample.sequence} · {sample.status} · 资源 {sample.resource_status ?? 'unknown'} / 业务 {sample.business_status ?? 'unknown'}
        <small> · {sample.started_at} → {sample.finished_at ?? '中断或未完成'}</small>
      </li>)}</ul>
    </details>
  </section>
}
