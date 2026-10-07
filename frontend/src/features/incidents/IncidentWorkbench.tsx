import { useEffect, useRef, useState } from 'react'
import { ApiClient } from '../../api'
import type { CommandReceipt, IncidentStatusResponse, InteractionIntent, InteractionResult, Message, Operation, Question, Recheck, RunSummary, SubmitApprovalRequest } from '../../api/types'
import { ApprovalDecisionPanel } from './ApprovalDecisionPanel'
import { IncidentAnalysis } from './IncidentAnalysis'
import { IncidentDebugPanel } from './IncidentDebugPanel'
import { IncidentOutcomePanel } from './IncidentOutcomePanel'
import { ObservationPanel } from './ObservationPanel'
import { WORKSPACE_STAGES, conversationEntries, initialStage, taskLabel, type WorkspaceStage } from './workspaceNavigation'
import { evidenceElementId } from './presentation'
import { commandPermissions, createClientKey, defaultContent, definiteRejection, describeError, isMissing, mergeBy, pendingKey, postCommand, readPending, recoverCommand, type PendingCommand } from './workbenchState'
import './workbench.css'

const api = new ApiClient({ timeoutMs: 15_000 })
const time = (value?: string | null) => value ? new Date(value).toLocaleString() : '时间未记录'
const commandNames: Record<InteractionIntent, string> = { auto: '发送消息', explain: '解释依据', compare: '比较两轮',
  supplement: '仅补充信息', investigate: '继续调查', recheck: '重新检查', observe: '观察稳定性', stop: '停止调查' }

