"""Prompt-only exact probe grouping; all other sampled lines stay verbatim."""
from datetime import datetime
import json
import re

PROJECTION = "exact-probe-groups-v1"
PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+) (.*)$")
ACCESS = re.compile(r'^"GET /(?:readyz|livez) HTTP/1\.[01]" [1-5][0-9]{2} -$')


def encoded_bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8"))


def probe_line(line):
    message = line.rstrip("\r\n")
    timestamp = None
    match = PREFIX.fullmatch(message)
    if match:
        try:
            if datetime.fromisoformat(match[1].replace("Z", "+00:00")).tzinfo is None:
                return None
        except ValueError:
            return None
        timestamp, message = match.groups()
    try:
        if not message.startswith("{"):
            return None
        # Duplicate JSON keys must not hide an additional diagnostic field.
        pairs = json.loads(message, object_pairs_hook=list)
        if not isinstance(pairs, list) or len(pairs) != 2:
            return None
        value = dict(pairs)
        if (set(value) != {"service", "message"} or not isinstance(value["service"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", value["service"])):
            return None
        if not isinstance(value["message"], str) or not ACCESS.fullmatch(value["message"]):
            return None
    except (ValueError, TypeError, RecursionError):
        return None
    return message, timestamp


def project_logs(content):
    """Return a smaller view or None; never truncate, summarize or merge errors."""
    lines = content.splitlines(keepends=True)
    candidates, groups = [], {}
    for number, line in enumerate(lines, 1):
        parsed = probe_line(line)
        candidates.append(parsed)
        if parsed is None:
            continue
        message, timestamp = parsed
        group = groups.setdefault(message, {
            "sample_raw": line, "count": 0, "first_timestamp": timestamp,
            "last_timestamp": timestamp, "line_ranges": [],
        })
        group["count"] += 1
        group["last_timestamp"] = timestamp
        ranges = group["line_ranges"]
        if ranges and ranges[-1][1] == number - 1:
            ranges[-1][1] = number
        else:
            ranges.append([number, number])
    repeated = {message: group for message, group in groups.items() if group["count"] > 1}
    if not repeated:
        return None
    spans = []
    for number, (line, parsed) in enumerate(zip(lines, candidates), 1):
        if parsed is not None and parsed[0] in repeated:
            continue
        if spans and spans[-1]["end_line"] == number - 1:
            spans[-1]["content"] += line
            spans[-1]["end_line"] = number
        else:
            spans.append({"start_line": number, "end_line": number, "content": line})
    view = {
        "format": PROJECTION, "sampled_lines": len(lines), "omitted_lines": 0,
        "scope": "Counts and 1-based line ranges refer only to this saved sample. "
                 "First/last timestamps follow source order. Repeated probe access lines share one raw example; "
                 "individual occurrence timestamps remain in saved evidence. All other lines are verbatim.",
        "probe_groups": list(repeated.values()), "raw_spans": spans,
    }
    return view if encoded_bytes({"log_view": view}) < encoded_bytes({"content": content}) else None
