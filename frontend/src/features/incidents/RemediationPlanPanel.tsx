import type {
  IncidentStatusResponse,
  RemediationPlan,
} from '../../api'
import {
  evidenceElementId,
  formatLabel,
  formatLabelPairs,
  formatOptionalValue,
  runbookElementId,
} from './presentation'

interface ReferenceLinksProps {
  identifiers: readonly string[]
  availableIdentifiers: ReadonlySet<string>
  kind: 'evidence' | 'runbook'
}

function ReferenceLinks({
  identifiers,
  availableIdentifiers,
  kind,
}: ReferenceLinksProps) {
  if (identifiers.length === 0) {
    return (
      <p className="citation-empty">
        No references declared.
      </p>
    )
  }

  return (
    <div className="citation-list">
      {identifiers.map((identifier) => {
        const available =
          availableIdentifiers.has(identifier)

        const targetId =
          kind === 'evidence'
            ? evidenceElementId(identifier)
            : runbookElementId(identifier)

        if (!available) {
          return (
            <span
              key={identifier}
              className="citation-chip is-missing"
              title="The referenced item is missing from the response."
            >
              {identifier}
            </span>
          )
        }

        return (
          <a
            key={identifier}
            className="citation-chip"
            href={`#${targetId}`}
          >
            {identifier}
          </a>
        )
      })}
    </div>
  )
}

interface ActionParametersProps {
  plan: RemediationPlan
}

function ActionParameters({
  plan,
}: ActionParametersProps) {
  const parameters = plan.parameters

  if (plan.action === 'patch_readiness_probe') {
    return (
      <div className="change-grid">
        <article className="change-card">
          <span>Current probe</span>

          <dl>
            <div>
              <dt>Container</dt>
              <dd>
                {formatOptionalValue(
                  parameters.container_name,
                )}
              </dd>
            </div>

            <div>
              <dt>Path</dt>
              <dd>
                {formatOptionalValue(
                  parameters.current_probe_path,
                )}
              </dd>
            </div>

            <div>
              <dt>Port</dt>
              <dd>
                {formatOptionalValue(
                  parameters.current_probe_port,
                )}
              </dd>
            </div>
          </dl>
        </article>

        <article className="change-card is-proposed">
          <span>Proposed probe</span>

          <dl>
            <div>
              <dt>Path</dt>
              <dd>
                {formatOptionalValue(
                  parameters.proposed_probe_path,
                )}
              </dd>
            </div>

            <div>
              <dt>Port</dt>
              <dd>
                {formatOptionalValue(
                  parameters.proposed_probe_port,
                )}
              </dd>
            </div>
          </dl>
        </article>
      </div>
    )
  }

  if (plan.action === 'patch_service_selector') {
    return (
      <div className="change-grid">
        <article className="change-card">
          <span>Current selector</span>
          <code className="selector-value">
            {formatLabelPairs(
              parameters.current_selector,
            )}
          </code>
        </article>

        <article className="change-card is-proposed">
          <span>Proposed selector</span>
          <code className="selector-value">
            {formatLabelPairs(
              parameters.proposed_selector,
            )}
          </code>
        </article>
      </div>
    )
  }

  return (
    <div className="change-card">
      <span>Manual investigation steps</span>

      {parameters.investigation_steps.length > 0 ? (
        <ol className="investigation-steps">
          {parameters.investigation_steps.map(
            (step, index) => (
              <li key={`${index}-${step}`}>
                {step}
              </li>
            ),
          )}
        </ol>
      ) : (
        <p className="citation-empty">
          No investigation steps provided.
        </p>
      )}
    </div>
  )
}

interface RemediationPlanPanelProps {
  incident: IncidentStatusResponse
}

export function RemediationPlanPanel({
  incident,
}: RemediationPlanPanelProps) {
  const plan = incident.remediation_plan

  const availableEvidenceIds = new Set(
    incident.evidence.map(
      (item) => item.evidence_id,
    ),
  )

  const availableRunbookIds = new Set(
    incident.retrieved_runbooks.flatMap(
      (item) =>
        item.runbook_id
          ? [item.runbook_id]
          : [],
    ),
  )

  const approvalLabel =
    incident.waiting_for_approval
      ? 'Approval pending'
      : plan?.requires_approval
        ? 'Approval required'
        : 'No approval required'

  return (
    <section className="content-panel analysis-panel remediation-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">
            Controlled remediation
          </p>
          <h2>Remediation plan</h2>
        </div>

        {plan && (
          <div className="remediation-badges">
            <span
              className="risk-badge"
              data-risk={plan.risk_level}
            >
              {formatLabel(plan.risk_level)} risk
            </span>

            <span className="analysis-meta-badge">
              {approvalLabel}
            </span>
          </div>
        )}
      </div>

      {plan ? (
        <div className="remediation-body">
          <div className="remediation-summary">
            <span className="field-label">
              Proposed action
            </span>
            <h3>{formatLabel(plan.action)}</h3>
            <p>{plan.summary}</p>
          </div>

          <dl className="remediation-facts">
            <div>
              <dt>Namespace</dt>
              <dd>{plan.parameters.namespace}</dd>
            </div>

            <div>
              <dt>Resource kind</dt>
              <dd>
                {plan.parameters.resource_kind}
              </dd>
            </div>

            <div>
              <dt>Resource name</dt>
              <dd>
                {plan.parameters.resource_name}
              </dd>
            </div>

            <div>
              <dt>Approval</dt>
              <dd>{approvalLabel}</dd>
            </div>
          </dl>

          <div className="remediation-section">
            <h3>Controlled parameter changes</h3>
            <p className="section-description">
              Only the structured parameters below can
              reach the execution policy. Arbitrary shell
              commands and free-form patches are not
              accepted.
            </p>

            <ActionParameters plan={plan} />
          </div>

          <div className="outcome-grid">
            <article>
              <span>Expected result</span>
              <p>{plan.expected_result}</p>
            </article>

            <article>
              <span>Rollback plan</span>
              <p>{plan.rollback_plan}</p>
            </article>
          </div>

          <div className="remediation-reference-grid">
            <div className="remediation-section">
              <h3>Evidence references</h3>
              <ReferenceLinks
                identifiers={plan.evidence_ids}
                availableIdentifiers={
                  availableEvidenceIds
                }
                kind="evidence"
              />
            </div>

            <div className="remediation-section">
              <h3>Runbook references</h3>
              <ReferenceLinks
                identifiers={plan.runbook_ids}
                availableIdentifiers={
                  availableRunbookIds
                }
                kind="runbook"
              />
            </div>
          </div>

          <div className="approval-boundary-note">
            <strong>Execution boundary</strong>
            <p>
              This page only displays the validated plan.
              No action is executed without the required
              human approval and execution-policy check.
            </p>
          </div>
        </div>
      ) : (
        <div
          className={`empty-state analysis-empty remediation-empty${
            incident.phase === 'remediation_skipped'
              ? ' is-skipped'
              : ''
          }`}
        >
          <div
            className="empty-state-icon"
            aria-hidden="true"
          >
            {incident.phase === 'remediation_skipped'
              ? '✓'
              : '—'}
          </div>

          <h3>
            {incident.phase === 'remediation_skipped'
              ? 'No remediation required'
              : 'Remediation plan not available'}
          </h3>

          <p>
            {incident.phase === 'remediation_skipped'
              ? 'The diagnosis did not identify a fault that requires a controlled Kubernetes change.'
              : 'The workflow has not produced a validated remediation plan.'}
          </p>
        </div>
      )}
    </section>
  )
}