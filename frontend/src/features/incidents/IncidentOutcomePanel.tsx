import type {
  IncidentStatusResponse,
} from '../../api'
import {
  formatJsonValue,
  formatLabel,
  formatOptionalValue,
  formatTimestamp,
  outcomeTone,
} from './presentation'

interface StatusBadgeProps {
  status: string | null
}

function StatusBadge({
  status,
}: StatusBadgeProps) {
  return (
    <span
      className="outcome-status-badge"
      data-tone={outcomeTone(status)}
    >
      {status
        ? formatLabel(status)
        : 'Unknown'}
    </span>
  )
}

interface JsonDetailsProps {
  label: string
  value: unknown
}

function JsonDetails({
  label,
  value,
}: JsonDetailsProps) {
  return (
    <details className="outcome-json-details">
      <summary>{label}</summary>
      <pre>{formatJsonValue(value)}</pre>
    </details>
  )
}

interface IncidentOutcomePanelProps {
  incident: IncidentStatusResponse
}

export function IncidentOutcomePanel({
  incident,
}: IncidentOutcomePanelProps) {
  const approvalRecord =
    incident.approval_record
  const actionResult =
    incident.action_result
  const verificationResult =
    incident.verification_result

  const hasOutcome =
    approvalRecord !== null
    || actionResult !== null
    || verificationResult !== null
    || incident.approval_status === 'approved'
    || incident.approval_status === 'rejected'
    || incident.approval_status === 'failed'

  if (!hasOutcome) {
    return null
  }

  const approvalStatus =
    approvalRecord
      ? approvalRecord.approved
        ? 'approved'
        : 'rejected'
      : incident.approval_status

  return (
    <section className="content-panel outcome-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">
            Workflow outcome
          </p>
          <h2>Execution and recovery</h2>
        </div>

        <StatusBadge
          status={verificationResult?.status
            ?? actionResult?.status
            ?? approvalStatus}
        />
      </div>

      <div className="outcome-body">
        <section className="outcome-section">
          <div className="outcome-section-heading">
            <div>
              <span className="outcome-step-number">
                01
              </span>
              <h3>Approval audit</h3>
            </div>

            <StatusBadge
              status={approvalStatus}
            />
          </div>

          {approvalRecord ? (
            <>
              <dl className="outcome-facts">
                <div>
                  <dt>Approval ID</dt>
                  <dd>
                    <code>
                      {approvalRecord.approval_id}
                    </code>
                  </dd>
                </div>

                <div>
                  <dt>Approver</dt>
                  <dd>{approvalRecord.approver}</dd>
                </div>

                <div>
                  <dt>Decision</dt>
                  <dd>
                    {approvalRecord.approved
                      ? 'Approved'
                      : 'Rejected'}
                  </dd>
                </div>

                <div>
                  <dt>Decided at</dt>
                  <dd>
                    {formatTimestamp(
                      approvalRecord.decided_at,
                    )}
                  </dd>
                </div>
              </dl>

              <div className="outcome-message">
                <span>Audit comment</span>
                <p>
                  {approvalRecord.comment
                    || 'No comment provided.'}
                </p>
              </div>
            </>
          ) : (
            <p className="outcome-unavailable">
              Approval audit record is not available.
            </p>
          )}
        </section>

        <section className="outcome-section">
          <div className="outcome-section-heading">
            <div>
              <span className="outcome-step-number">
                02
              </span>
              <h3>Action execution</h3>
            </div>

            {actionResult && (
              <StatusBadge
                status={actionResult.status}
              />
            )}
          </div>

          {actionResult ? (
            <>
              <dl className="outcome-facts">
                <div>
                  <dt>Execution ID</dt>
                  <dd>
                    <code>
                      {actionResult.execution_id}
                    </code>
                  </dd>
                </div>

                <div>
                  <dt>Action</dt>
                  <dd>
                    {formatLabel(
                      actionResult.action,
                    )}
                  </dd>
                </div>

                <div>
                  <dt>Target</dt>
                  <dd>
                    {actionResult.resource_kind}/
                    {actionResult.resource_name}
                  </dd>
                </div>

                <div>
                  <dt>Namespace</dt>
                  <dd>{actionResult.namespace}</dd>
                </div>

                <div>
                  <dt>Started</dt>
                  <dd>
                    {formatTimestamp(
                      actionResult.started_at,
                    )}
                  </dd>
                </div>

                <div>
                  <dt>Finished</dt>
                  <dd>
                    {formatTimestamp(
                      actionResult.finished_at,
                    )}
                  </dd>
                </div>
              </dl>

              <div className="outcome-message">
                <span>Execution message</span>
                <p>{actionResult.message}</p>
              </div>

              {actionResult.error_message && (
                <div
                  className="outcome-error"
                  role="alert"
                >
                  <strong>
                    {actionResult.error_code
                      ?? 'EXECUTION_ERROR'}
                  </strong>
                  <p>
                    {actionResult.error_message}
                  </p>
                </div>
              )}

              <div className="outcome-details-grid">
                <JsonDetails
                  label="Applied patch"
                  value={actionResult.applied_patch}
                />

                <JsonDetails
                  label="Rollback patch"
                  value={actionResult.rollback_patch}
                />

                <JsonDetails
                  label="Before snapshot"
                  value={actionResult.before_snapshot}
                />

                <JsonDetails
                  label="After snapshot"
                  value={actionResult.after_snapshot}
                />
              </div>
            </>
          ) : approvalStatus === 'rejected' ? (
            <p className="outcome-skipped">
              Execution was not started because the
              remediation plan was rejected.
            </p>
          ) : (
            <p className="outcome-unavailable">
              Execution result is not available.
            </p>
          )}
        </section>

        <section className="outcome-section">
          <div className="outcome-section-heading">
            <div>
              <span className="outcome-step-number">
                03
              </span>
              <h3>Recovery verification</h3>
            </div>

            {verificationResult && (
              <StatusBadge
                status={verificationResult.status}
              />
            )}
          </div>

          {verificationResult ? (
            <>
              <dl className="outcome-facts verification-facts">
                <div>
                  <dt>Attempts</dt>
                  <dd>
                    {verificationResult.attempts}
                  </dd>
                </div>

                <div>
                  <dt>Desired replicas</dt>
                  <dd>
                    {formatOptionalValue(
                      verificationResult
                        .desired_replicas,
                    )}
                  </dd>
                </div>

                <div>
                  <dt>Available replicas</dt>
                  <dd>
                    {formatOptionalValue(
                      verificationResult
                        .available_replicas,
                    )}
                  </dd>
                </div>

                <div>
                  <dt>Ready Pods</dt>
                  <dd>
                    {formatOptionalValue(
                      verificationResult
                        .ready_pods,
                    )}
                  </dd>
                </div>

                <div>
                  <dt>Ready endpoints</dt>
                  <dd>
                    {formatOptionalValue(
                      verificationResult
                        .ready_endpoints,
                    )}
                  </dd>
                </div>

                <div>
                  <dt>Duration</dt>
                  <dd>
                    {formatTimestamp(
                      verificationResult.started_at,
                    )}
                    {' → '}
                    {formatTimestamp(
                      verificationResult.finished_at,
                    )}
                  </dd>
                </div>
              </dl>

              <div className="outcome-message">
                <span>Verification message</span>
                <p>{verificationResult.message}</p>
              </div>

              {verificationResult.error_message && (
                <div
                  className="outcome-error"
                  role="alert"
                >
                  <strong>
                    {verificationResult.error_code
                      ?? 'VERIFICATION_ERROR'}
                  </strong>
                  <p>
                    {verificationResult.error_message}
                  </p>
                </div>
              )}

              <div className="verification-checks">
                {verificationResult.checks.map(
                  (check) => (
                    <article
                      key={check.name}
                      className="verification-check"
                      data-passed={check.passed}
                    >
                      <div>
                        <strong>{check.name}</strong>
                        <span>
                          {check.passed
                            ? 'Passed'
                            : 'Failed'}
                        </span>
                      </div>

                      <p>{check.message}</p>

                      <dl>
                        <div>
                          <dt>Observed</dt>
                          <dd>
                            <code>
                              {formatJsonValue(
                                check.observed,
                              )}
                            </code>
                          </dd>
                        </div>

                        <div>
                          <dt>Expected</dt>
                          <dd>
                            <code>
                              {formatJsonValue(
                                check.expected,
                              )}
                            </code>
                          </dd>
                        </div>
                      </dl>
                    </article>
                  ),
                )}
              </div>
            </>
          ) : (
            <p className="outcome-unavailable">
              Recovery verification was not run.
            </p>
          )}
        </section>
      </div>
    </section>
  )
}