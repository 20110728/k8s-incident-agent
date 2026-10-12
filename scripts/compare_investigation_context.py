"""Compare representation sizes from a saved v3.1 last-llm export; no model or database calls."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from backend.app.investigation.working_context import structured_block, LEGACY_VERSION, VERSION
from backend.app.investigation.context import encode


def compare(report):
    messages = (report.get("input") or {}).get("messages") or []
    user = next(m["content"] for m in messages if m.get("role") == "user")
    original = json.loads(user)
    if original.get("context_version") != LEGACY_VERSION:
        raise ValueError("Expected a saved v3.1 input for a same-evidence comparison")
    candidate = deepcopy(original)
    rows, logs = [], []
    for row in original["evidence"]:
        body = json.loads(row["excerpt"])
        # Reconstruct only the already redacted input view, not original DB evidence.
        source = {k: row[k] for k in ("evidence_id", "resource_type", "resource_name", "collected_at", "coverage", "error") if k in row}
        source.update(data=body, truncated=row.get("source_truncated", False))
        projected = structured_block(source)
        rows.append(projected)
        if row["resource_type"] == "PodLogs" and isinstance(body.get("content"), str):
            view = projected["data"].get("log_view")
            total = len(body["content"].splitlines())
            grouped = sum(g["count"] for g in view["probe_groups"]) if view else 0
            logs.append({"evidence_id": row["evidence_id"], "sampled_lines": total,
                         "grouped_probe_lines": grouped, "verbatim_lines": total - grouped,
                         "projection": projected["projection"], **projected["representation_bytes"]})
    candidate["evidence"] = rows
    old_body, new_body = encode(original["evidence"]), encode(rows)
    return {
        "incident_id": report.get("incident_id"), "run_id": report.get("run_id"),
        "comparison": f"{LEGACY_VERSION} -> {VERSION}",
        "old_evidence_utf8_bytes": len(old_body.encode()), "new_evidence_utf8_bytes": len(new_body.encode()),
        "original_user_characters": len(user), "candidate_user_characters": len(encode(candidate)),
        "evidence_ids_unchanged": [r["evidence_id"] for r in rows] == original["available_evidence_ids"],
        "logs": logs,
        "scope": "Representation-only comparison of the same saved input. Candidate retains old context metadata; "
                 "not a production prompt replay or tokenizer measurement. No model/tool/database calls; "
                 "no diagnosis quality claim. Source digests refer to reconstructed input views, not DB originals.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--last-llm", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compare(json.loads(args.last_llm.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
