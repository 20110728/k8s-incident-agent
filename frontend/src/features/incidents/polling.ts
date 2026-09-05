import type {
  IncidentStatusResponse,
} from '../../api'

export const DEFAULT_INCIDENT_POLL_INTERVAL_MS =
  3_000

const POLLING_PHASES = new Set([
  'created',
  'validated',
  'collection_planned',
  'evidence_collected',
  'evidence_collected_with_errors',
  'runbooks_retrieved',
  'diagnosis_completed',
  'remediation_planned',
  'approval_approved',
  'remediation_executed',
])

export function shouldContinueIncidentPolling(
  incident: IncidentStatusResponse,
): boolean {
  if (incident.waiting_for_approval) {
    return false
  }

  return POLLING_PHASES.has(incident.phase)
}

export interface IncidentPollingOptions {
  incidentId: string
  fetchIncident: (
    incidentId: string,
  ) => Promise<IncidentStatusResponse>
  onUpdate: (
    incident: IncidentStatusResponse,
  ) => void
  onError: (error: unknown) => void
  intervalMs?: number
}

export function startIncidentPolling({
  incidentId,
  fetchIncident,
  onUpdate,
  onError,
  intervalMs = DEFAULT_INCIDENT_POLL_INTERVAL_MS,
}: IncidentPollingOptions): () => void {
  if (
    !Number.isFinite(intervalMs)
    || intervalMs <= 0
  ) {
    throw new RangeError(
      'Polling interval must be greater than zero.',
    )
  }

  let stopped = false
  let timeoutId:
    | ReturnType<typeof globalThis.setTimeout>
    | null = null

  function scheduleNextPoll() {
    if (stopped) {
      return
    }

    timeoutId = globalThis.setTimeout(() => {
      void poll()
    }, intervalMs)
  }

  async function poll() {
    try {
      const nextIncident =
        await fetchIncident(incidentId)

      if (stopped) {
        return
      }

      onUpdate(nextIncident)

      if (
        shouldContinueIncidentPolling(
          nextIncident,
        )
      ) {
        scheduleNextPoll()
      }
    } catch (error) {
      if (!stopped) {
        onError(error)
      }
    }
  }

  scheduleNextPoll()

  return () => {
    stopped = true

    if (timeoutId !== null) {
      globalThis.clearTimeout(timeoutId)
    }
  }
}