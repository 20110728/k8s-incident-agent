import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.app.config import ApiSettings
from backend.app.main import create_app


ALLOWED_ORIGIN = "http://127.0.0.1:5173"
DISALLOWED_ORIGIN = "https://untrusted.example"


def make_test_settings(
    origins: tuple[str, ...] = (
        ALLOWED_ORIGIN,
    ),
) -> ApiSettings:
    return ApiSettings(
        _env_file=None,
        environment="test",
        cors_allowed_origins=origins,
    )


def test_allowed_origin_is_added_to_response() -> None:
    app = create_app(make_test_settings())

    with TestClient(app) as client:
        response = client.get(
            "/healthz",
            headers={
                "Origin": ALLOWED_ORIGIN,
            },
        )

    assert response.status_code == 200
    assert (
        response.headers[
            "access-control-allow-origin"
        ]
        == ALLOWED_ORIGIN
    )


def test_incident_preflight_is_allowed() -> None:
    app = create_app(make_test_settings())

    with TestClient(app) as client:
        response = client.options(
            "/api/v1/incidents",
            headers={
                "Origin": ALLOWED_ORIGIN,
                "Access-Control-Request-Method": (
                    "POST"
                ),
                "Access-Control-Request-Headers": (
                    "content-type"
                ),
            },
        )

    assert response.status_code == 200
    assert (
        response.headers[
            "access-control-allow-origin"
        ]
        == ALLOWED_ORIGIN
    )

    allowed_methods = response.headers[
        "access-control-allow-methods"
    ]
    assert "POST" in allowed_methods

    allowed_headers = response.headers[
        "access-control-allow-headers"
    ].lower()
    assert "content-type" in allowed_headers


def test_disallowed_origin_is_not_authorized() -> None:
    app = create_app(make_test_settings())

    with TestClient(app) as client:
        response = client.get(
            "/healthz",
            headers={
                "Origin": DISALLOWED_ORIGIN,
            },
        )

    assert response.status_code == 200
    assert (
        "access-control-allow-origin"
        not in response.headers
    )


def test_cors_can_be_disabled() -> None:
    app = create_app(
        make_test_settings(origins=())
    )

    with TestClient(app) as client:
        response = client.get(
            "/healthz",
            headers={
                "Origin": ALLOWED_ORIGIN,
            },
        )

    assert response.status_code == 200
    assert (
        "access-control-allow-origin"
        not in response.headers
    )


def test_cors_origins_are_read_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "INCIDENT_AGENT_API_CORS_ALLOWED_ORIGINS",
        (
            '["http://127.0.0.1:5173",'
            '"https://console.example"]'
        ),
    )

    settings = ApiSettings(_env_file=None)

    assert settings.cors_allowed_origins == (
        "http://127.0.0.1:5173",
        "https://console.example",
    )


def test_cors_origins_are_normalized() -> None:
    settings = make_test_settings(
        origins=(
            "http://localhost:5173/",
            "http://localhost:5173",
        )
    )

    assert settings.cors_allowed_origins == (
        "http://localhost:5173",
    )


def test_wildcard_cors_origin_is_rejected() -> None:
    with pytest.raises(
        ValidationError,
        match="wildcard CORS origins",
    ):
        make_test_settings(
            origins=("*",)
        )


def test_cors_origin_with_path_is_rejected() -> None:
    with pytest.raises(
        ValidationError,
        match="invalid CORS origin",
    ):
        make_test_settings(
            origins=(
                "https://console.example/app",
            )
        )