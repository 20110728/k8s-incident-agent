"""Small log previews for polling; saved/model evidence remains unchanged."""
from copy import deepcopy

PREVIEW_CHARACTERS = 2000


def evidence_previews(rows):
    result = []
    for item in rows:
        data = item.get("data") or {}
        if item.get("resource_type") != "PodLogs" and data.get("tool") != "pod_logs":
            result.append(item)
            continue
        view = deepcopy(item)
        omitted = []
        for key in ("content", "text", "payload"):
            value = data.get(key)
            if isinstance(value, str) and len(value) > PREVIEW_CHARACTERS:
                view["data"][key] = value[-PREVIEW_CHARACTERS:]
                omitted.append(key)
        if omitted:
            view["body_preview"] = {"truncated": True, "fields": omitted,
                                    "scope": "Recent preview only; load saved body explicitly."}
        result.append(view)
    return result
