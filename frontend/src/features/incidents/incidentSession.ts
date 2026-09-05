export const LAST_INCIDENT_STORAGE_KEY =
  'k8s-incident-agent:last-incident-id'

const INCIDENT_ID_PATTERN =
  /^[a-zA-Z0-9-]+$/

export function normalizeIncidentId(
  value: string | null,
): string | null {
  if (value === null) {
    return null
  }

  const normalized = value.trim()

  if (
    !normalized
    || normalized.length > 128
    || !INCIDENT_ID_PATTERN.test(normalized)
  ) {
    return null
  }

  return normalized
}

export function resolveIncidentId(
  search: string,
  storedIncidentId: string | null,
): string | null {
  const parameters =
    new URLSearchParams(search)

  const queryIncidentId =
    parameters.get('incident_id')

  if (queryIncidentId !== null) {
    return normalizeIncidentId(
      queryIncidentId,
    )
  }

  return normalizeIncidentId(
    storedIncidentId,
  )
}

export function buildIncidentSearch(
  currentSearch: string,
  incidentId: string,
): string {
  const normalized =
    normalizeIncidentId(incidentId)

  if (!normalized) {
    throw new Error(
      'Cannot build a URL with an invalid incident ID.',
    )
  }

  const parameters =
    new URLSearchParams(currentSearch)

  parameters.set(
    'incident_id',
    normalized,
  )

  return `?${parameters.toString()}`
}