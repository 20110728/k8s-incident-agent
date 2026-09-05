import { useState, type FormEvent } from 'react'

import type { IncidentRequest } from '../../api'

interface IncidentCreateFormProps {
  submitting: boolean
  onSubmit: (request: IncidentRequest) => Promise<void>
}

const KUBERNETES_NAME_PATTERN =
  '[a-z0-9]([-a-z0-9]*[a-z0-9])?'

export function IncidentCreateForm({
  submitting,
  onSubmit,
}: IncidentCreateFormProps) {
  const [namespace, setNamespace] =
    useState('agent-demo')
  const [serviceName, setServiceName] =
    useState('order-service')
  const [description, setDescription] =
    useState('')
  const [validationError, setValidationError] =
    useState<string | null>(null)

  async function handleSubmit(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault()

    const request: IncidentRequest = {
      namespace: namespace.trim(),
      service_name: serviceName.trim(),
      description: description.trim(),
    }

    if (
      !request.namespace ||
      !request.service_name ||
      !request.description
    ) {
      setValidationError('请填写所有必填字段。')
      return
    }

    setValidationError(null)
    await onSubmit(request)
  }

  function clearValidationError() {
    if (validationError) {
      setValidationError(null)
    }
  }

  return (
    <section className="content-panel incident-form-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">Incident intake</p>
          <h2>Create incident</h2>
        </div>
      </div>

      <form
        className="incident-form"
        onSubmit={handleSubmit}
        noValidate={false}
      >
        <label className="form-field">
          <span>Namespace</span>
          <input
            name="namespace"
            value={namespace}
            required
            maxLength={63}
            pattern={KUBERNETES_NAME_PATTERN}
            disabled={submitting}
            spellCheck={false}
            onChange={(event) => {
              setNamespace(event.target.value)
              clearValidationError()
            }}
          />
          <small>
            Kubernetes namespace，例如 agent-demo。
          </small>
        </label>

        <label className="form-field">
          <span>Service name</span>
          <input
            name="service_name"
            value={serviceName}
            required
            maxLength={63}
            pattern={KUBERNETES_NAME_PATTERN}
            disabled={submitting}
            spellCheck={false}
            onChange={(event) => {
              setServiceName(event.target.value)
              clearValidationError()
            }}
          />
          <small>
            当前要调查的 Kubernetes Service。
          </small>
        </label>

        <label className="form-field">
          <span>Incident description</span>
          <textarea
            name="description"
            value={description}
            required
            maxLength={1000}
            rows={6}
            disabled={submitting}
            placeholder="描述故障现象、告警信息以及期望调查的内容。"
            onChange={(event) => {
              setDescription(event.target.value)
              clearValidationError()
            }}
          />
          <small>{description.length}/1000</small>
        </label>

        {validationError && (
          <p className="form-error" role="alert">
            {validationError}
          </p>
        )}

        <div className="form-actions">
          <button
            className="primary-button"
            type="submit"
            disabled={submitting}
          >
            {submitting
              ? 'Creating incident…'
              : 'Create incident'}
          </button>

          <span className="form-help">
            创建操作可能需要等待证据采集、检索和诊断完成。
          </span>
        </div>
      </form>
    </section>
  )
}