"""Program-built context views. User claims/history never become cluster evidence."""
from copy import deepcopy
import json

from backend.app.investigation.cards import evidence_card, VERSION as CARD_VERSION
from backend.app.investigation.records import digest
from backend.app.tools.investigation import redact_output

VERSION = "investigation-context-v2"


def text(value, limit):
    return json.loads(redact_output(str(value or "")))[:limit]


def human_context(dialogue):
    if dialogue is None:
        return None
    answers = []
    seen = set()
    for answer in dialogue.get("answers", []):
        key = answer.get("message_id")
        if key and key in seen:
            continue
        seen.add(key)
        fields = ("question_id", "version", "message_id", "slot", "accepted_at", "skip", "changed_resource_refs")
        value = {k: deepcopy(answer[k]) for k in fields if k in answer}
        raw = json.loads(redact_output(str(answer.get("text") or "")))
        marker = "\n...[omitted]...\n"
        excerpt = raw if len(raw) <= 600 else raw[:400] + marker + raw[-(200 - len(marker)):]
        value.update(text=excerpt, text_truncated=len(raw) > 600, source="user_supplied_unverified")
        answers.append(value)
    return {"answers": answers, "asked_slots": list(dict.fromkeys(dialogue.get("asked_slots", []))),
            "freshness_seconds": dialogue.get("freshness_seconds", {})}


def historical_context(state, human):
    source = state.get("round_context") or {}
    if not source:
        return None
    answered = {a.get("message_id") for a in (human or {}).get("answers", []) if a.get("message_id")}
    messages = []
    seen = set(answered)
    for message in source.get("messages", []):
        mid = message.get("message_id")
        if mid and mid in seen:
            continue
        seen.add(mid)
        messages.append({"message_id": mid, "content": text(message.get("content"), 200), "excerpt": True})
    previous = source.get("previous_result") or {}
    return {"usage": "Historical background and hypotheses; never current evidence or approval.",
            "messages": messages[-5:], "omitted_messages": len(source.get("messages", [])) - len(messages[-5:]),
            "previous_result": {k: text(previous.get(k), 400) for k in ("phase", "diagnosis_excerpt", "verification_excerpt")}}


def working_state(state, facts, human):
    """Small reference-only projection; bodies live once in prompt.evidence."""
    rows = state.get("evidence", [])
    by_name = {}
    for row in rows:
        data = row.get("data") or {}
        if isinstance(data, dict) and data.get("uid"):
            key = (row.get("resource_type"), data.get("namespace"), row.get("resource_name"))
            by_name.setdefault(key, {}).setdefault(data["uid"], []).append(row["evidence_id"])
    conflicts = [sorted(eid for ids in values.values() for eid in ids)
                 for _, values in sorted(by_name.items(), key=lambda item: str(item[0])) if len(values) > 1]
    answers = (human or {}).get("answers", [])
    answered = {a.get("slot") for a in answers}
    return {"version": VERSION, "facts_ref": "policy_facts", "evidence_ref": "evidence",
            "snapshot_status": "requires_new_baseline_for_health_after_human_wait" if answers else "sampled_scope_only",
            "failed_evidence_ids": [r["evidence_id"] for r in rows if r.get("error") or r.get("coverage") == "unknown"
                                    or (isinstance(r.get("data"), dict) and r["data"].get("error_code"))],
            "identity_conflicts": conflicts,
            "unanswered_slots": [s for s in (human or {}).get("asked_slots", []) if s not in answered],
            "source_digest": digest({"evidence": rows, "policy_facts": facts, "human": human}),
            "historical_evidence_ids": state.get("historical_evidence_ids", [])}


def unique_evidence(rows):
    """Dedup representations only. Conflicting content with the same ID is an error."""
    seen, result = {}, []
    for row in rows:
        key, fingerprint = row["evidence_id"], digest(row)
        if key in seen:
            if seen[key] != fingerprint:
                raise ValueError("CONFLICTING_EVIDENCE_ID")
            continue
        seen[key] = fingerprint
        result.append(row)
    return result


def card_block(item, *, expanded=False):
    """A single valid JSON representation, never raw text plus repeated fields.

    This is a bounded view of the C1 card, not the complete card/export. Omission
    markers survive each reduction, and omitted source bodies are not citable.
    """
    card = evidence_card(item)
    omissions = []
    def shrink(value, path="fields"):
        if isinstance(value, str):
            cap = 300 if expanded else 140
            if len(value) > cap:
                omissions.append(path)
            return value[:cap]
        if isinstance(value, list):
            cap = 8 if expanded else 2
            if len(value) > cap:
                omissions.append(path)
            return [shrink(v, f"{path}[{i}]") for i, v in enumerate(value[:cap])]
        if isinstance(value, dict):
            return {k: shrink(v, f"{path}.{k}") for k, v in value.items()}
        return value
    if card["parse_status"] == "parsed":
        content = shrink(card["fields"])
    else:
        content = {"unparsed_excerpt": shrink(card["excerpt"]), "reason": card["parse_reason"]}
    if omissions:
        content["view_omitted_paths"] = omissions
    identity = card["identity"]
    block = {"evidence_id": card["evidence_id"], "resource_type": identity["resource_type"],
             "resource_name": identity["resource_name"], "collected_at": card["collected_at"],
             "error": card["error"], "coverage": card["coverage"], "parse_status": card["parse_status"],
             "excerpt": json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
             "excerpt_truncated": True, "projection": CARD_VERSION}
    # Identity is metadata, not a second copy of the sampled body.
    for key in ("uid", "container", "previous"):
        if identity[key] is not None:
            block[key] = identity[key]
    block["source_truncated"] = card["source_truncated"]
    if card["projection_omissions"]:
        block["projection_omitted"] = True
    return block
