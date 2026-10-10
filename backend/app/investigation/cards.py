"""Versioned, deterministic read projections. Never collect, infer health or mutate evidence.

C1 exposes these through the opt-in diagnostic export. Paid prompts deliberately
keep their existing representation until C2 migrates context assembly.
"""
from datetime import datetime
import json
import re

from backend.app.investigation.records import digest
from backend.app.tools.investigation import redact_output

VERSION = "evidence-card-v1"
TEXT_LIMIT = 600
LIST_LIMIT = 20
GROUP_LIMIT = 8
SCAN_LIMIT = 120000

FIELDS = {
    "Service": ("namespace", "name", "selector", "ports", "service_type"),
    "Deployment": ("namespace", "name", "generation", "desired_replicas", "ready_replicas",
                   "available_replicas", "unavailable_replicas", "selector", "template_labels", "containers"),
    "PodStatus": ("namespace", "name", "phase", "ready", "containers"),
    "EndpointSlice": ("namespace", "name", "service_uid", "service_name", "endpoints"),
    "BusinessCheck": ("check_id", "status", "http_status", "content_matches", "error_code", "scope"),
    "PodSelection": ("namespace", "service_pod_names", "namespace_pod_names", "matched_count",
                     "selected_count", "truncated", "pod_names"),
    "OwnerChain": ("owner_chain",),
    "Node": ("name", "ready", "conditions"),
    "PodEvents": ("events",),
}
CONTAINER_FIELDS = ("name", "state", "ready", "restart_count", "waiting_reason", "waiting_message",
                    "terminated_reason", "terminated_exit_code", "last_terminated_reason",
                    "last_terminated_exit_code", "image", "requests", "limits", "resources",
                    "readiness_probe", "liveness_probe")
TIME_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+)\s(.*)$")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(value):
    """Redact before truncating, including credentials straddling a limit."""
    def remove_execution_details(part):
        if isinstance(part, dict):
            return {k: "[OMITTED]" if str(k).lower() in {"command", "args", "env", "envfrom"}
                    else remove_execution_details(v) for k, v in part.items()}
        if isinstance(part, list):
            return [remove_execution_details(v) for v in part]
        return part
    return json.loads(redact_output(remove_execution_details(value)))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def bounded(value, omissions, path="fields", depth=0):
    if isinstance(value, str):
        text = CONTROL.sub("", ANSI.sub("", value))
        if len(text) > TEXT_LIMIT:
            omissions.append({"path": path, "characters": len(text) - TEXT_LIMIT})
        return text[:TEXT_LIMIT]
    if isinstance(value, (dict, list)):
        if depth >= 5:
            omissions.append({"path": path, "reason": "depth_limit"})
            return None
        if isinstance(value, list):
            if len(value) > LIST_LIMIT:
                omissions.append({"path": path, "items": len(value) - LIST_LIMIT})
            return [bounded(v, omissions, f"{path}[{i}]", depth + 1)
                    for i, v in enumerate(value[:LIST_LIMIT])]
        keys = sorted(value)
        if len(keys) > LIST_LIMIT:
            omissions.append({"path": path, "keys": len(keys) - LIST_LIMIT})
        return {k: bounded(value[k], omissions, f"{path}.{k}", depth + 1) for k in keys[:LIST_LIMIT]}
    return value


