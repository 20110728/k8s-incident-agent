import pytest
from pydantic import ValidationError

from backend.app.agent.schemas import IncidentRequest


def test_incident_request_accepts_valid_input():
    request = IncidentRequest(
        namespace="agent-demo",
        service_name="order-service",
        description="服务无法访问",
    )

    assert request.namespace == "agent-demo"
    assert request.service_name == "order-service"


def test_incident_request_rejects_invalid_service_name():
    with pytest.raises(ValidationError):
        IncidentRequest(
            namespace="agent-demo",
            service_name="Order_Service",
            description="服务无法访问",
        )


def test_incident_request_rejects_blank_description():
    with pytest.raises(ValidationError):
        IncidentRequest(
            namespace="agent-demo",
            service_name="order-service",
            description="   ",
        )
