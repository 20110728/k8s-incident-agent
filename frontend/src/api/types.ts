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
  | 'application_error'
  | 'dependency_error'
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

export interface DiagnosticAssessment {
  schema_version: 'v2'
  problem_domain: 'deployment_configuration' | 'application_runtime' | 'dependency' | 'insufficient_evidence' | 'none'
  symptoms: { summary: string; evidence_ids: string[] }[]
  root_cause_hypotheses: { summary: string; evidence_ids: string[]; status: 'suspected' | 'supported' }[]
  missing_evidence: string[]
  next_investigation: string[]
  resource_status: 'ready' | 'not_ready' | 'unknown'
  business_status: 'passed' | 'failed' | 'unknown'
  unverified_scope: string[]
}

export interface Diagnosis {
  assessment?: DiagnosticAssessment | null
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
  verification_scope?: 'resource_only' | 'resources_and_registered_business'
  resource_verification_status?: VerificationStatus | null
  resource_status?: 'ready' | 'not_ready' | 'unknown'
  business_status?: 'passed' | 'failed' | 'unknown' | 'skipped'
  observation?: ObservationSummary | null
  unverified_scope?: string[]
  post_repair_evidence?: EvidenceItem[]

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
  investigation?: InvestigationView | null
  execution_mode?: 'sync' | 'queued'
  worker_available?: boolean
  run?: RunSummary | null
  llm_debug?: JsonObject
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

export interface Question {
  change_candidates?: { resource_ref: string; kind: string; name: string; container?: string | null }[]
  question_id: string
  version: number
  questions: { slot: string; text: string }[]
  reason: string
  evidence_revision: string
}

export interface RunSummary {
  run_id: string
  status: string
  run_kind: string
  created_at: string
  updated_at: string
  finished_at: string | null
  attempt: number
  last_error_code: string | null
  stop_requested?: boolean
  invalidated_at?: string | null
  question?: Question | null
  adopted_message_ids?: string[]
}

export interface IncidentListItem {
  incident_id: string
  namespace: string
  service_name: string
  phase: string
  created_at: string
  updated_at: string
  run: RunSummary | null
}

export interface CursorPage<T> { items: T[]; next_cursor: string | null }
export interface SequencePage<T> { items: T[]; next_before_sequence: number | null }
export interface Message {
  message_id: string
  sequence: number
  role: string
  source: string
  content: string
  created_at: string
  related_run_id: string | null
  evidence_refs: string[]
  adopted_by_run_ids: string[]
}
export type InteractionIntent = 'auto' | 'explain' | 'compare' | 'supplement' | 'investigate' | 'recheck' | 'observe' | 'stop'
export interface InteractionRequest {
  client_message_id: string
  content: string
  intent: InteractionIntent
  reference_run_id?: string
  compare_run_id?: string
}
export interface AnswerRequest {
  changed_resource_refs?: string[]
  client_message_id: string
  content: string
  question_id: string
  version: number
  answers: Record<string, string>
  skip: boolean
}
export interface InteractionResult {
  run: RunSummary
  output: {
    intent?: string
    answer?: string
    reason?: string
    unknowns?: string[]
    diagnosis_run_id?: string
    historical_only?: boolean
    reference_snapshots?: { run_id: string | null; snapshot_at: string }[]
    citations?: { citation_id: string; run_id: string | null; collected_at: string | null; snapshot_at: string }[]
  } | null
  calls: { purpose?: string; model?: string; elapsed_ms?: number; status?: string; usage?: Record<string, number> }[]
}
export interface ControlReceipt {
  control_id: string
  status: string
  action?: string
  message_id: string
  diagnosis_run_id?: string
  deferred_until_write_checked?: boolean
  processing?: string
}
export type CommandReceipt = InteractionResult | ControlReceipt
export interface ObservationSummary {
  status: string
  consecutive: number
  policy: { version: string; required_consecutive: number }
  samples: { sequence: number; status: string; resource_status?: string; business_status?: string; started_at: string; finished_at: string | null }[]
}

export interface Recheck {
  observation?: ObservationSummary | null
  recheck_id: string
  started_at: string
  finished_at: string
  note: string
  status: string
  resource_status: string
  business_status: string
  target_comparison: { status: string; changes: JsonObject }
  unverified_scope: string[]
  error_code: string | null
  collection_errors: JsonObject[]
}

export interface DelayedRecheck {
  delayed_id: string
  sequence: number
  status: 'pending' | 'running' | 'passed' | 'relapsed' | 'unknown' | 'invalidated' | 'expired'
  due_at: string
  expires_at: string
  finished_at: string | null
  reason: string | null
  initial_result: { status: string; finished_at: string; consecutive: number }
  result: Recheck | null
}
export interface Operation {
  operation_id: string
  run_id: string
  state: string
  error_code: string | null
}
export interface RunBudget {
  generation?: { attempts: number; reported_tokens: number; unreported_attempts: number }
  available: boolean
  run_id: string
  policy?: { active_seconds: number; extra_seconds: number; total_tokens: number; decisions: number; tools: number }
  used?: { active_seconds: number; extra_seconds: number; tokens: number; decisions: number; tools: number }
  exhausted?: string | null
  calls?: unknown[]
  handoff?: { reason: string; known: string; unknown: string; next_step: string } | null
}

export interface InvestigationView {
  version: number
  steps: { step: number; action: string; reason: string | null; missing_fact: string | null; evidence_ids: string[];
    results: { tool: string; coverage: string; error_code: string | null; evidence_ids: string[]; generation: number }[] }[]
  observations: { evidence_id: string; origin: string; resource_type: string; resource_name: string;
    collected_at: string | null; coverage: string; error: string | null; truncated: boolean; current: boolean }[]
  omitted_observations: number
  outcome: string | null
  stop_reason: string | null
  question_count: number
  answer_count: number
}
