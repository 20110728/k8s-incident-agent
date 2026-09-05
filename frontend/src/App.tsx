import {
  useEffect,
  useState,
} from 'react'

import {
  apiClient,
  isApiClientError,
  type IncidentRequest,
  type IncidentStatusResponse,
  type SubmitApprovalRequest,
} from './api'
import './App.css'
import { IncidentCreateForm } from './features/incidents/IncidentCreateForm'

import {
  shouldContinueIncidentPolling,
  startIncidentPolling,
} from './features/incidents/polling'

import {
  IncidentAnalysis,
} from './features/incidents/IncidentAnalysis'

import {
  buildIncidentSearch,
  LAST_INCIDENT_STORAGE_KEY,
  resolveIncidentId,
} from './features/incidents/incidentSession'

import {
  ApprovalDecisionPanel,
} from './features/incidents/ApprovalDecisionPanel'

import {
  IncidentOutcomePanel,
} from './features/incidents/IncidentOutcomePanel'

const workflowSteps = [
  {
    name: 'Incident intake',
    description: 'Create and validate the incident request.',
  },
  {
    name: 'Evidence collection',
    description: 'Collect read-only Kubernetes evidence.',
  },
  {
    name: 'Diagnosis',
    description: 'Retrieve runbooks and identify the root cause.',
  },
  {
    name: 'Remediation plan',
    description: 'Generate a constrained remediation proposal.',
  },
  {
    name: 'Human approval',
    description: 'Approve or reject controlled execution.',
  },
  {
    name: 'Execution & verification',
    description: 'Execute approved actions and verify recovery.',
  },
]


type RequestState =
  | 'idle'
  | 'creating'
  | 'succeeded'
  | 'failed'

type StatusSyncState =
  | 'idle'
  | 'polling'
  | 'failed'

type RestoreState =
  | 'idle'
  | 'loading'
  | 'succeeded'
  | 'failed'

type ApprovalSubmissionState =
  | 'idle'
  | 'submitting'
  | 'succeeded'
  | 'failed'

function formatPhase(phase: string) {
  return phase.replaceAll('_', ' ')
}

function activeWorkflowStep(
  phase: string | undefined,
) {
  if (!phase) {
    return 0
  }

  if (
    phase.includes('completed') ||
    phase.includes('verification') ||
    phase.includes('execut') ||
    phase.includes('skipped')
  ) {
    return 5
  }

  if (
    phase.includes('approval') ||
    phase.includes('rejected')
  ) {
    return 4
  }

  if (
    phase.includes('remediation') ||
    phase.includes('planning')
  ) {
    return 3
  }

  if (
    phase.includes('diagnos') ||
    phase.includes('runbook') ||
    phase.includes('retriev')
  ) {
    return 2
  }

  if (
    phase.includes('evidence') ||
    phase.includes('collect')
  ) {
    return 1
  }

  return 0
}

function normalizeError(error: unknown) {
  if (isApiClientError(error)) {
    return `[${error.code}] ${error.message}`
  }

  if (error instanceof Error) {
    return error.message
  }

  return '创建事故时发生未知错误。'
}

function readInitialIncidentId(): string | null {
  let storedIncidentId: string | null = null

  try {
    storedIncidentId =
      globalThis.localStorage.getItem(
        LAST_INCIDENT_STORAGE_KEY,
      )
  } catch {
    storedIncidentId = null
  }

  return resolveIncidentId(
    globalThis.location.search,
    storedIncidentId,
  )
}

function persistIncidentId(
  incidentId: string,
) {
  try {
    globalThis.localStorage.setItem(
      LAST_INCIDENT_STORAGE_KEY,
      incidentId,
    )
  } catch {
    // The URL remains the recovery source when
    // browser storage is unavailable.
  }

  const nextSearch = buildIncidentSearch(
    globalThis.location.search,
    incidentId,
  )

  globalThis.history.replaceState(
    globalThis.history.state,
    '',
    `${globalThis.location.pathname}${nextSearch}${globalThis.location.hash}`,
  )
}

