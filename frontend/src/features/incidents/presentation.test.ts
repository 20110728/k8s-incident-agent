import {
  describe,
  expect,
  it,
} from 'vitest'

import {
  evidenceElementId,
  formatConfidence,
  formatLabel,
  formatTimestamp,
  formatVectorDistance,
  isReferenced,
  runbookElementId,
  formatLabelPairs,
  formatOptionalValue,
  formatJsonValue,
  outcomeTone,
} from './presentation'

describe('incident presentation helpers', () => {
  it('formats machine-readable labels', () => {
    expect(
      formatLabel('no_fault_detected'),
    ).toBe('No fault detected')

    expect(
      formatLabel(
        'service-selector-mismatch',
      ),
    ).toBe('Service selector mismatch')
  })

  it('formats and bounds confidence values', () => {
    expect(formatConfidence(0.95)).toBe('95%')
    expect(formatConfidence(2)).toBe('100%')
    expect(formatConfidence(-1)).toBe('0%')
    expect(formatConfidence(Number.NaN)).toBe('—')
  })

  it('formats vector distance without treating it as confidence', () => {
    expect(
      formatVectorDistance(
        0.3286473946957216,
      ),
    ).toBe('0.3286')

    expect(
      formatVectorDistance(Number.NaN),
    ).toBe('—')
  })

  it('creates stable DOM identifiers', () => {
    expect(
      evidenceElementId(
        'ev-241f2068-001',
      ),
    ).toBe('evidence-ev-241f2068-001')

    expect(
      runbookElementId(
        'wrong-http-path',
      ),
    ).toBe('runbook-wrong-http-path')
  })

  it('matches only declared references', () => {
    const references = [
      'ev-001',
      'ev-002',
    ]

    expect(
      isReferenced(references, 'ev-002'),
    ).toBe(true)

    expect(
      isReferenced(references, 'ev-003'),
    ).toBe(false)

    expect(
      isReferenced(references, null),
    ).toBe(false)

    expect(
      formatTimestamp('not-a-date'),
    ).toBe('not-a-date')
  })
  it('formats optional remediation values', () => {
    expect(formatOptionalValue(null)).toBe(
      'Not set',
    )
    expect(formatOptionalValue('')).toBe(
      'Not set',
    )
    expect(formatOptionalValue('   ')).toBe(
      'Not set',
    )
    expect(formatOptionalValue('/healthz')).toBe(
      '/healthz',
    )
    expect(formatOptionalValue(8080)).toBe(
      '8080',
    )
  })

  it('formats structured selector labels', () => {
    expect(formatLabelPairs([])).toBe('None')

    expect(
      formatLabelPairs([
        {
          key: 'app',
          value: 'order-service',
        },
        {
          key: 'tier',
          value: 'backend',
        },
      ]),
    ).toBe(
      'app=order-service, tier=backend',
    )
  })

})

it('maps workflow outcomes to visual tones', () => {
  expect(outcomeTone('succeeded')).toBe(
    'success',
  )
  expect(outcomeTone('approved')).toBe(
    'success',
  )
  expect(outcomeTone('conflict')).toBe(
    'warning',
  )
  expect(outcomeTone('timeout')).toBe(
    'warning',
  )
  expect(outcomeTone('rejected')).toBe(
    'danger',
  )
  expect(outcomeTone('failed')).toBe(
    'danger',
  )
  expect(outcomeTone(null)).toBe(
    'neutral',
  )
})

it('formats primitive result values', () => {
  expect(formatJsonValue('ready')).toBe(
    'ready',
  )
  expect(formatJsonValue(2)).toBe('2')
  expect(formatJsonValue(true)).toBe('true')
  expect(formatJsonValue(null)).toBe('null')
})

it('formats structured result values', () => {
  expect(
    formatJsonValue({
      path: '/healthz',
      port: 'http',
    }),
  ).toBe(
    '{\n'
      + '  "path": "/healthz",\n'
      + '  "port": "http"\n'
      + '}',
  )
})

it('handles an unserializable display value', () => {
  expect(
    formatJsonValue(undefined),
  ).toBe('—')
})