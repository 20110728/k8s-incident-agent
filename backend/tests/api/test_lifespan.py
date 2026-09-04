from collections.abc import Iterator
from contextlib import contextmanager

from fastapi.testclient import TestClient

from backend.app.config import ApiSettings
from backend.app.main import create_app
from backend.app.services.incident_service import IncidentSnapshot
from backend.tests.api.fakes import FakeIncidentService


INCIDENT_ID = "incident-lifespan-test"


def test_lifespan_exposes_service_to_dependency() -> None:
    service = FakeIncidentService(
        get_result=IncidentSnapshot(
            incident_id=INCIDENT_ID,
            thread_id=INCIDENT_ID,
            state={
                "incident_id": INCIDENT_ID,
                "request": {
                    "namespace": "agent-demo",
                    "service_name": "order-service",
                    "description": "服务异常",
                },
                "phase": "diagnosis_completed",
            },
        )
    )
    events: list[str] = []

    @contextmanager
    def fake_service_context() -> (
        Iterator[FakeIncidentService]
    ):
        events.append("entered")
        try:
            yield service
        finally:
            events.append("exited")

    settings = ApiSettings(
        _env_file=None,
        environment="test",
    )
    app = create_app(
        settings,
        service_context_factory=fake_service_context,
    )

    with TestClient(app) as client:
        assert app.state.ready is True
        assert app.state.incident_service is service

        response = client.get(
            f"/api/v1/incidents/{INCIDENT_ID}"
        )

        assert response.status_code == 200
        assert response.json()["incident_id"] == (
            INCIDENT_ID
        )

    assert app.state.ready is False
    assert not hasattr(
        app.state,
        "incident_service",
    )
    assert events == ["entered", "exited"]