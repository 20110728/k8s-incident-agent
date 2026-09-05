import {
  describe,
  expect,
  it,
} from 'vitest'

import {
  buildApprovalDecision,
} from './approvalDecision'

const approvalId = 'apr-0123456789abcdef'

describe('approval decision builder', () => {
  it('builds a normalized approval request', () => {
    const result = buildApprovalDecision({
      approvalId,
      approved: true,
      approver: '  operator@example.com  ',
      comment: '  Reviewed the evidence.  ',
      acknowledged: true,
    })

    expect(result).toEqual({
      ok: true,
      request: {
        approval_id: approvalId,
        approved: true,
        approver: 'operator@example.com',
        comment: 'Reviewed the evidence.',
      },
    })
  })

  it('builds a rejection request', () => {
    const result = buildApprovalDecision({
      approvalId,
      approved: false,
      approver: 'incident-commander',
      comment: 'The proposed change is rejected.',
      acknowledged: true,
    })

    expect(result).toEqual({
      ok: true,
      request: {
        approval_id: approvalId,
        approved: false,
        approver: 'incident-commander',
        comment: 'The proposed change is rejected.',
      },
    })
  })

  it('requires an acknowledged review', () => {
    expect(
      buildApprovalDecision({
        approvalId,
        approved: true,
        approver: 'operator',
        comment: '',
        acknowledged: false,
      }),
    ).toEqual({
      ok: false,
      error:
        'Review and acknowledge the remediation plan before submitting a decision.',
    })
  })

  it('rejects a blank approver', () => {
    expect(
      buildApprovalDecision({
        approvalId,
        approved: false,
        approver: '   ',
        comment: '',
        acknowledged: true,
      }),
    ).toEqual({
      ok: false,
      error: 'Approver is required.',
    })
  })

  it('rejects an invalid approval ID', () => {
    expect(
      buildApprovalDecision({
        approvalId: 'invalid',
        approved: true,
        approver: 'operator',
        comment: '',
        acknowledged: true,
      }),
    ).toEqual({
      ok: false,
      error: 'The approval request ID is invalid.',
    })
  })

  it('enforces backend length limits', () => {
    expect(
      buildApprovalDecision({
        approvalId,
        approved: true,
        approver: 'a'.repeat(101),
        comment: '',
        acknowledged: true,
      }).ok,
    ).toBe(false)

    expect(
      buildApprovalDecision({
        approvalId,
        approved: true,
        approver: 'operator',
        comment: 'a'.repeat(1001),
        acknowledged: true,
      }).ok,
    ).toBe(false)
  })
})