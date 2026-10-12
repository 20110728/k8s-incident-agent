"""Append-only observations; current view replaces only a matching evidence slot."""
from copy import deepcopy
from datetime import datetime
from backend.app.investigation.records import digest


def slot(item):
    data = item.get("data", {})
    return (item.get("resource_type"), item.get("resource_name"), data.get("container_name"), data.get("previous"))


def active_evidence(baseline, observations):
    # Preserve duplicate baseline observations: fact validators must see them,
    # rather than silently turning ambiguous input into one apparently valid row.
    current = deepcopy(baseline.get("evidence", []))
    for item in observations:
        def matches(value):
            old, new = value.get("data", {}), item.get("data", {})
            if old.get("namespace") and new.get("namespace") and old["namespace"] != new["namespace"]:
                return False
            # A recreated object is a conflict, not a newer version of one UID.
            if value.get("resource_type") == item.get("resource_type") and old.get("uid") and new.get("uid") and old["uid"] != new["uid"]:
                return False
            if slot(value) == slot(item):
                return True
            for kind, name in item.get("invalidates", []):
                if value.get("resource_type") == kind and (value.get("resource_name") == name or
                        kind == "BusinessCheck" and value.get("resource_name", "").startswith(name + "/")):
                    return True
            if item.get("invalidates_log"):
                name, container, previous = item["invalidates_log"]
                return (value.get("resource_type") == "PodLogs" and value.get("resource_name") == name and
                        old.get("container_name") in (None, container) and old.get("previous", False) == previous)
            return False
        replaced = [value for value in current if matches(value)]
        def timestamp(value):
            try:
                result = datetime.fromisoformat(value.get("collected_at", "").replace("Z", "+00:00"))
                return result if result.tzinfo else None
            except (ValueError, AttributeError):
                return None
        sampled = timestamp(item)
        if sampled is not None and any(timestamp(value) is not None and timestamp(value) > sampled for value in replaced):
            continue  # Out-of-order replay must not replace a newer observation.
        current = [value for value in current if not matches(value)]
        current.append(deepcopy(item))
    return current


def adapt(result, request, ref):
    """Never parse a truncated JSON string or promote an incomplete summary to facts."""
    common = {"source": "investigation_tool", "collected_at": result["collected_at"],
              "request_id": result["request_id"], "coverage": result["coverage"],
              "truncated": result["truncated"], "untrusted": True}
    rows = []
    def add(kind, name, data, error=None):
        rows.append({**common, "resource_type": kind, "resource_name": name, "data": data, "error": error,
                     "evidence_id": "ev-" + digest([result["request_id"], len(rows)])[:20] + "-001"})
    payload = result.get("payload")
    if result["coverage"] == "unknown":
        add("ToolObservation", ref["name"], {"tool": request["tool"], "text": result["text"]}, result["error_code"])
    elif request["tool"] == "pod_logs":
        add("PodLogs", ref["name"], {"namespace": ref["namespace"], "pod_name": ref["name"],
            "uid": ref["uid"], "container_name": ref["container"], "previous": request.get("previous", False),
            "content": payload if isinstance(payload, str) else result["text"]})
    elif request["tool"] == "resource_summary" and result["coverage"] == "observed" and isinstance(payload, dict):
        add("Service", ref["service"], payload["service"])
        add("Deployment", ref["deployment"], payload["deployment"])
    elif request["tool"] == "deployment" and result["coverage"] == "observed" and isinstance(payload, dict):
        add("Deployment", ref["deployment"], payload)
    elif request["tool"] == "registered_business" and isinstance(payload, list):
        for check in payload:
            add("BusinessCheck", f"{ref['service']}/{check.get('check_id')}", check)
            rows[-1]["source"] = "cluster_http_probe"
    else:
        # A single slice/event tail/ReplicaSet is supplementary, not complete
        # Pod/Endpoint inventory. Keep it visible without upgrading ready facts.
        add("ToolObservation", f"{ref['name']}/{request['tool']}", {"tool": request["tool"],
            "namespace": ref["namespace"], "uid": ref["uid"], "text": result["text"],
            **({"payload": payload, "payload_complete": True} if result.get("payload_complete") is True else {})})
    if not rows:
        add("ToolObservation", ref["name"], {"tool": request["tool"]}, "EMPTY_OBSERVATION")
    invalidate = {"registered_business": [("BusinessCheck", ref["service"])],
                  "resource_summary": [("Service", ref["service"]), ("Deployment", ref["deployment"])],
                  "deployment": [("Deployment", ref["deployment"])]}.get(request["tool"], [])
    if rows and invalidate:
        rows[0]["invalidates"] = invalidate
    if request["tool"] == "pod_logs":
        rows[0]["invalidates_log"] = [ref["name"], ref["container"], request.get("previous", False)]
    return rows


def current_state(baseline, observations):
    state = {**baseline, "evidence": active_evidence(baseline, observations)}
    active_ids = {row["evidence_id"] for row in state["evidence"]}
    state["historical_evidence_ids"] = list(dict.fromkeys(
        row["evidence_id"] for row in [*baseline.get("evidence", []), *observations]
        if row["evidence_id"] not in active_ids))
    return state
