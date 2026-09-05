export type JsonPrimitive =
  | string
  | number
  | boolean
  | null

export type JsonValue =
  | JsonPrimitive
  | JsonObject
  | JsonValue[]

export type JsonObject = {
  [key: string]: JsonValue
}

export type FaultCategory =
  | 'crash_loop_backoff'
  | 'image_pull_backoff'
  | 'oom_killed'
  | 'readiness_probe_error'
  | 'service_selector_mismatch'
  | 'no_fault_detected'
  | 'unknown'

export type RiskLevel =
  | 'low'
  | 'medium'
  | 'high'

export type RemediationAction =
  | 'manual_investigation'
  | 'patch_readiness_probe'
  | 'patch_service_selector'

export type ApprovalStatus =
  | 'not_required'
  | 'pending'
  | 'approved'
  | 'rejected'
  | 'failed'

export type ExecutionStatus =
  | 'succeeded'
  | 'already_applied'
  | 'conflict'
  | 'failed'

export type VerificationStatus =
  | 'succeeded'
  | 'failed'
  | 'timeout'
  | 'skipped'

export type TraceStatus =
  | 'started'
  | 'completed'
  | 'failed'

export type RemediationResourceKind =
  | 'Service'
  | 'Deployment'
  | 'Pod'

export type MutableResourceKind =
  | 'Service'
  | 'Deployment'

export interface IncidentRequest {
  namespace: string
  service_name: string
  description: string
}

export interface SubmitApprovalRequest {
  approval_id: string
  approved: boolean
  approver: string
  comment?: string
}

export interface TraceEvent {
  step: string
  status: TraceStatus
  message: string
  timestamp: string
}

export interface EvidenceItem {
  evidence_id: string
  source: string
  resource_type: string
  resource_name: string
  collected_at: string
  data: JsonObject
  error: string | null
}

export interface RetrievedRunbook {
  document_id: string | null
  runbook_id: string | null
  category: string | null
  title: string | null
  section: string | null
  source: string | null
  chunk_index: number | null
  content: string
  score: number
}

export interface Diagnosis {
  fault_category: FaultCategory
  root_cause: string
  evidence_ids: string[]
  runbook_ids: string[]
  confidence: number
  reasoning_summary: string
}

export interface LabelPair {
  key: string
  value: string
}

export interface RemediationParameters {
  namespace: string
  resource_kind: RemediationResourceKind
  resource_name: string

  container_name: string | null
  current_probe_path: string | null
  proposed_probe_path: string | null
  current_probe_port: string | number | null
  proposed_probe_port: string | number | null

  current_selector: LabelPair[]
  proposed_selector: LabelPair[]
  investigation_steps: string[]
}

export interface RemediationPlan {
  action: RemediationAction
  parameters: RemediationParameters
  risk_level: RiskLevel
  summary: string
  expected_result: string
  rollback_plan: string
  evidence_ids: string[]
  runbook_ids: string[]
  requires_approval: boolean
}

export interface ApprovalRequest {
  approval_id: string
  incident_id: string
  plan: RemediationPlan
}

export interface ApprovalRecord {
  approval_id: string
  incident_id: string
  action: RemediationAction
  approved: boolean
  approver: string
  comment: string
  decided_at: string
}

export interface ResourceSnapshot {
  namespace: string
  resource_kind: MutableResourceKind
  resource_name: string
  resource_version: string
  configuration: JsonObject
}

export interface ActionExecutionResult {
  execution_id: string
  approval_id: string
  action: RemediationAction
  status: ExecutionStatus

  namespace: string
  resource_kind: MutableResourceKind
  resource_name: string

  started_at: string
  finished_at: string

  before_snapshot: ResourceSnapshot | null
  after_snapshot: ResourceSnapshot | null

  applied_patch: JsonObject
  rollback_patch: JsonObject

  message: string
  error_code: string | null
  error_message: string | null
}

export interface VerificationCheck {
  name: string
  passed: boolean
  observed: JsonValue
  expected: JsonValue
  message: string
}

export interface RecoveryVerificationResult {
  execution_id: string
  action: RemediationAction
  status: VerificationStatus

  started_at: string
  finished_at: string
  attempts: number

  checks: VerificationCheck[]

  desired_replicas: number | null
  available_replicas: number | null
  ready_pods: number | null
  ready_endpoints: number | null

  message: string
  error_code: string | null
  error_message: string | null
}

export interface IncidentError {
  stage: string
  code: string
  message: string
  [key: string]: JsonValue
}

export interface IncidentStatusResponse {
  incident_id: string
  thread_id: string
  phase: string
  waiting_for_approval: boolean

  request: IncidentRequest
  valid: boolean | null
  error_count: number

  collection_plan: string[]
  evidence: EvidenceItem[]

  retrieval_query: string | null
  retrieved_runbooks: RetrievedRunbook[]

  diagnosis: Diagnosis | null
  llm_model: string | null
  llm_usage: Record<string, number>
  diagnosis_retry_count: number

  remediation_plan: RemediationPlan | null
  risk_level: RiskLevel | null
  remediation_llm_model: string | null
  remediation_llm_usage: Record<string, number>

  requires_approval: boolean
  approved: boolean | null
  approval_status: ApprovalStatus | null
  approval_request: ApprovalRequest | null
  approval_record: ApprovalRecord | null

  action_result: ActionExecutionResult | null
  verification_result: RecoveryVerificationResult | null

  errors: IncidentError[]
  trace: TraceEvent[]
}

export interface ErrorDetail {
  code: string
  message: string
  details: JsonValue | null
}

export interface ErrorResponse {
  error: ErrorDetail
}

export interface HealthResponse {
  status: 'ok'
  service: string
  version: string
}

export interface ReadinessResponse {
  status: 'ready'
  checks: Record<string, boolean>
}