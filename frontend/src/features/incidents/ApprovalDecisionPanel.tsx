import {
  useState,
  type FormEvent,
} from 'react'

import type {
  IncidentStatusResponse,
  SubmitApprovalRequest,
} from '../../api'
import {
  buildApprovalDecision,
} from './approvalDecision'
import {
  formatLabel,
} from './presentation'

interface ApprovalDecisionPanelProps {
  incident: IncidentStatusResponse
  submitting: boolean
  error: string | null
  onSubmit: (
    request: SubmitApprovalRequest,
  ) => Promise<void>
}

export function ApprovalDecisionPanel({
  incident,
  submitting,
  error,
  onSubmit,
}: ApprovalDecisionPanelProps) {
  const [approver, setApprover] = useState('')
  const [comment, setComment] = useState('')
  const [acknowledged, setAcknowledged] =
    useState(false)
  const [validationError, setValidationError] =
    useState<string | null>(null)

  if (!incident.waiting_for_approval) {
    return null
  }

  const approvalRequest =
    incident.approval_request

  if (
    !approvalRequest
    || incident.approval_status !== 'pending'
  ) {
    return (
      <section className="content-panel approval-panel">
        <div className="panel-header">
          <div>
            <p className="eyebrow">
              Human approval
            </p>
            <h2>Approval data unavailable</h2>
          </div>
        </div>

        <div className="approval-invalid-state">
          The incident is waiting for approval, but its
          approval request is missing or inconsistent.
          Submission is disabled.
        </div>
      </section>
    )
  }

  const approvalId =
  approvalRequest.approval_id

  async function submitDecision(
    approved: boolean,
  ) {
    const result = buildApprovalDecision({
      approvalId,
      approved,
      approver,
      comment,
      acknowledged,
    })

    if (!result.ok) {
      setValidationError(result.error)
      return
    }

    setValidationError(null)
    await onSubmit(result.request)
  }

  function preventFormSubmit(
    event: FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault()
  }

  return (
    <section className="content-panel approval-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">
            Human approval
          </p>
          <h2>Review and decide</h2>
        </div>

        <span className="approval-pending-badge">
          Pending
        </span>
      </div>

      <form
        className="approval-form"
        onSubmit={preventFormSubmit}
      >
        <div className="approval-context">
          <div>
            <span>Approval ID</span>
            <code>
              {approvalRequest.approval_id}
            </code>
          </div>

          <div>
            <span>Action</span>
            <strong>
              {formatLabel(
                approvalRequest.plan.action,
              )}
            </strong>
          </div>

          <div>
            <span>Risk</span>
            <strong>
              {formatLabel(
                approvalRequest.plan.risk_level,
              )}
            </strong>
          </div>
        </div>

        <div className="approval-warning">
          <strong>Execution warning</strong>
          <p>
            Approval resumes the interrupted LangGraph
            workflow. The execution policy and action
            whitelist will still validate the proposed
            operation before any Kubernetes change.
          </p>
        </div>

        <label className="form-field">
          <span>Approver</span>
          <input
            value={approver}
            required
            maxLength={100}
            disabled={submitting}
            placeholder="Name, email or operator ID"
            onChange={(event) => {
              setApprover(event.target.value)
              setValidationError(null)
            }}
          />
        </label>

        <label className="form-field">
          <span>Decision comment</span>
          <textarea
            value={comment}
            maxLength={1000}
            rows={4}
            disabled={submitting}
            placeholder="Optional audit comment"
            onChange={(event) => {
              setComment(event.target.value)
              setValidationError(null)
            }}
          />
          <small>{comment.length}/1000</small>
        </label>

        <label className="approval-acknowledgement">
          <input
            type="checkbox"
            checked={acknowledged}
            disabled={submitting}
            onChange={(event) => {
              setAcknowledged(
                event.target.checked,
              )
              setValidationError(null)
            }}
          />

          <span>
            I reviewed the evidence, Runbook references,
            target resource, proposed parameters and
            rollback plan.
          </span>
        </label>

        {(validationError || error) && (
          <div className="approval-error" role="alert">
            {validationError ?? error}
          </div>
        )}

        <div className="approval-actions">
          <button
            type="button"
            className="reject-button"
            disabled={submitting}
            onClick={() => {
              void submitDecision(false)
            }}
          >
            {submitting
              ? 'Submitting…'
              : 'Reject plan'}
          </button>

          <button
            type="button"
            className="approve-button"
            disabled={submitting}
            onClick={() => {
              void submitDecision(true)
            }}
          >
            {submitting
              ? 'Submitting…'
              : 'Approve and execute'}
          </button>
        </div>
      </form>
    </section>
  )
}