function App() {
  const [requestState, setRequestState] =
    useState<RequestState>('idle')
  const [requestError, setRequestError] =
    useState<string | null>(null)
  const [statusSyncState, setStatusSyncState] =
    useState<StatusSyncState>('idle')
  const [statusSyncError, setStatusSyncError] =
    useState<string | null>(null)
  const [incident, setIncident] =
    useState<IncidentStatusResponse | null>(null)
  const [initialIncidentId] =
    useState(readInitialIncidentId)

  const [restoreState, setRestoreState] =
    useState<RestoreState>(
      initialIncidentId ? 'loading' : 'idle',
    )

  const [restoreError, setRestoreError] =
    useState<string | null>(null)

  const [
    approvalSubmissionState,
    setApprovalSubmissionState,
  ] = useState<ApprovalSubmissionState>('idle')

  const [
    approvalSubmissionError,
    setApprovalSubmissionError,
  ] = useState<string | null>(null)

  const [
    statusSyncRevision,
    setStatusSyncRevision,
  ] = useState(0)

  async function createIncident(
    request: IncidentRequest,
  ) {
    setRequestState('creating')
    setRequestError(null)
    setStatusSyncState('idle')
    setStatusSyncError(null)
    setRestoreState('idle')
    setRestoreError(null)
    setApprovalSubmissionState('idle')
    setApprovalSubmissionError(null)

    try {
      const result =
        await apiClient.createIncident(request)

      setStatusSyncState('polling')
      setStatusSyncError(null)
      persistIncidentId(result.incident_id)
      setIncident(result)
      setRequestState('succeeded')
    } catch (error) {
      setRequestError(normalizeError(error))
      setRequestState('failed')
    }
  }
  async function submitApprovalDecision(
    request: SubmitApprovalRequest,
  ) {
    if (!incident) {
      setApprovalSubmissionState('failed')
      setApprovalSubmissionError(
        'No incident is selected.',
      )
      return
    }

    setApprovalSubmissionState('submitting')
    setApprovalSubmissionError(null)

    try {
      const result =
        await apiClient.submitApproval(
          incident.incident_id,
          request,
        )

      persistIncidentId(result.incident_id)
      setIncident(result)
      setApprovalSubmissionState('succeeded')
      setStatusSyncState('polling')
      setStatusSyncError(null)
      setStatusSyncRevision(
        (revision) => revision + 1,
      )
    } catch (error) {
      setApprovalSubmissionState('failed')
      setApprovalSubmissionError(
        normalizeError(error),
      )
    }
  }


  const incidentId = incident?.incident_id
  useEffect(() => {
    if (!initialIncidentId) {
      return
    }

    let cancelled = false

    void apiClient
      .getIncident(initialIncidentId)
      .then((restoredIncident) => {
        if (cancelled) {
          return
        }

        persistIncidentId(
          restoredIncident.incident_id,
        )
        setIncident(restoredIncident)
        setRequestState('succeeded')
        setRestoreState('succeeded')
        setRestoreError(null)
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return
        }

        setRestoreState('failed')
        setRestoreError(normalizeError(error))
      })

    return () => {
      cancelled = true
    }
  }, [initialIncidentId])

  useEffect(() => {
    if (!incidentId) {
      return
    }


    return startIncidentPolling({
      incidentId,
      fetchIncident: (currentIncidentId) =>
        apiClient.getIncident(currentIncidentId),

      onUpdate: (nextIncident) => {
        setIncident(nextIncident)

        setStatusSyncState(
          shouldContinueIncidentPolling(nextIncident)
            ? 'polling'
            : 'idle',
        )
      },

      onError: (error) => {
        setStatusSyncState('failed')
        setStatusSyncError(normalizeError(error))
      },
    })
  }, [incidentId, statusSyncRevision])

  const activeStep = activeWorkflowStep(
    incident?.phase,
  )

  const requestStatus =
    restoreState === 'loading'
      ? 'Loading incident'
      : requestState === 'creating'
        ? 'Requesting'
        : approvalSubmissionState === 'submitting'
          ? 'Submitting decision'
          : requestState === 'failed'
            || restoreState === 'failed'
            || statusSyncState === 'failed'
            || approvalSubmissionState === 'failed'
            ? 'Error'
            : statusSyncState === 'polling'
              ? 'Syncing'
              : incident
                ? 'Connected'
                : 'Ready'

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            K8s
          </div>
          <div>
            <strong>Kubernetes Incident Agent</strong>
            <small>Evidence-driven incident response</small>
          </div>
        </div>

        <div className="environment-badge">
          Development
        </div>
      </header>

      <div className="workspace">
        <aside className="sidebar">
          <p className="sidebar-label">Workflow</p>

          <ol className="workflow-list">
            {workflowSteps.map((step, index) => (
              <li
                key={step.name}
                className={`workflow-step${
                  index === activeStep ? ' is-active' : ''
                }`}
                aria-current={
                  index === activeStep ? 'step' : undefined
                }
              >
                <span className="step-number">
                  {index + 1}
                </span>
                <div>
                  <strong>{step.name}</strong>
                  <p>{step.description}</p>
                </div>
              </li>
            ))}
          </ol>
        </aside>

        <main className="main-content">
          <div className="page-heading">
            <div>
              <p className="eyebrow">
                Incident workspace
              </p>
              <h1>Investigate Kubernetes incidents</h1>
              <p className="page-description">
                Create an incident to collect evidence,
                retrieve runbooks and start a structured
                diagnosis.
              </p>
            </div>

            <div
              className="request-indicator"
              data-state={requestState}
              aria-live="polite"
            >
              <span />
              {requestStatus}
            </div>
          </div>

          <section className="metric-grid">
            <article className="metric-card">
              <span>Current incident</span>
              <strong>
                {incident ? 'Created' : 'None'}
              </strong>
            </article>

            <article className="metric-card">
              <span>Current phase</span>
              <strong>
                {incident
                  ? formatPhase(incident.phase)
                  : 'Not started'}
              </strong>
            </article>

            <article className="metric-card">
              <span>Human approval</span>
              <strong>
                {incident?.waiting_for_approval
                  ? 'Required'
                  : 'Not waiting'}
              </strong>
            </article>
          </section>

          {requestError && (
            <div className="api-error" role="alert">
              <strong>Incident creation failed</strong>
              <span>{requestError}</span>
            </div>
          )}

          {statusSyncError && (
            <div className="api-error" role="alert">
              <strong>Status refresh failed</strong>
              <span>{statusSyncError}</span>
            </div>
          )}

          {restoreError && (
            <div className="api-error" role="alert">
              <strong>Incident restore failed</strong>
              <span>{restoreError}</span>
            </div>
          )}

          <div className="incident-layout">
            <IncidentCreateForm
              submitting={requestState === 'creating'}
              onSubmit={createIncident}
            />

            <section className="content-panel incident-details">
              <div className="panel-header">
                <div>
                  <p className="eyebrow">
                    Current response
                  </p>
                  <h2>Incident summary</h2>
                </div>
              </div>

              {incident ? (
                <>
                  <dl className="incident-summary">
                    <div>
                      <dt>Incident ID</dt>
                      <dd>
                        <code className="incident-id">
                          {incident.incident_id}
                        </code>
                      </dd>
                    </div>

                    <div>
                      <dt>Namespace</dt>
                      <dd>
                        {incident.request['namespace']}
                      </dd>
                    </div>

                    <div>
                      <dt>Service</dt>
                      <dd>
                        {
                          incident.request[
                            'service_name'
                          ]
                        }
                      </dd>
                    </div>

                    <div>
                      <dt>Phase</dt>
                      <dd>
                        {formatPhase(incident.phase)}
                      </dd>
                    </div>

                    <div>
                      <dt>Waiting for approval</dt>
                      <dd>
                        {incident.waiting_for_approval
                          ? 'Yes'
                          : 'No'}
                      </dd>
                    </div>

                    <div>
                      <dt>Fault category</dt>
                      <dd>
                        {incident.diagnosis
                          ?.fault_category ?? 'Pending'}
                      </dd>
                    </div>
                  </dl>

                  <p className="incident-note">
                    创建响应已保存。状态轮询、诊断证据和
                    Runbook 详情将在下一阶段接入。
                  </p>
                </>
              ) : (
                <div className="empty-state">
                  <div
                    className="empty-state-icon"
                    aria-hidden="true"
                  >
                    01
                  </div>
                  <h3>No incident selected</h3>
                  <p>
                    Submit the form to create an incident
                    and display the initial graph state.
                  </p>
                </div>
              )}
            </section>
          </div>
          {incident && (
            <IncidentAnalysis incident={incident} />
          )}

          {incident && (
            <ApprovalDecisionPanel
              incident={incident}
              submitting={
                approvalSubmissionState ===
                'submitting'
              }
              error={approvalSubmissionError}
              onSubmit={submitApprovalDecision}
            />
          )}

          {incident && (
            <IncidentOutcomePanel incident={incident} />
          )}

        </main>
      </div>
    </div>
  )
}

export default App