function QuestionForm({ question, disabled, submit }: {
  question: Question; disabled: boolean; submit: (answers: Record<string, string>, skip: boolean) => void
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({})
  return <form className="question-card" onSubmit={e => { e.preventDefault(); submit(answers, false) }}>
    <h3>需要你补充信息 · 第 {question.version} 轮</h3>
    <p>{question.reason}</p>
    {question.questions.map(q => <label key={q.slot}>{q.text}
      <textarea required maxLength={2000} disabled={disabled} value={answers[q.slot] ?? ''}
        onChange={e => setAnswers(old => ({ ...old, [q.slot]: e.target.value }))} />
    </label>)}
    <div className="workbench-actions">
      <button disabled={disabled || question.questions.some(q => !answers[q.slot]?.trim())}>回答并继续</button>
      <button type="button" disabled={disabled} onClick={() => submit({}, true)}>不知道，跳过追问</button>
    </div>
    <small>回答保存为未核实陈述。继续后会重新采集；跳过可能保留未知结论。</small>
  </form>
}

export function InteractionCard({ item, onCitation }: { item: InteractionResult; onCitation?: (run: string | null, evidence: string) => void }) {
  const output = item.output
  return <article className="workbench-record">
    <h4>{output?.intent ?? '交互任务'} · {item.run.status}</h4>
    <small>{time(item.run.updated_at)} · {item.run.run_id}</small>
    {item.run.last_error_code && <p role="alert">任务未完成：{item.run.last_error_code}。不能据此判断业务已经恢复。</p>}
    {output?.answer && <p className="preserve-text">{output.answer}</p>}
    {output?.historical_only && <p>这是指定轮次的历史解释，没有重新采集现场。</p>}
    {output?.reason && <p>处理依据：{output.reason}</p>}
    {output?.reference_snapshots?.map(ref => <p key={ref.run_id ?? 'legacy'}>引用轮次 {ref.run_id ?? '原始事件'} · 快照读取于 {time(ref.snapshot_at)}</p>)}
    {!!output?.citations?.length && <ul>{output.citations.map(c => <li key={c.citation_id}>
      {onCitation ? <button className="citation-link" onClick={() => onCitation(c.run_id, c.citation_id.slice(c.citation_id.indexOf(':') + 1))}>证据 {c.citation_id}</button> : <>证据 {c.citation_id}</>} · 采样于 {time(c.collected_at)}
    </li>)}</ul>}
    {!!output?.unknowns?.length && <p>尚未核实：{output.unknowns.join('；')}</p>}
    {output?.diagnosis_run_id && <p>新调查已创建：{output.diagnosis_run_id}，可切换到当前轮次查看。</p>}
    {!!item.calls.length && <details><summary>调用用量（未计价）</summary>
      <ul>{item.calls.map((call, i) => <li key={i}>{call.purpose} · {call.model} · {call.status} · {call.elapsed_ms ?? '?'} ms ·
        {call.usage && Object.keys(call.usage).length ? JSON.stringify(call.usage) : '用量未返回，不能按零计算'}</li>)}</ul>
    </details>}
  </article>
}

export function RecheckCard({ item }: { item: Recheck }) {
  return <article className="workbench-record">
    <h4>独立复查 · {item.status}</h4>
    <p>采样开始 {time(item.started_at)}；完成 {time(item.finished_at)}</p>
    <p>资源：{item.resource_status}；登记业务：{item.business_status}</p>
    <ObservationPanel observation={item.observation} />
    <p>目标对比：{item.target_comparison.status}。本次观察不覆盖原结论，也不能证明此前修复导致了恢复。</p>
    <p className="preserve-text">人工处理说明（未核实）：{item.note || '无'}</p>
    <p>未验证范围：{item.unverified_scope.join('；') || '未提供范围说明'}</p>
    {item.error_code && <p role="alert">复查错误：{item.error_code}</p>}
    <details><summary>目标变化和采集错误</summary><pre>{JSON.stringify({ changes: item.target_comparison.changes, errors: item.collection_errors }, null, 2)}</pre></details>
  </article>
}

interface Props {
  incident: IncidentStatusResponse
  onCurrent: (value: IncidentStatusResponse) => void
  onApproval: (request: SubmitApprovalRequest) => Promise<void>
  approving: boolean
  approvalError: string | null
}

export function IncidentWorkbench({ incident, onCurrent, onApproval, approving, approvalError }: Props) {
  const id = incident.incident_id
  const queuedMode = incident.execution_mode === 'queued'
  const controlKey = `incident-agent.control.v1.${id}`
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [messages, setMessages] = useState<Message[]>([])
  const [rechecks, setRechecks] = useState<Recheck[]>([])
  const [operations, setOperations] = useState<Operation[]>([])
  const [interactions, setInteractions] = useState<InteractionResult[]>([])
  const interactionCache = useRef(new Map<string, InteractionResult>())
  const [runCursor, setRunCursor] = useState<string | null | undefined>(undefined)
  const [messageCursor, setMessageCursor] = useState<number | null | undefined>(undefined)
  const [recheckCursor, setRecheckCursor] = useState<number | null | undefined>(undefined)
  const [selected, setSelected] = useState(() => new URLSearchParams(location.search).get('run_id') || 'current')
  const [stage, setStage] = useState<WorkspaceStage>(() => initialStage(location.search, incident))
  const [chatOpen, setChatOpen] = useState(() => {
    const saved = new URLSearchParams(location.search).get('chat')
    return saved ? saved !== 'closed' : !(typeof matchMedia === 'function' && matchMedia('(max-width: 1050px)').matches)
  })
  const [citationTarget, setCitationTarget] = useState<string | null>(null)
  const [citationNotice, setCitationNotice] = useState('')
  const conversationRef = useRef<HTMLDivElement>(null)
  const followChat = useRef(true)
  const previousScroll = useRef<{ height: number; top: number } | null>(null)
  const [historical, setHistorical] = useState<IncidentStatusResponse | null>(null)
  const [historyError, setHistoryError] = useState('')
  const [compare, setCompare] = useState('')
  const [content, setContent] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [fresh, setFresh] = useState(false)
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [revision, setRevision] = useState(0)
  const [pending, setPending] = useState<PendingCommand | null>(null)
  const [storageError, setStorageError] = useState('')
  const [receipt, setReceipt] = useState<CommandReceipt | null>(null)
  const trackedControl = useRef<string | null>(null)
  const [paging, setPaging] = useState(false)
  const permissions = commandPermissions(incident, runs, operations,
    !!receipt && 'control_id' in receipt && receipt.status === 'pending')
  const selectedCurrent = selected === 'current'
  const shown = selectedCurrent ? incident : historical
  const currentRef = incident.run?.run_id ?? 'legacy'
  const reference = selectedCurrent ? currentRef : selected
  const locked = busy || !!pending || !!storageError || !fresh || approving

  function chooseStage(next: WorkspaceStage) {
    setStage(next)
    if (typeof matchMedia === 'function' && matchMedia('(max-width: 1050px)').matches) setChatOpen(false)
  }
  function viewCitation(run: string | null, evidence: string) {
    const next = run === currentRef ? 'current' : run ?? 'legacy'
    if (next !== selected) { setHistorical(null); setSelected(next) }
    setCitationTarget(evidenceElementId(evidence)); setCitationNotice('')
    chooseStage('evidence')
  }
  useEffect(() => {
    const url = new URL(location.href)
    url.searchParams.set('stage', stage)
    url.searchParams.set('chat', chatOpen ? 'open' : 'closed')
    history.replaceState(history.state, '', url)
  }, [stage, chatOpen])
  useEffect(() => {
    if (!citationTarget || stage !== 'evidence' || !shown) return
    const target = document.getElementById(citationTarget)
    if (target) target.scrollIntoView({ block: 'center' })
    else setCitationNotice('本轮快照中未找到该引用，请查看原回答的轮次和证据编号。')
    setCitationTarget(null)
  }, [citationTarget, stage, shown])
  useEffect(() => {
    const view = conversationRef.current
    if (!view || !chatOpen) return
    if (previousScroll.current) {
      view.scrollTop = previousScroll.current.top + view.scrollHeight - previousScroll.current.height
      previousScroll.current = null
    } else if (followChat.current) view.scrollTop = view.scrollHeight
  }, [messages, interactions, chatOpen])

  // Reload and browser reopen only restore a GET lookup obligation, never a POST.
  useEffect(() => {
    try { setPending(readPending(localStorage, id)); trackedControl.current = localStorage.getItem(controlKey) }
    catch (e) { setStorageError(describeError(e)) }
  }, [id, controlKey])

  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        if (!queuedMode) {
          const current = await api.getIncident(id)
          if (!cancelled) { onCurrent(current); setFresh(true); setError('') }
          return
        }
        const [current, runPage, messagePage, recheckPage, operationPage] = await Promise.all([
          api.getIncident(id), api.listRuns(id), api.listMessages(id), api.listRechecks(id), api.listOperations(id),
        ])
        const updated = await Promise.all(runPage.items.filter(r => r.run_kind === 'interaction'
          && interactionCache.current.get(r.run_id)?.run.updated_at !== r.updated_at).map(r => api.getInteraction(id, r.run_id)))
        let tracked: CommandReceipt | null = null
        if (trackedControl.current) {
          try { tracked = await api.findCommand(id, trackedControl.current, true) }
          catch (e) {
            if (!isMissing(e)) throw e
            if (!cancelled) {
              trackedControl.current = null
              localStorage.removeItem(controlKey)
              setNotice('此前控制记录未找到，请检查消息和轮次；没有重新提交。')
            }
          }
        }
        if (cancelled) return
        if (tracked) setReceipt(tracked)
        updated.forEach(item => interactionCache.current.set(item.run.run_id, item))
        setInteractions([...interactionCache.current.values()])
        onCurrent(current)
        setRuns(old => mergeBy(old, runPage.items, x => x.run_id).sort((a, b) => b.created_at.localeCompare(a.created_at) || b.run_id.localeCompare(a.run_id)))
        setMessages(old => mergeBy(old, messagePage.items, x => x.message_id).sort((a, b) => a.sequence - b.sequence))
        setRechecks(old => mergeBy(old, recheckPage.items, x => x.recheck_id))
        setOperations(operationPage.items)
        setRunCursor(old => old === undefined ? runPage.next_cursor : old)
        setMessageCursor(old => old === undefined ? messagePage.next_before_sequence : old)
        setRecheckCursor(old => old === undefined ? recheckPage.next_before_sequence : old)
        setFresh(true)
        setError('')
      } catch (e) {
        if (!cancelled) { setFresh(false); setError(`状态同步失败，操作暂时禁用：${describeError(e)}`) }
      } finally {
        if (!cancelled) timer = setTimeout(() => void poll(), 3000)
      }
    }
    void poll()
    return () => { cancelled = true; clearTimeout(timer) }
  }, [id, onCurrent, revision, queuedMode, controlKey])

  useEffect(() => {
    const url = new URL(location.href)
    if (selected === 'current') url.searchParams.delete('run_id')
    else url.searchParams.set('run_id', selected)
    history.replaceState(history.state, '', url)
    setHistorical(null)
    setHistoryError('')
    if (selected === 'current') return
    let cancelled = false
    void api.getRound(id, selected).then(value => { if (!cancelled) setHistorical(value.result) })
      .catch(e => { if (!cancelled) setHistoryError(describeError(e)) })
    return () => { cancelled = true }
  }, [id, selected])

  function received(value: CommandReceipt) {
    setReceipt(value)
    if ('control_id' in value) {
      const saved = readPending(localStorage, id)
      if (saved) {
        trackedControl.current = saved.body.client_message_id
        localStorage.setItem(controlKey, saved.body.client_message_id)
      }
    } else {
      trackedControl.current = null
      localStorage.removeItem(controlKey)
    }
    localStorage.removeItem(pendingKey(id))
    setPending(null)
    setFresh(false) // Keep stale approvals disabled until a new server read.
    setNotice('请求已找回或已保存。后台任务状态以下次同步为准。')
    setRevision(x => x + 1)
  }
  async function execute(command: PendingCommand | null, retry = false) {
    if (busyRef.current) return
    busyRef.current = true
    setBusy(true)
    try {
      if (command) {
        // Fail before POST if browser storage cannot preserve the recovery key.
        localStorage.setItem(pendingKey(id), JSON.stringify(command))
        setPending(command)
      } else command = pending
      if (!command) return
      let value: CommandReceipt
      if (retry) value = await recoverCommand(api, command, true)
      else {
        try { value = await postCommand(api, command) }
        catch (e) {
          if (definiteRejection(e)) throw e
          value = await recoverCommand(api, command)
        }
      }
      received(value)
      setContent('')
    } catch (e) {
      if (definiteRejection(e)) {
        localStorage.removeItem(pendingKey(id)); setPending(null)
        setNotice(`请求未接受：${describeError(e)}。请刷新状态后调整，不要重复批准旧方案。`)
        setFresh(false); setRevision(x => x + 1)
      } else setNotice(`结果尚未确认：${describeError(e)}。请先查询；重试会使用原请求编号。`)
    } finally { busyRef.current = false; setBusy(false) }
  }

  useEffect(() => {
    if (!pending || busyRef.current) return
    let cancelled = false
    void recoverCommand(api, pending).then(value => {
      if (!cancelled) received(value)
    }).catch(e => {
      if (!cancelled) setNotice(isMissing(e) ? '暂未找到原请求，可稍后查询或用原编号重试。' : `原请求仍待确认：${describeError(e)}`)
    })
    return () => { cancelled = true }
    // Only the stored request changing triggers recovery; never replay POST.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending])

  async function lookup() {
    if (!pending || busyRef.current) return
    busyRef.current = true; setBusy(true)
    try { received(await recoverCommand(api, pending)) }
    catch (e) { setNotice(isMissing(e) ? '暂未找到请求。可以稍后再查，或用原编号重试。' : describeError(e)) }
    finally { busyRef.current = false; setBusy(false) }
  }
  function send(intent: InteractionIntent) {
    if (locked || !permissions[intent] || (!selectedCurrent && !['explain', 'compare'].includes(intent))) return
    const text = content.trim() || defaultContent(intent)
    if (!text || (intent === 'compare' && (!compare || compare === reference))) return
    void execute({ incidentId: id, kind: 'interaction', body: {
      client_message_id: createClientKey(), content: text, intent,
      ...(['explain', 'compare'].includes(intent) ? { reference_run_id: reference } : {}),
      ...(intent === 'compare' ? { compare_run_id: compare } : {}),
    } })
  }
  async function more(kind: 'runs' | 'messages' | 'rechecks') {
    if (paging) return
    setPaging(true)
    try {
      if (kind === 'runs' && runCursor) {
        const page = await api.listRuns(id, runCursor)
        const results = await Promise.all(page.items.filter(r => r.run_kind === 'interaction').map(r => api.getInteraction(id, r.run_id)))
        results.forEach(item => interactionCache.current.set(item.run.run_id, item))
        if (conversationRef.current) previousScroll.current = { height: conversationRef.current.scrollHeight, top: conversationRef.current.scrollTop }
        setInteractions([...interactionCache.current.values()])
        setRuns(old => mergeBy(old, page.items, x => x.run_id)); setRunCursor(page.next_cursor)
      } else if (kind === 'messages' && messageCursor) {
        const page = await api.listMessages(id, messageCursor)
        if (conversationRef.current) previousScroll.current = { height: conversationRef.current.scrollHeight, top: conversationRef.current.scrollTop }
        setMessages(old => mergeBy(old, page.items, x => x.message_id).sort((a, b) => a.sequence - b.sequence)); setMessageCursor(page.next_before_sequence)
      } else if (kind === 'rechecks' && recheckCursor) {
        const page = await api.listRechecks(id, recheckCursor)
        setRechecks(old => mergeBy(old, page.items, x => x.recheck_id)); setRecheckCursor(page.next_before_sequence)
      }
    } catch (e) { setNotice(`历史读取失败，可重试：${describeError(e)}`) }
    finally { setPaging(false) }
  }
  const diagnosisRuns = runs.filter(r => r.run_kind === 'diagnosis')
  const stageIndex = WORKSPACE_STAGES.findIndex(s => s.id === stage)
  const entries = conversationEntries(messages, interactions)
  return <div className="incident-workbench" data-chat={chatOpen ? 'open' : 'closed'}>
    <header className="event-header">
      <a className="home-link" href={location.pathname || '/'}>← 事件列表</a>
      <div className="event-heading"><h1>{incident.request.service_name}</h1><small>{incident.request.namespace} · {id}</small></div>
      <span className="phase-badge" data-status={incident.run?.status}>{taskLabel(incident.run?.status, incident.phase)}</span>
      <label className="round-picker">查看轮次 <select value={selected} onChange={e => {
        if (e.target.value !== selected) { setHistorical(null); setSelected(e.target.value) }
      }}>
        <option value="current">当前轮次</option><option value="legacy">原始事件记录</option>
        {!['current', 'legacy'].includes(selected) && !diagnosisRuns.some(r => r.run_id === selected) && <option value={selected}>{selected}</option>}
        {diagnosisRuns.map(r => <option key={r.run_id} value={r.run_id}>{time(r.created_at)} · {r.run_id.slice(0, 8)}</option>)}
      </select></label>
      <button className="chat-toggle" aria-expanded={chatOpen} aria-controls="event-conversation" onClick={() => setChatOpen(x => !x)}>
        {chatOpen ? '收起对话' : '打开对话'}{incident.run?.question ? ' · 有待回答问题' : pending ? ' · 请求待确认' : ''}
      </button>
    </header>
    <div className="event-statusbar">
      <span>{incident.execution_mode ?? 'sync'} · worker {incident.worker_available ? '在线' : '未在线或未报告'}</span>
      <span>{fresh ? '状态已同步' : '状态待同步，暂不能提交操作'}</span>
      <button onClick={() => setRevision(x => x + 1)}>刷新状态</button>
      {runCursor && <button disabled={paging} onClick={() => void more('runs')}>更早轮次及交互</button>}
    </div>
    <div className="event-notices">
      {error && <p role="alert">{error}</p>}
      {storageError && <p role="alert">{storageError}</p>}
      {!permissions.enabled && <p>多轮交互需要 queued 模式；保留原有同步流程。</p>}
      {incident.worker_available === false && permissions.enabled && <p>任务可以保存，worker 上线后才会执行。</p>}
      {!selectedCurrent && <p className="workbench-warning">正在查看历史快照，不能在这里审批、回答或启动现场操作。请切回“当前轮次”。</p>}
      {historyError && <p role="alert">历史读取失败：{historyError}</p>}
      {(incident.run?.invalidated_at || incident.run?.stop_requested) && <p className="workbench-warning">当前轮次已请求停止或被新信息替代。旧方案仅供追溯，已发出的操作仍须核对结果。</p>}
      {operations.filter(o => ['dispatching', 'outcome_unknown', 'manual_required'].includes(o.state)).map(o => <p className="workbench-warning" key={o.operation_id}>
        操作 {o.operation_id}：{o.state}。修复请求可能已经发出，停止不能撤回它；先核对结果，不能用新调查绕过。{o.error_code}
      </p>)}
      {!chatOpen && (pending || notice) && <p role="status">{pending ? '有请求尚未确认，请打开对话查询。' : notice}</p>}
    </div>
    <div className="event-columns">
      <nav className="stage-navigation" aria-label="事件环节">
        {WORKSPACE_STAGES.map(s => <button key={s.id} aria-current={stage === s.id ? 'page' : undefined} onClick={() => chooseStage(s.id)}>{s.label}</button>)}
      </nav>
      <section className="stage-content" aria-label={WORKSPACE_STAGES[stageIndex].label} onClick={event => {
        // Existing diagnosis/plan citations were same-page anchors. Mount the
        // evidence page first, then scroll to that exact evidence or runbook.
        const link = (event.target as HTMLElement).closest<HTMLAnchorElement>('a[href^="#evidence-"],a[href^="#runbook-"]')
        if (link) { event.preventDefault(); setCitationNotice(''); setCitationTarget(link.hash.slice(1)); chooseStage('evidence') }
      }}>
        <div className="stage-heading"><h2>{WORKSPACE_STAGES[stageIndex].label}</h2>
          {shown && <small>本轮结果时间：{time(shown.run?.finished_at ?? shown.run?.updated_at)}</small>}</div>
        {!shown && !historyError && <p role="status">正在读取本轮快照……</p>}
        {shown && <>
          {stage === 'overview' && <section className="content-panel overview-panel">
            <h3>原始描述</h3><p className="preserve-text">{incident.request.description}</p>
            <h3>本轮进展</h3><p>{taskLabel(shown.run?.status, shown.phase)}</p>
            <p>证据 {shown.evidence.length} 条 · 诊断：{shown.diagnosis?.fault_category ?? '尚未形成结论'}</p>
            {selectedCurrent && permissions.answer && <button onClick={() => setChatOpen(true)}>回答待补充的问题 →</button>}
            {selectedCurrent && incident.waiting_for_approval && <button onClick={() => chooseStage('approval')}>查看待审批方案 →</button>}
            {shown.diagnosis && <button onClick={() => chooseStage('diagnosis')}>查看诊断依据 →</button>}
            <details><summary>任务详情</summary><p>run：{shown.run?.run_id ?? '旧事件，无任务编号'}</p><p>更新时间：{time(shown.run?.updated_at)}</p><p>错误：{shown.run?.last_error_code ?? '无'}</p></details>
          </section>}
          {['diagnosis', 'evidence', 'plan'].includes(stage) && <>
            {citationNotice && <p role="alert">{citationNotice}</p>}
            <IncidentAnalysis incident={shown} section={stage as 'diagnosis' | 'evidence' | 'plan'} />
            {stage === 'diagnosis' && <div className="workbench-actions"><button onClick={() => {
              setChatOpen(true); setContent(`请解释${selectedCurrent ? '当前轮次' : '所选历史轮次'}的诊断依据及未验证内容。`)
            }}>在对话中解释这一轮</button><button onClick={() => chooseStage('plan')}>查看修复方案 →</button></div>}
            {stage === 'plan' && selectedCurrent && incident.waiting_for_approval && <button onClick={() => chooseStage('approval')}>核对方案后进入审批 →</button>}
          </>}
          {stage === 'approval' && (selectedCurrent ? <>
            {!incident.waiting_for_approval && <p className="content-panel">当前没有可提交的待审批方案。已决定或已失效的方案不能重新授权。</p>}
            <fieldset disabled={locked} className="approval-fieldset"><ApprovalDecisionPanel key={incident.approval_request?.approval_id ?? 'none'}
              incident={incident} submitting={approving} error={approvalError} onSubmit={async request => { if (!locked) await onApproval(request) }} /></fieldset>
          </> : <p className="content-panel">历史轮次只读，不提供审批入口。</p>)}
          {stage === 'results' && <>
            <IncidentOutcomePanel incident={shown} />
            <section className="content-panel recheck-history"><h3>事件的独立复查历史</h3>
              <p>以下观察属于整个事件，各自保留采样时间，不覆盖所选轮次的结论。</p>
              {!rechecks.length && <p>尚无复查记录。</p>}
              {[...rechecks].sort((a, b) => b.finished_at.localeCompare(a.finished_at)).map(item => <RecheckCard key={item.recheck_id} item={item} />)}
              {recheckCursor && <button disabled={paging} onClick={() => void more('rechecks')}>更早复查</button>}
            </section>
          </>}
          {stage === 'debug' && <><section className="content-panel"><h3>本轮调用用量（未计价）</h3>
            <p>诊断：{Object.keys(shown.llm_usage).length ? JSON.stringify(shown.llm_usage) : '未记录'}</p>
            <p>方案：{Object.keys(shown.remediation_llm_usage).length ? JSON.stringify(shown.remediation_llm_usage) : '未记录'}</p></section>
            <IncidentDebugPanel incident={shown} /></>}
        </>}
        <footer className="stage-pagination"><button disabled={stageIndex === 0} onClick={() => chooseStage(WORKSPACE_STAGES[stageIndex - 1].id)}>← 上一环节</button>
          <span>{stageIndex + 1} / {WORKSPACE_STAGES.length}</span><button disabled={stageIndex === WORKSPACE_STAGES.length - 1} onClick={() => chooseStage(WORKSPACE_STAGES[stageIndex + 1].id)}>下一环节 →</button></footer>
      </section>
      <aside id="event-conversation" className="event-conversation" hidden={!chatOpen} aria-label="事件对话">
        <div className="conversation-heading"><h2>事件对话</h2><small>{selectedCurrent ? '当前轮次' : '正在引用历史轮次'} · 切换环节不会清空对话</small></div>
        <div className="conversation-history" ref={conversationRef} onScroll={e => {
          const view = e.currentTarget; followChat.current = view.scrollHeight - view.scrollTop - view.clientHeight < 80
        }}>
          <div className="conversation-paging">
            {messageCursor && <button disabled={paging} onClick={() => void more('messages')}>更早消息</button>}
            {runCursor && <button disabled={paging} onClick={() => void more('runs')}>更早交互</button>}
          </div>
          {!entries.length && <p>可以询问诊断依据，或补充新的现场信息。</p>}
          {entries.map(entry => entry.kind === 'interaction' ? <div className="chat-bubble assistant" key={entry.key}>
            <InteractionCard item={entry.interaction} onCitation={viewCitation} />
          </div> : <article className={`chat-bubble ${entry.message.role === 'user' ? 'user' : 'assistant'}`} key={entry.key}>
            <small>{entry.message.role === 'user' ? '你' : entry.message.role === 'assistant' ? 'Agent' : '工具'} · {time(entry.message.created_at)} · #{entry.message.sequence}</small>
            <p className="preserve-text">{entry.message.content}</p>
            {entry.message.source === 'user_supplied' && <small>用户陈述，尚未核实。{entry.message.adopted_by_run_ids.length ? `已被诊断采用：${entry.message.adopted_by_run_ids.join('、')}` : '已保存，尚无诊断采用记录。'}</small>}
            {!!entry.message.evidence_refs.length && <small>引用：{entry.message.evidence_refs.join('、')}</small>}
          </article>)}
          {receipt && 'control_id' in receipt && <div className="chat-bubble assistant"><p>控制请求：{receipt.action ?? '回答'} · {receipt.status}</p>
            <p>{receipt.deferred_until_write_checked ? '须等已发修复结果核对后才能继续。' : '请求已保存，以当前任务状态为准。'}</p>
            {receipt.diagnosis_run_id && <small>新轮次：{receipt.diagnosis_run_id}</small>}</div>}
          {selectedCurrent && permissions.answer && incident.run?.question && <QuestionForm
            key={`${incident.run.question.question_id}:${incident.run.question.version}`} question={incident.run.question} disabled={locked}
            submit={(answers, skip) => { if (locked) return; void execute({ kind: 'answer', incidentId: id, runId: incident.run!.run_id,
              body: { client_message_id: createClientKey(), content: skip ? '不知道，跳过追问' : '回答追问',
                question_id: incident.run!.question!.question_id, version: incident.run!.question!.version, answers, skip } }) }} />}
        </div>
        <div className="conversation-composer">
          {pending && <div className="workbench-warning" role="status">有请求尚未确认：{pending.body.client_message_id}。刷新不会重发。
            <div className="workbench-actions"><button disabled={busy} onClick={() => void lookup()}>查询结果</button><button disabled={busy} onClick={() => void execute(null, true)}>查询后原编号重试</button></div></div>}
          {notice && <p role="status">{notice}</p>}
          <small>重新检查：单次采样。观察稳定性：限时连续采样，无模型调用；完成后在“执行与复查”查看。</small>
          <div className="workbench-actions">{(['explain', 'investigate', 'recheck', 'observe', 'stop'] as InteractionIntent[]).map(intent => <button key={intent}
            disabled={locked || !permissions[intent] || (!selectedCurrent && intent !== 'explain')} onClick={() => send(intent)}>{commandNames[intent]}</button>)}</div>
          <form onSubmit={e => { e.preventDefault(); send(selectedCurrent ? 'auto' : 'explain') }}>
            <label>消息或人工处理说明<textarea value={content} maxLength={2000} disabled={locked}
              onChange={e => setContent(e.target.value)} placeholder="补充信息或提出问题；聊天不能批准修复。" /></label>
            <div className="workbench-actions"><button className="primary-button" disabled={locked || !content.trim() || !(selectedCurrent ? permissions.auto : permissions.explain)}>{selectedCurrent ? '发送消息' : '解释所选轮次'}</button>
              <button type="button" disabled={locked || !permissions.supplement || !selectedCurrent || !content.trim()} onClick={() => send('supplement')}>仅保存补充</button></div>
          </form>
          {selectedCurrent && !permissions.auto && <small>当前阶段请使用明确操作按钮；批准修复请前往审批页。</small>}
          <details><summary>比较历史轮次</summary><label>比较另一轮<select value={compare} onChange={e => setCompare(e.target.value)}>
            <option value="">选择轮次</option><option value="legacy">原始事件</option>
            {diagnosisRuns.map(r => <option value={r.run_id} key={r.run_id}>{time(r.created_at)} · {r.run_id.slice(0, 8)}</option>)}
          </select></label><button disabled={locked || !permissions.compare || !compare || compare === reference} onClick={() => send('compare')}>比较两轮</button></details>
        </div>
      </aside>
    </div>
  </div>
}
