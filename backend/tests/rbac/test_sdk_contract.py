"""SDK contract regression; real authentication remains covered by ECS tests."""
import pytest
from kubernetes.client.exceptions import ApiException

from backend.tests.rbac.lab import Lab, call_json_api


class LegacyApi:
    def __init__(self):
        self.calls = []

    def call_api(self, path, method, response_type=None, **kwargs):
        assert response_type == "object"
        self.calls.append((path, method, kwargs))
        return {"status": "ready"}, 200, {}

    def close(self):
        pass


class MapApi(LegacyApi):
    def call_api(self, path, method, response_types_map=None, **kwargs):
        assert response_types_map[200] == "object"
        assert response_types_map[202] == "object"
        assert "response_type" not in kwargs
        self.calls.append((path, method, kwargs))
        return {"status": "ready"}, 200, {}


@pytest.mark.parametrize("api_class", [LegacyApi, MapApi])
def test_request_and_uid_cleanup_use_supported_sdk_contract(api_class, tmp_path):
    api = api_class()
    lab = Lab.__new__(Lab)
    lab.directory, lab.results = tmp_path, []
    lab.admin, lab.apis = api, []
    lab.created = [("/api/v1/namespaces/agent-demo/services/test", "original-uid")]
    value = lab.request(api, "test-subject", "probe", "GET", "/readyz", 200)
    assert value == {"status": "ready"}
    lab.close()
    assert len(api.calls) == 2
    path, method, kwargs = api.calls[1]
    assert path == lab.created[0][0] and method == "DELETE"
    assert kwargs["body"]["preconditions"] == {"uid": "original-uid"}
    assert kwargs["header_params"]["Content-Type"] == "application/json"
    assert kwargs["auth_settings"] == ["BearerToken"]
    assert (tmp_path / "cleanup.json").read_text() == "[]"


@pytest.mark.parametrize("error", [TypeError("transport failure"), ApiException(status=403)])
def test_write_errors_are_not_retried_with_another_signature(error):
    class FailingApi(MapApi):
        def call_api(self, path, method, response_types_map=None, **kwargs):
            self.calls.append((path, method, kwargs))
            raise error

    api = FailingApi()
    with pytest.raises(type(error)):
        call_json_api(api, "/target", "PATCH", body={"spec": {}},
                      query=[("dryRun", "All")], content_type="application/merge-patch+json")
    assert len(api.calls) == 1
    kwargs = api.calls[0][2]
    assert kwargs["query_params"] == [("dryRun", "All")]
    assert kwargs["header_params"]["Content-Type"] == "application/merge-patch+json"
