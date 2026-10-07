"""Read-only smoke checks against an existing 4B-2 ECS API; never create jobs."""
import json
import os
from urllib.parse import quote, urlencode
from urllib.request import urlopen


def main():
    base = os.environ.get("STAGE4C_API_URL", "http://127.0.0.1:8000").rstrip("/")

    def read(path):
        with urlopen(base + path, timeout=20) as response:
            assert response.status == 200, path
            return json.load(response)

    def page(path, cursor_name, cursor_field):
        first = read(path + "?limit=1")
        assert isinstance(first["items"], list) and cursor_field in first, path
        if first[cursor_field] is not None:
            following = read(path + "?" + urlencode({"limit": 1, cursor_name: first[cursor_field]}))
            assert isinstance(following["items"], list), path
            assert not following["items"] or following["items"][0] != first["items"][0], path
        print(f"PASS GET {path}: page and cursor contract")
        return first["items"]

    assert read("/readyz")["status"] == "ready"
    incidents = page("/api/v1/incidents", "cursor", "next_cursor")
    incident_id = os.environ.get("STAGE4C_INCIDENT_ID") or (incidents[0]["incident_id"] if incidents else None)
    assert incident_id, "Need an existing incident; set STAGE4C_INCIDENT_ID to a known ID."
    root = "/api/v1/incidents/" + quote(incident_id, safe="")
    current = read(root)
    assert current["execution_mode"] == "queued", "4C interactions require queued mode."
    assert current["incident_id"] == incident_id
    assert isinstance(read(root + "/operations")["items"], list)
    status = read(root + "/interaction-status")
    assert status["incident_id"] == incident_id and status["model_called"] is False
    runs = page(root + "/runs", "cursor", "next_cursor")
    messages = page(root + "/messages", "before_sequence", "next_before_sequence")
    rechecks = page(root + "/rechecks", "before_sequence", "next_before_sequence")
    for message in messages:
        assert isinstance(message["adopted_by_run_ids"], list)
    for recheck in rechecks:
        assert all(key in recheck for key in ("started_at", "finished_at", "target_comparison", "unverified_scope"))
    for run in runs:
        path = root + ("/interactions/" if run["run_kind"] == "interaction" else "/runs/") + quote(run["run_id"], safe="")
        result = read(path)
        assert ("calls" in result and "output" in result) if run["run_kind"] == "interaction" else "result" in result
    print("PASS: live read-only API contracts; no model, collection or write was requested.")


if __name__ == "__main__":
    main()
