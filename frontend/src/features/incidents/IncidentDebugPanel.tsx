import { useState } from 'react'
import type { IncidentStatusResponse } from '../../api/types'

/** 原始调试记录仅作为文本显示，不能成为可执行的修复计划。 */
export function IncidentDebugPanel({ incident }: { incident: IncidentStatusResponse }) {
  const [notice, setNotice] = useState('')
  const text = JSON.stringify(incident, null, 2)
  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setNotice('已复制完整事件 JSON')
    } catch {
      setNotice('浏览器不支持复制，请使用下载 JSON 或选中文本复制。')
    }
  }
  function download() {
    const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `incident-${incident.incident_id}-debug.json`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  return (
    <section className="content-panel">
      <h2>调试反馈</h2>
      <p>当前阶段：{incident.phase}。包含校验错误、每次模型响应和证据；模型原文未经认可，分享前检查敏感内容。</p>
      {incident.errors.map((error, index) => (
        <pre key={index} style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
          {error.stage} / {error.code}{'\n'}{error.message}
        </pre>
      ))}
      <button type="button" onClick={copy}>复制完整 JSON</button>{' '}
      <button type="button" onClick={download}>下载 JSON</button>
      <p role="status">{notice}</p>
      <details>
        <summary>模型逐次调用原文与校验结果</summary>
        <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxHeight: 600, overflow: 'auto' }}>
          {incident.llm_debug ? JSON.stringify(incident.llm_debug, null, 2) : '此事件没有新格式的调用记录。更新后创建的新事件才会记录；规则分支可能不调用模型。'}
        </pre>
      </details>
    </section>
  )
}
