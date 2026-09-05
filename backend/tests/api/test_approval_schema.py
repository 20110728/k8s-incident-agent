import pytest

from backend.app.api.schemas import (
    SubmitApprovalRequest,
)


APPROVAL_ID = "apr-0123456789abcdef"


def test_submit_approval_request_accepts_valid_payload() -> None:
    request = SubmitApprovalRequest.model_validate(
        {
            "approval_id": APPROVAL_ID,
            "approved": True,
            "approver": "  test-operator  ",
            "comment": "  ready to proceed  ",
        }
    )

    assert request.approval_id == APPROVAL_ID
    assert request.approved is True
    assert request.approver == "test-operator"
    assert request.comment == "ready to proceed"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "approval_id": "wrong-id",
            "approved": True,
            "approver": "test-operator",
        },
        {
            "approval_id": APPROVAL_ID,
            "approved": "true",
            "approver": "test-operator",
        },
        {
            "approval_id": APPROVAL_ID,
            "approved": True,
            "approver": "   ",
        },
        {
            "approval_id": APPROVAL_ID,
            "approved": True,
            "approver": "test-operator",
            "unexpected": "forbidden",
        },
    ],
)
def test_submit_approval_request_rejects_invalid_payload(
    payload: dict,
) -> None:
    with pytest.raises(ValueError):
        SubmitApprovalRequest.model_validate(payload)


def test_submit_approval_request_defaults_comment() -> None:
    request = SubmitApprovalRequest.model_validate(
        {
            "approval_id": APPROVAL_ID,
            "approved": False,
            "approver": "test-operator",
        }
    )

    assert request.comment == ""