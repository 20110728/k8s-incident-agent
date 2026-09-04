from fastapi.testclient import TestClient

from backend.app.config import ApiSettings
from backend.app.main import create_app


def make_test_settings() -> ApiSettings:
    return ApiSettings(
        _env_file=None,
        app_name="Test Incident Agent API",
        app_version="test-version",
        environment="test",
    )


def test_settings_read_prefixed_environment(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "INCIDENT_AGENT_API_ENVIRONMENT",
        "test",
    )
    monkeypatch.setenv(
        "INCIDENT_AGENT_API_DOCS_ENABLED",
        "false",
    )

    settings = ApiSettings(_env_file=None)

    assert settings.environment == "test"
    assert settings.docs_enabled is False


def test_health_endpoint() -> None:
    app = create_app(make_test_settings())

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "Test Incident Agent API",
        "version": "test-version",
    }


def test_readiness_endpoint() -> None:
    app = create_app(make_test_settings())

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {
            "api": True,
        },
    }


def test_not_ready_uses_standard_error() -> None:
    app = create_app(make_test_settings())
    app.state.ready = False

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "code": "SERVICE_NOT_READY",
            "message": "The API is not ready.",
            "details": {
                "checks": {
                    "api": False,
                }
            },
        }
    }


def test_openapi_contains_system_contracts() -> None:
    app = create_app(make_test_settings())

    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    document = response.json()
    assert "/healthz" in document["paths"]
    assert "/readyz" in document["paths"]
    assert (
        document["info"]["title"]
        == "Test Incident Agent API"
    )


def test_docs_can_be_disabled() -> None:
    settings = make_test_settings().model_copy(
        update={
            "docs_enabled": False,
        }
    )
    app = create_app(settings)

    with TestClient(app) as client:
        docs_response = client.get("/docs")
        openapi_response = client.get(
            "/openapi.json"
        )

    assert docs_response.status_code == 404
    assert openapi_response.status_code == 404


def test_unknown_route_uses_standard_error() -> None:
    app = create_app(make_test_settings())

    with TestClient(app) as client:
        response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message": "Not Found",
            "details": None,
        }
    }