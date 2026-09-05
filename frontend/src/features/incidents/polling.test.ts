import {
  afterEach,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'

import type {
  IncidentStatusResponse,
} from '../../api'
import {
  shouldContinueIncidentPolling,
  startIncidentPolling,
} from './polling'

function buildIncident(
  phase: string,
  waitingForApproval = false,
): IncidentStatusResponse {
  return {
    incident_id: 'incident-1',
    thread_id: 'incident-1',
    phase,
    waiting_for_approval: waitingForApproval,

    request: {
      namespace: 'agent-demo',
      service_name: 'order-service',
      description: 'test incident',
    },

    valid: true,
    error_count: 0,

    collection_plan: [],
    evidence: [],

    retrieval_query: null,
    retrieved_runbooks: [],

    diagnosis: null,
    llm_model: null,
    llm_usage: {},
    diagnosis_retry_count: 0,

    remediation_plan: null,
    risk_level: null,
    remediation_llm_model: null,
    remediation_llm_usage: {},

    requires_approval: false,
    approved: null,
    approval_status: null,
    approval_request: null,
    approval_record: null,

    action_result: null,
    verification_result: null,

    errors: [],
    trace: [],
  }
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('incident polling', () => {
  it('continues only for known active phases', () => {
    expect(
      shouldContinueIncidentPolling(
        buildIncident('runbooks_retrieved'),
      ),
    ).toBe(true)

    expect(
      shouldContinueIncidentPolling(
        buildIncident('remediation_skipped'),
      ),
    ).toBe(false)

    expect(
      shouldContinueIncidentPolling(
        buildIncident(
          'awaiting_approval',
          true,
        ),
      ),
    ).toBe(false)

    expect(
      shouldContinueIncidentPolling(
        buildIncident('future_unknown_phase'),
      ),
    ).toBe(false)
  })

  it('performs one reconciliation GET for a terminal incident', async () => {
    const terminalIncident =
      buildIncident('remediation_skipped')

    const fetchIncident = vi
      .fn()
      .mockResolvedValue(terminalIncident)

    const onUpdate = vi.fn()
    const onError = vi.fn()

    startIncidentPolling({
      incidentId: terminalIncident.incident_id,
      fetchIncident,
      onUpdate,
      onError,
      intervalMs: 3_000,
    })

    await vi.advanceTimersByTimeAsync(3_000)

    expect(fetchIncident).toHaveBeenCalledOnce()
    expect(fetchIncident).toHaveBeenCalledWith(
      'incident-1',
    )
    expect(onUpdate).toHaveBeenCalledWith(
      terminalIncident,
    )
    expect(onError).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(9_000)

    expect(fetchIncident).toHaveBeenCalledOnce()
  })

  it('continues polling while active and stops at a terminal phase', async () => {
    const activeIncident =
      buildIncident('runbooks_retrieved')
    const terminalIncident =
      buildIncident('remediation_skipped')

    const fetchIncident = vi
      .fn()
      .mockResolvedValueOnce(activeIncident)
      .mockResolvedValueOnce(terminalIncident)

    const onUpdate = vi.fn()

    startIncidentPolling({
      incidentId: 'incident-1',
      fetchIncident,
      onUpdate,
      onError: vi.fn(),
      intervalMs: 3_000,
    })

    await vi.advanceTimersByTimeAsync(3_000)

    expect(fetchIncident).toHaveBeenCalledTimes(1)
    expect(onUpdate).toHaveBeenLastCalledWith(
      activeIncident,
    )

    await vi.advanceTimersByTimeAsync(3_000)

    expect(fetchIncident).toHaveBeenCalledTimes(2)
    expect(onUpdate).toHaveBeenLastCalledWith(
      terminalIncident,
    )

    await vi.advanceTimersByTimeAsync(9_000)

    expect(fetchIncident).toHaveBeenCalledTimes(2)
  })

  it('reports a GET failure and stops polling', async () => {
    const error = new Error('GET failed')
    const fetchIncident = vi
      .fn()
      .mockRejectedValue(error)
    const onError = vi.fn()

    startIncidentPolling({
      incidentId: 'incident-1',
      fetchIncident,
      onUpdate: vi.fn(),
      onError,
      intervalMs: 3_000,
    })

    await vi.advanceTimersByTimeAsync(3_000)

    expect(onError).toHaveBeenCalledOnce()
    expect(onError).toHaveBeenCalledWith(error)

    await vi.advanceTimersByTimeAsync(9_000)

    expect(fetchIncident).toHaveBeenCalledOnce()
  })

  it('cancels a scheduled poll', async () => {
    const fetchIncident = vi.fn()

    const stopPolling = startIncidentPolling({
      incidentId: 'incident-1',
      fetchIncident,
      onUpdate: vi.fn(),
      onError: vi.fn(),
      intervalMs: 3_000,
    })

    stopPolling()

    await vi.advanceTimersByTimeAsync(3_000)

    expect(fetchIncident).not.toHaveBeenCalled()
  })
})