export function formatLabel(
  value: string,
): string {
  const normalized = value
    .trim()
    .replace(/[_-]+/g, ' ')

  if (!normalized) {
    return 'Unknown'
  }

  return (
    normalized.charAt(0).toUpperCase()
    + normalized.slice(1)
  )
}

export function formatConfidence(
  value: number,
): string {
  if (!Number.isFinite(value)) {
    return '—'
  }

  const normalized = Math.min(
    1,
    Math.max(0, value),
  )

  return `${Math.round(normalized * 100)}%`
}

export function formatVectorDistance(
  value: number,
): string {
  if (!Number.isFinite(value)) {
    return '—'
  }

  return value.toFixed(4)
}

export function formatTimestamp(
  value: string,
): string {
  const timestamp = Date.parse(value)

  if (Number.isNaN(timestamp)) {
    return value
  }

  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'medium',
  }).format(new Date(timestamp))
}

function toDomToken(value: string): string {
  const token = value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')

  return token || 'unknown'
}

export function evidenceElementId(
  evidenceId: string,
): string {
  return `evidence-${toDomToken(evidenceId)}`
}

export function runbookElementId(
  runbookId: string,
): string {
  return `runbook-${toDomToken(runbookId)}`
}

export function isReferenced(
  references: readonly string[],
  identifier: string | null,
): boolean {
  return (
    identifier !== null
    && references.includes(identifier)
  )
}

export interface DisplayLabelPair {
  key: string
  value: string
}

export function formatOptionalValue(
  value: string | number | null,
): string {
  if (value === null) {
    return 'Not set'
  }

  if (
    typeof value === 'string'
    && !value.trim()
  ) {
    return 'Not set'
  }

  return String(value)
}

export function formatLabelPairs(
  pairs: readonly DisplayLabelPair[],
): string {
  if (pairs.length === 0) {
    return 'None'
  }

  return pairs
    .map(({ key, value }) => `${key}=${value}`)
    .join(', ')
}

export type OutcomeTone =
  | 'success'
  | 'warning'
  | 'danger'
  | 'neutral'

export function outcomeTone(
  status: string | null,
): OutcomeTone {
  if (
    status === 'succeeded'
    || status === 'already_applied'
    || status === 'approved'
  ) {
    return 'success'
  }

  if (
    status === 'pending'
    || status === 'skipped'
    || status === 'timeout'
    || status === 'conflict'
  ) {
    return 'warning'
  }

  if (
    status === 'failed'
    || status === 'rejected'
  ) {
    return 'danger'
  }

  return 'neutral'
}

export function formatJsonValue(
  value: unknown,
): string {
  if (typeof value === 'string') {
    return value
  }

  const serialized = JSON.stringify(
    value,
    null,
    2,
  )

  return serialized ?? '—'
}