def grouped_logs(content):
    """Only valid timestamp prefixes are removed for exact-message grouping.

    Group before redaction/normalization so distinct credentials, numbers and
    control bytes cannot collapse into one apparently repeated observation.
    Counts refer only to the scanned source; positions are original line numbers.
    """
    lines = content.splitlines()
    groups = {}
    scanned = 0
    for number, line in enumerate(lines, 1):
        if scanned + len(line) + 1 > SCAN_LIMIT:
            break
        scanned += len(line) + 1
        match = TIME_PREFIX.match(line)
        timestamp, message = None, line
        if match:
            try:
                parsed = datetime.fromisoformat(match[1].replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    timestamp, message = match[1], match[2]
            except ValueError:
                pass
        if message not in groups:
            groups[message] = {"message": message, "count": 0, "first_line": number,
                               "first_timestamp": timestamp}
        group = groups[message]
        group.update(count=group["count"] + 1, last_line=number, last_timestamp=timestamp)
    values = list(groups.values())
    selected = values if len(values) <= GROUP_LIMIT else values[:4] + values[-4:]
    scanned_lines = sum(g["count"] for g in values)
    omissions = []
    return {"groups": bounded(clean(selected), omissions, "logs.groups"),
            "input_lines": len(lines), "scanned_lines": scanned_lines,
            "omitted_lines": len(lines) - scanned_lines,
            "omitted_groups": len(values) - len(selected),
            "omitted_group_occurrences": sum(g["count"] for g in values) - sum(g["count"] for g in selected),
            "projection_omissions": omissions,
            "scope": "Counts describe scanned saved lines only; first/last follow source order, not time sorting."}


def project(kind, data, truncated):
    """Return selected fields plus an explicit parser result; no health inference."""
    if not isinstance(data, dict):
        return {}, "unparsed", "DATA_NOT_OBJECT"
    if kind == "PodLogs":
        if not isinstance(data.get("content"), str):
            return {}, "unparsed", "LOG_CONTENT_NOT_TEXT"
        return {"logs": grouped_logs(data["content"])}, "parsed", None
    if kind == "ToolObservation":
        # Older records contain JSON text rather than a typed payload. Never
        # salvage a cut JSON prefix or interpret a partial string as full facts.
        if truncated or not isinstance(data.get("text"), str):
            return {}, "unparsed", "TOOL_TEXT_INCOMPLETE_OR_MISSING"
        try:
            payload = json.loads(data["text"])
        except (ValueError, TypeError):
            return {}, "unparsed", "TOOL_TEXT_NOT_JSON"
        tool = data.get("tool")
        if tool == "pod_events" and isinstance(payload, list):
            return project("PodEvents", {"events": payload}, False)
        if tool == "endpoint_slice" and isinstance(payload, dict) and isinstance(payload.get("endpoints"), list):
            return project("EndpointSlice", {"endpoints": payload["endpoints"]}, False)
        if tool == "replica_set" and isinstance(payload, dict):
            fields = {k: payload[k] for k in ("replicas", "ready_replicas") if k in payload}
            if any(v is not None and type(v) is not int for v in fields.values()):
                return {}, "unparsed", "INVALID_INTEGER_FIELD"
            return fields, "parsed" if fields else "unparsed", None if fields else "NO_KNOWN_FIELDS"
        return {}, "unparsed", "UNSUPPORTED_TOOL_PAYLOAD"
    names = FIELDS.get(kind)
    if names is None:
        return {}, "unparsed", "UNSUPPORTED_RESOURCE_TYPE"
    fields = {k: data[k] for k in names if k in data}
    for key in ("ready", "content_matches", "truncated"):
        if key in fields and fields[key] is not None and type(fields[key]) is not bool:
            return {}, "unparsed", "INVALID_BOOLEAN_FIELD"
    for key in ("desired_replicas", "ready_replicas", "available_replicas", "unavailable_replicas", "http_status"):
        if key in fields and fields[key] is not None and type(fields[key]) is not int:
            return {}, "unparsed", "INVALID_INTEGER_FIELD"
    for key in ("containers", "endpoints", "events", "conditions"):
        if key in fields and (not isinstance(fields[key], list) or
                              any(not isinstance(v, dict) for v in fields[key])):
            return {}, "unparsed", "INVALID_COLLECTION_SHAPE"
    if "containers" in fields:
        fields["containers"] = [{k: c[k] for k in CONTAINER_FIELDS if k in c} for c in fields["containers"]]
    return fields, "parsed" if fields else "unparsed", None if fields else "NO_KNOWN_FIELDS"


def evidence_card(item, *, record_path=None):
    """Accept legacy JSON evidence without inventing missing UID/time/coverage."""
    item = item if isinstance(item, dict) else {"data": item}
    data = item.get("data")
    mapping = data if isinstance(data, dict) else {}
    kind = item.get("resource_type")
    source_truncated = item.get("truncated") is True or mapping.get("truncated") is True
    fields, status, reason = project(kind, data, source_truncated)
    omissions = []
    # Log groups already have independent message bounds and count metadata.
    projected = fields if kind == "PodLogs" and status == "parsed" else bounded(clean(fields), omissions)
    if kind == "PodLogs" and status == "parsed":
        omissions.extend(projected["logs"]["projection_omissions"])
        if projected["logs"]["omitted_lines"] or projected["logs"]["omitted_groups"]:
            omissions.append({"path": "logs", "reason": "log_selection_limit"})
    raw = mapping.get("content") if kind == "PodLogs" else mapping.get("text") if kind == "ToolObservation" else data
    excerpt = clean(raw)
    excerpt = excerpt if isinstance(excerpt, str) else canonical(excerpt)
    excerpt_omissions = []
    excerpt = bounded(excerpt, excerpt_omissions, "excerpt")
    error = item.get("error") or mapping.get("error_code")
    coverage = item.get("coverage") or "unspecified_legacy_snapshot"
    identity = {"resource_type": kind, "resource_name": item.get("resource_name"),
                "namespace": mapping.get("namespace"), "uid": mapping.get("uid"),
                "container": mapping.get("container_name"), "previous": mapping.get("previous")}
    return {"schema_version": VERSION, "evidence_id": item.get("evidence_id"),
            "source_ref": {"record_path": record_path, "request_id": item.get("request_id"),
                           "record_digest": digest(item)},
            "identity": clean(identity), "source": clean(item.get("source")),
            "collected_at": item.get("collected_at"), "coverage": clean(coverage),
            "source_truncated": source_truncated, "error": clean(error),
            "observation_status": "failed_or_unknown" if error or coverage == "unknown" else "recorded_not_health_verified",
            "parse_status": status, "parse_reason": reason, "fields": projected,
            "projection_omissions": omissions, "excerpt": excerpt, "excerpt_source": "saved_data",
            "excerpt_truncated": bool(excerpt_omissions), "untrusted": True,
            "limitations": ["Selected fields are not complete evidence or a diagnosis.",
                            "Missing identity/time/coverage stays unknown; no evidence is resampled."]}


def saved_cards(snapshot, budget, *, limit=100):
    """Project saved records without merging away historical samples or duplicate IDs."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("CARD_LIMIT_OUT_OF_RANGE")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    baseline = snapshot.get("baseline") or budget.get("investigation_baseline") or {}
    baseline = baseline if isinstance(baseline, dict) else {}
    path = "output_snapshot.baseline.evidence" if snapshot.get("baseline") else "budget.investigation_baseline.evidence"
    rows = [(path, baseline.get("evidence", [])), ("output_snapshot.observations", snapshot.get("observations", []))]
    if not baseline and "evidence" in snapshot:
        rows[0] = ("output_snapshot.evidence", snapshot["evidence"])
    entries = [(f"{path}[{i}]", item) for path, items in rows if isinstance(items, list)
               for i, item in enumerate(items)]
    return {"schema_version": VERSION, "cards": [evidence_card(item, record_path=path)
            for path, item in entries[:limit]], "omitted_records": max(0, len(entries) - limit),
            "scope": "Saved baseline and published observations only; not a current-state projection. Running or legacy records may be incomplete."}
