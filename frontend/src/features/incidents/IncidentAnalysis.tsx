import type {
  Diagnosis,
  EvidenceItem,
  IncidentStatusResponse,
  RetrievedRunbook,
} from '../../api'

import {
  evidenceElementId,
  formatConfidence,
  formatLabel,
  formatTimestamp,
  formatVectorDistance,
  isReferenced,
  runbookElementId,
} from './presentation'

import {
  RemediationPlanPanel,
} from './RemediationPlanPanel'

interface DiagnosisPanelProps {
  diagnosis: Diagnosis | null
  llmModel: string | null
  availableEvidenceIds: ReadonlySet<string>
  availableRunbookIds: ReadonlySet<string>
}

function DiagnosisPanel({
  diagnosis,
  llmModel,
  availableEvidenceIds,
  availableRunbookIds,
}: DiagnosisPanelProps) {
  return (
    <section className="content-panel analysis-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">
            Structured diagnosis
          </p>
          <h2>Diagnosis</h2>
        </div>

        {llmModel && (
          <span className="analysis-meta-badge">
            {llmModel}
          </span>
        )}
      </div>

      {diagnosis ? (
        <div className="diagnosis-body">
          <div className="diagnosis-overview">
            <div>
              <span className="field-label">
                Fault category
              </span>
              <strong className="fault-category">
                {formatLabel(
                  diagnosis.fault_category,
                )}
              </strong>
            </div>

            <div>
              <span className="field-label">
                Confidence
              </span>
              <strong className="confidence-value">
                {formatConfidence(
                  diagnosis.confidence,
                )}
              </strong>
            </div>
          </div>

          <div className="diagnosis-section">
            <h3>Root cause</h3>
            <p>{diagnosis.root_cause}</p>
          </div>

          <div className="diagnosis-section">
            <h3>Reasoning summary</h3>
            <p>{diagnosis.reasoning_summary}</p>
          </div>

          <div className="diagnosis-section">
            <h3>Evidence citations</h3>

            <div className="citation-list">
              {diagnosis.evidence_ids.map(
                (evidenceId) =>
                  availableEvidenceIds.has(
                    evidenceId,
                  ) ? (
                    <a
                      key={evidenceId}
                      className="citation-chip"
                      href={`#${evidenceElementId(
                        evidenceId,
                      )}`}
                    >
                      {evidenceId}
                    </a>
                  ) : (
                    <span
                      key={evidenceId}
                      className="citation-chip is-missing"
                      title="Referenced evidence is missing from the response."
                    >
                      {evidenceId}
                    </span>
                  ),
              )}
            </div>
          </div>

          <div className="diagnosis-section">
            <h3>Runbook citations</h3>

            {diagnosis.runbook_ids.length > 0 ? (
              <div className="citation-list">
                {diagnosis.runbook_ids.map(
                  (runbookId) =>
                    availableRunbookIds.has(
                      runbookId,
                    ) ? (
                      <a
                        key={runbookId}
                        className="citation-chip"
                        href={`#${runbookElementId(
                          runbookId,
                        )}`}
                      >
                        {runbookId}
                      </a>
                    ) : (
                      <span
                        key={runbookId}
                        className="citation-chip is-missing"
                        title="Referenced runbook is missing from the retrieved results."
                      >
                        {runbookId}
                      </span>
                    ),
                )}
              </div>
            ) : (
              <p className="citation-empty">
                No Runbook was cited in the final
                diagnosis.
              </p>
            )}
          </div>
        </div>
      ) : (
        <div className="empty-state analysis-empty">
          <h3>Diagnosis not available</h3>
          <p>
            The workflow has not produced a structured
            diagnosis.
          </p>
        </div>
      )}
    </section>
  )
}

interface EvidencePanelProps {
  evidence: EvidenceItem[]
  referencedIds: readonly string[]
}

function EvidencePanel({
  evidence,
  referencedIds,
}: EvidencePanelProps) {
  const referencedCount = evidence.filter(
    (item) =>
      isReferenced(
        referencedIds,
        item.evidence_id,
      ),
  ).length

  return (
    <section className="content-panel analysis-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">
            Kubernetes evidence
          </p>
          <h2>Evidence</h2>
        </div>

        <span className="analysis-meta-badge">
          {referencedCount}/{evidence.length} cited
        </span>
      </div>

      {evidence.length > 0 ? (
        <div className="evidence-grid">
          {evidence.map((item) => {
            const referenced = isReferenced(
              referencedIds,
              item.evidence_id,
            )

            return (
              <article
                id={evidenceElementId(
                  item.evidence_id,
                )}
                key={item.evidence_id}
                className={`evidence-card${
                  referenced
                    ? ' is-referenced'
                    : ''
                }${
                  item.error ? ' has-error' : ''
                }`}
              >
                <div className="evidence-card-header">
                  <code>{item.evidence_id}</code>

                  <span className="resource-badge">
                    {item.resource_type}
                  </span>
                </div>

                <h3>{item.resource_name}</h3>

                <dl className="evidence-metadata">
                  <div>
                    <dt>Source</dt>
                    <dd>{item.source}</dd>
                  </div>

                  <div>
                    <dt>Collected</dt>
                    <dd>
                      {formatTimestamp(
                        item.collected_at,
                      )}
                    </dd>
                  </div>
                </dl>

                <span
                  className={`reference-state${
                    referenced
                      ? ' is-referenced'
                      : ''
                  }`}
                >
                  {referenced
                    ? 'Cited by diagnosis'
                    : 'Collected context'}
                </span>

                {item.error && (
                  <p
                    className="evidence-error"
                    role="alert"
                  >
                    {item.error}
                  </p>
                )}

                <details className="structured-data">
                  <summary>
                    Inspect structured data
                  </summary>
                  <pre>
                    {JSON.stringify(
                      item.data,
                      null,
                      2,
                    )}
                  </pre>
                </details>
              </article>
            )
          })}
        </div>
      ) : (
        <div className="empty-state analysis-empty">
          <h3>No evidence available</h3>
          <p>
            The incident response does not contain
            collected Kubernetes evidence.
          </p>
        </div>
      )}
    </section>
  )
}

