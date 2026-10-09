import type { InvestigationView } from '../../api/types'
import { evidenceElementId, formatTimestamp } from './presentation'

const actions: Record<string, string> = { collect: '补采证据', ask_user: '向你追问', conclude: '形成结论',
  propose_plan: '建议修复，仍需审批', stop: '停止调查', handoff: '交接处理' }
const tools: Record<string, string> = { pod_logs: 'Pod 日志', pod_events: 'Pod 事件', resource_summary: '资源概要',
  registered_business: '登记业务接口', deployment: 'Deployment', replica_set: 'ReplicaSet', endpoint_slice: 'EndpointSlice' }
const coverage: Record<string, string> = { observed: '已观察到结果', partial: '仅部分范围', unknown: '结果未知', baseline_snapshot: '初始采样' }
const stops: Record<string, string> = { MODEL_TOKEN_LIMIT: '剩余 Token 预算不足', ACTIVE_TIME_LIMIT: '活动时间预算不足',
  MODEL_ATTEMPT_LIMIT: '已达到模型调用尝试上限', TOOL_REQUEST_LIMIT: '已达到追加工具上限',
  INVESTIGATION_DECISION_LIMIT: '已达到调查决策上限', HUMAN_QUESTION_SKIPPED: '用户跳过了问题，保留未知项并交接',
  DECISION_VALIDATION_FAILED: '模型决定未通过程序校验，已保守停止', MODEL_REQUEST_FAILED: '模型请求失败或结果未知' }

function Citations({ ids, current }: { ids: string[]; current: ReadonlySet<string> }) {
  return <div className="citation-list">{ids.map(id => current.has(id)
    ? <a key={id} className="citation-chip" href={`#${evidenceElementId(id)}`}>{id}</a>
    : <span key={id} className="citation-chip is-missing" title="历史证据，不属于当前证据视图">{id} · 历史</span>)}</div>
}

export function InvestigationPanel({ value, evidenceIds }: { value?: InvestigationView | null; evidenceIds: string[] }) {
  if (!value) return <section className="content-panel"><p>本轮没有调查过程记录：可能尚未开始，或使用旧版流程。</p></section>
  const current = new Set(evidenceIds)
  return <section className="content-panel investigation-panel">
    <h3>本轮调查过程</h3>
    <p>这里展示已保存的行动、简短依据和采样结果。没有记录的步骤不推断为已完成，结论与审批分别查看对应环节。</p>
    <p>追问 {value.question_count} 次 · 已回答 {value.answer_count} 次</p>
    {!value.steps.length && <p>尚无已保存的调查决定。</p>}
    <ol className="investigation-steps">{value.steps.map(step => <li key={step.step}>
      <h4>第 {step.step} 步 · {actions[step.action] ?? step.action}</h4>
      {step.missing_fact && <p>需要补齐：{step.missing_fact}</p>}
      {step.reason && <p>选择依据：{step.reason}</p>}
      <Citations ids={step.evidence_ids} current={current} />
      {step.action === 'collect' && !step.results.length && <p>已选择补采，尚无整批完成记录；不能据此认为采样成功。</p>}
      {step.results.map((result, i) => <div key={i} className="investigation-result">
        <p>{tools[result.tool] ?? result.tool} · {coverage[result.coverage] ?? result.coverage} · {result.generation ? `第 ${result.generation} 次复采` : '首次采样'}</p>
        {result.error_code && <p>采样错误：{result.error_code}</p>}
        <Citations ids={result.evidence_ids} current={current} />
      </div>)}
    </li>)}</ol>
    {value.outcome && <p>调查决定：{actions[value.outcome] ?? value.outcome}。这不代表修复已执行或服务已恢复。</p>}
    {value.stop_reason && <p className="preserve-text">停止或交接原因：{stops[value.stop_reason] ?? value.stop_reason}
      {stops[value.stop_reason] && <>（<code>{value.stop_reason}</code>）</>}</p>}
    <details><summary>采样历史与当前证据（{value.observations.length} 条）</summary>
      <p>“当前”表示仍在本轮证据视图中，不保证此刻仍然有效。失败或部分结果不能证明健康。</p>
      {value.observations.map((item, i) => <article className="investigation-result" key={`${item.evidence_id}:${i}`}>
        <strong>{item.resource_type} · {item.resource_name}</strong>
        <p>{item.origin === 'baseline' ? '初始采样' : '追加采样'} · {item.current ? '当前证据视图' : '已替换或失效，保留历史'} · {item.collected_at ? formatTimestamp(item.collected_at) : '时间未记录'}</p>
        <p>{coverage[item.coverage] ?? item.coverage}{item.truncated ? ' · 内容有截断' : ''}{item.error ? ` · 错误：${item.error}` : ''}</p>
        <Citations ids={[item.evidence_id]} current={current} />
      </article>)}
      {!!value.omitted_observations && <p>另有 {value.omitted_observations} 条未在此展开，原记录仍保留。</p>}
    </details>
  </section>
}
