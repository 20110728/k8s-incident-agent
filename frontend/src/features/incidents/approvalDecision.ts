import type {
  SubmitApprovalRequest,
} from '../../api'

const APPROVAL_ID_PATTERN =
  /^apr-[0-9a-f]{16}$/

export interface ApprovalDecisionInput {
  approvalId: string
  approved: boolean
  approver: string
  comment: string
  acknowledged: boolean
}

export type ApprovalDecisionResult =
  | {
      ok: true
      request: SubmitApprovalRequest
    }
  | {
      ok: false
      error: string
    }

export function buildApprovalDecision(
  input: ApprovalDecisionInput,
): ApprovalDecisionResult {
  if (!APPROVAL_ID_PATTERN.test(input.approvalId)) {
    return {
      ok: false,
      error: 'The approval request ID is invalid.',
    }
  }

  const approver = input.approver.trim()
  const comment = input.comment.trim()

  if (!approver) {
    return {
      ok: false,
      error: 'Approver is required.',
    }
  }

  if (approver.length > 100) {
    return {
      ok: false,
      error:
        'Approver must not exceed 100 characters.',
    }
  }

  if (comment.length > 1000) {
    return {
      ok: false,
      error:
        'Comment must not exceed 1000 characters.',
    }
  }

  if (!input.acknowledged) {
    return {
      ok: false,
      error:
        'Review and acknowledge the remediation plan before submitting a decision.',
    }
  }

  return {
    ok: true,
    request: {
      approval_id: input.approvalId,
      approved: input.approved,
      approver,
      comment,
    },
  }
}