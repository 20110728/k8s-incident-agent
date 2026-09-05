import {
  describe,
  expect,
  it,
} from 'vitest'

import {
  buildIncidentSearch,
  normalizeIncidentId,
  resolveIncidentId,
} from './incidentSession'

describe('incident session helpers', () => {
  it('normalizes valid incident IDs', () => {
    expect(
      normalizeIncidentId(
        ' 1b55dea9-de39-4c5b-9322-b896542b5337 ',
      ),
    ).toBe(
      '1b55dea9-de39-4c5b-9322-b896542b5337',
    )
  })

  it('rejects invalid incident IDs', () => {
    expect(normalizeIncidentId(null)).toBeNull()
    expect(normalizeIncidentId('')).toBeNull()
    expect(
      normalizeIncidentId('../incident'),
    ).toBeNull()
    expect(
      normalizeIncidentId('incident?id=1'),
    ).toBeNull()
    expect(
      normalizeIncidentId('a'.repeat(129)),
    ).toBeNull()
  })

  it('prefers the URL incident ID over storage', () => {
    expect(
      resolveIncidentId(
        '?incident_id=url-incident',
        'stored-incident',
      ),
    ).toBe('url-incident')

    expect(
      resolveIncidentId(
        '',
        'stored-incident',
      ),
    ).toBe('stored-incident')
  })

  it('builds a query string without removing other parameters', () => {
    expect(
      buildIncidentSearch(
        '?view=details',
        'incident-123',
      ),
    ).toBe(
      '?view=details&incident_id=incident-123',
    )
  })
})