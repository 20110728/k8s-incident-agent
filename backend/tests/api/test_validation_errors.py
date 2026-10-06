"""Validation errors stay JSON-serializable without echoing input or ctx."""
from typing import Annotated

from fastapi import FastAPI, Query
from fastapi.testclient import TestClient
import pytest

from backend.app.api.errors import register_exception_handlers
from backend.app.services.message_schemas import CreateMessage


@pytest.fixture
def validation_client():
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/messages")
    def submit(body: CreateMessage, limit: Annotated[int, Query(ge=1)] = 20):
        pytest.fail("invalid request must not reach the endpoint")

    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("content", [" ", "\x00", "x" * 16001])
def test_custom_and_builtin_validation_errors_are_json(validation_client, content):
    response = validation_client.post("/messages", json={"client_message_id": "one", "content": content})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "REQUEST_VALIDATION_ERROR"
    assert error["details"][0]["loc"] == ["body", "content"]
    assert error["details"][0]["msg"]
    assert set(error["details"][0]) == {"loc", "type", "msg"}


def test_invalid_json_and_query_keep_validation_envelope(validation_client):
    malformed = validation_client.post("/messages", content='{', headers={"Content-Type": "application/json"})
    query = validation_client.post("/messages?limit=0", json={"client_message_id": "one", "content": "valid"})
    for response in (malformed, query):
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "REQUEST_VALIDATION_ERROR"
        assert all(set(item) == {"loc", "type", "msg"} for item in response.json()["error"]["details"])
    assert query.json()["error"]["details"][0]["loc"] == ["query", "limit"]