interface RunbookPanelProps {
  runbooks: RetrievedRunbook[]
  referencedIds: readonly string[]
  retrievalQuery: string | null
}

function RunbookPanel({
  runbooks,
  referencedIds,
  retrievalQuery,
}: RunbookPanelProps) {
  const assignedElementIds = new Set<string>()

  return (
    <section className="content-panel analysis-panel">
      <div className="panel-header">
        <div>
          <p className="eyebrow">
            Retrieved operational context
          </p>
          <h2>Runbooks</h2>
        </div>

        <span className="analysis-meta-badge">
          {runbooks.length} chunks
        </span>
      </div>

      {retrievalQuery && (
        <div className="retrieval-query">
          <span>Retrieval query</span>
          <p>{retrievalQuery}</p>
        </div>
      )}

      {runbooks.length > 0 ? (
        <div className="runbook-grid">
          {runbooks.map((runbook, index) => {
            const referenced = isReferenced(
              referencedIds,
              runbook.runbook_id,
            )

            let elementId: string | undefined

            if (
              runbook.runbook_id
              && !assignedElementIds.has(
                runbook.runbook_id,
              )
            ) {
              assignedElementIds.add(
                runbook.runbook_id,
              )
              elementId = runbookElementId(
                runbook.runbook_id,
              )
            }

            return (
              <article
                id={elementId}
                key={
                  runbook.document_id
                  ?? `${runbook.runbook_id}-${index}`
                }
                className={`runbook-card${
                  referenced
                    ? ' is-referenced'
                    : ''
                }`}
              >
                <div className="runbook-card-header">
                  <div>
                    <span className="resource-badge">
                      {runbook.category
                        ?? 'uncategorized'}
                    </span>
                    <h3>
                      {runbook.title
                        ?? runbook.runbook_id
                        ?? 'Untitled Runbook'}
                    </h3>
                  </div>

                  <span className="distance-badge">
                    Distance{' '}
                    {formatVectorDistance(
                      runbook.score,
                    )}
                  </span>
                </div>

                <dl className="runbook-metadata">
                  <div>
                    <dt>Runbook ID</dt>
                    <dd>
                      {runbook.runbook_id ?? '—'}
                    </dd>
                  </div>

                  <div>
                    <dt>Section</dt>
                    <dd>{runbook.section ?? '—'}</dd>
                  </div>

                  <div>
                    <dt>Source</dt>
                    <dd>{runbook.source ?? '—'}</dd>
                  </div>

                  <div>
                    <dt>Chunk</dt>
                    <dd>
                      {runbook.chunk_index ?? '—'}
                    </dd>
                  </div>
                </dl>

                <span
                  className={`reference-state${
                    referenced
                      ? ' is-referenced'
                      : ''
                  }`}
                >
                  {referenced
                    ? 'Cited by diagnosis'
                    : 'Retrieved, not cited'}
                </span>

                <pre className="runbook-content">
                  {runbook.content}
                </pre>
              </article>
            )
          })}
        </div>
      ) : (
        <div className="empty-state analysis-empty">
          <h3>No Runbooks retrieved</h3>
          <p>
            The workflow did not return Runbook
            retrieval results.
          </p>
        </div>
      )}
    </section>
  )
}

interface IncidentAnalysisProps {
  incident: IncidentStatusResponse
}

export function IncidentAnalysis({
  incident,
}: IncidentAnalysisProps) {
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

  const diagnosisEvidenceIds =
    incident.diagnosis?.evidence_ids ?? []

  const diagnosisRunbookIds =
    incident.diagnosis?.runbook_ids ?? []

  return (
    <div className="analysis-stack">
      <DiagnosisPanel
        diagnosis={incident.diagnosis}
        llmModel={incident.llm_model}
        availableEvidenceIds={
          availableEvidenceIds
        }
        availableRunbookIds={
          availableRunbookIds
        }
      />

      <EvidencePanel
        evidence={incident.evidence}
        referencedIds={diagnosisEvidenceIds}
      />

      <RunbookPanel
        runbooks={incident.retrieved_runbooks}
        referencedIds={diagnosisRunbookIds}
        retrievalQuery={incident.retrieval_query}
      />
      <RemediationPlanPanel incident={incident} />
    </div>
  )
}