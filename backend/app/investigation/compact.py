"""Deterministic prompt projections; full evidence remains in persistence."""
import re
from backend.app.tools.investigation import redact_output


FIELDS = {
    "Service": ("namespace", "name", "selector", "ports"),
    "Deployment": ("name", "desired_replicas", "ready_replicas", "available_replicas", "containers"),
    "PodStatus": ("namespace", "phase", "ready", "containers"),
    "EndpointSlice": ("name", "endpoints"),
    "BusinessCheck": ("check_id", "status", "http_status", "content_matches", "error_code", "scope"),
    "PodSelection": ("matched_count", "selected_count", "truncated", "pod_names"),
}
CONTAINER_FIELDS = ("name", "state", "ready", "restart_count", "waiting_reason", "last_terminated_reason",
                    "last_terminated_exit_code", "exit_code", "resources", "readiness_probe", "liveness_probe")


def log_excerpt(content):
    groups = {}
    for line in str(content).splitlines():
        # Kubernetes --timestamps prefixes vary per line. Preserve first/last
        # original examples while grouping only identical message text.
        key = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", line)
        if key not in groups:
            groups[key] = {"first": line[:220], "last": line[:220], "count": 0}
        groups[key]["last"] = line[:220]
        groups[key]["count"] += 1
    values = list(groups.values())
    selected = values if len(values) <= 8 else values[:4] + values[-4:]
    return {"groups": selected, "omitted_groups": max(0, len(values) - len(selected)),
            "format": "bounded first/last examples, exact-message counts; not complete logs"}


def evidence_text(item):
    data = item.get("data", {})
    kind = item.get("resource_type")
    if not isinstance(data, dict):
        return redact_output(data)
    if kind == "PodLogs":
        value = {"logs": log_excerpt(data.get("content", "")),
                 "container": data.get("container_name"), "previous": data.get("previous", False)}
    elif kind in FIELDS:
        value = {k: data[k] for k in FIELDS[kind] if k in data}
        if "containers" in value:
            value["containers"] = [{k: c[k] for k in CONTAINER_FIELDS if k in c} for c in value["containers"][:10]]
        if "endpoints" in value:
            value["endpoints"] = [{k: e[k] for k in ("ready", "target_name", "target_uid", "conditions") if k in e}
                                  for e in value["endpoints"][:20]]
    elif kind == "ToolObservation":
        value = {"tool": data.get("tool"), "excerpt": str(data.get("text", ""))[:700]}
    else:
        value = data
    return redact_output(value)


def compact_history(history):
    return [{**{k: h[k] for k in ("step", "action", "requests", "results", "evidence_ids") if k in h},
             "reason": str(h.get("reason") or "")[:120]} for h in history[-3:]]
