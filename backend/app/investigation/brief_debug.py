"""Small, read-only troubleshooting projection; never infer missing observations."""
import json
from backend.app.investigation.cards import saved_cards
from backend.app.investigation.diagnostics import debug_report, safe_text
from backend.app.tools.investigation import redact_output


def brief_debug_report(row, data):
    full = debug_report(row, data)
    cards = saved_cards(row.get("output_snapshot"), data)
    calls = list(data.get("calls", {}).values())
    models = [c for c in calls if c.get("kind") == "investigation_model"]
    refs = {r["resource_ref"]: r for r in full["available_resources"]}

    def request(r):
        r = r or {}
        resource = refs.get(r.get("resource_ref"), {})
        return {k: v for k, v in {
            "tool": r.get("tool"), "target": resource.get("name") or r.get("resource_ref"),
            "container": resource.get("container"), "previous": r.get("previous"),
            "tail_lines": r.get("tail_lines"),
        }.items() if v is not None}

    attempts = []
    for call in full["model_attempts"]:
        result, meta = call.get("result") or {}, call.get("metadata") or {}
        parsed = result.get("parsed") or {}
        decision = (parsed.get("decision") or {}) if isinstance(parsed, dict) else {}
        if not isinstance(decision, dict):
            decision = {}
        diagnosis = decision.get("diagnosis") or {}
        if not isinstance(diagnosis, dict):
            diagnosis = {}
        validation, provider = call.get("validation") or {}, result.get("diagnostics") or {}
        attempts.append({k: v for k, v in {
            "id": call.get("request_id"), "status": call.get("status"),
            "action": decision.get("action"), "reason": safe_text(decision.get("reason") or "", 180),
            "missing_fact": safe_text(decision.get("missing_fact") or "", 160),
            "diagnosis_category": diagnosis.get("fault_category"),
            "problem_domain": (diagnosis.get("assessment") or {}).get("problem_domain"),
            "diagnosis_facts": (call.get("validation_context") or {}).get("diagnosis_facts"),
            "root_cause": safe_text(diagnosis.get("root_cause") or "", 180),
            "requests": [request(r) for r in decision.get("requests", [])[:2]],
            "usage": call.get("usage"), "input_estimate": meta.get("input_estimate"),
            "context_version": meta.get("context_version"), "context_view": meta.get("context_view"),
            "error": result.get("error") or result.get("parse_error"),
            "validation": safe_text(validation.get("detail"), 400) if validation else None,
            "validation_stage": validation.get("stage"),
            "finish_reason": provider.get("finish_reason"),
            "parser_detail": safe_text(provider.get("parser_detail"), 400) if provider.get("parser_detail") else None,
            "selected_count": len(meta.get("selection") or []),
            "omitted_ids": (meta.get("omitted_evidence_ids") or [])[:30],
            "omitted_id_count": len(meta.get("omitted_evidence_ids") or []),
        }.items() if v is not None and v != [] and v != ""})

    evidence = []
    ordered_cards = sorted(cards["cards"], key=lambda c: (
        not (c.get("error") or c.get("parse_status") != "parsed"),
        not bool(c.get("source_ref", {}).get("request_id"))))
    for card in ordered_cards[:24]:
        identity, fields = card["identity"], card.get("fields") or {}
        item = {"id": card["evidence_id"], "type": identity["resource_type"],
                "target": identity["resource_name"], "parse": card["parse_status"],
                "coverage": card["coverage"]}
        for key, value in (("error", card.get("error")), ("parse_reason", card.get("parse_reason")),
                           ("request_id", card["source_ref"].get("request_id"))):
            if value is not None:
                item[key] = value
        if identity["resource_type"] == "PodLogs":
            logs = fields.get("logs") or {}
            item.update(previous=identity.get("previous"), sampled_lines=logs.get("scanned_lines"),
                        omitted_lines=logs.get("omitted_lines"), omitted_groups=logs.get("omitted_groups"))
            item["examples"] = [{"message": safe_text(g.get("message", ""), 160), "count": g.get("count")}
                                for g in logs.get("groups", [])[:2]]
        elif fields.get("events"):
            item["examples"] = [{"reason": safe_text(e.get("reason", ""), 80),
                                 "message": safe_text(e.get("message", ""), 160)}
                                for e in fields["events"][:2] if isinstance(e, dict)]
        evidence.append(item)

    failures = [{"request_id": key, "reason": value.get("reason"),
                 "input_estimate": value.get("input_estimate"),
                 "context_version": value.get("context_version"),
                 "omitted_id_count": len(value.get("omitted_ids", [])),
                 "omitted_ids": value.get("omitted_ids", [])[:30]}
                for key, value in list(data.get("context_assembly_failures", {}).items())[:5]]
    known_usage = [c.get("usage", {}).get("total_tokens") for c in models if isinstance(c.get("usage"), dict)]
    known_usage = [n for n in known_usage if type(n) is int and n >= 0]
    report = {
        "report_version": "brief-v1", "run": full["run"], "stop_reason": full["stop_reason"],
        "outcome": {k: ((row.get("output_snapshot") or {}).get("output") or {}).get(k)
                    for k in ("status", "diagnosis_source", "fallback_reason")},
        "budget": {"policy": data.get("policy"), "charged_or_reserved_tokens": data.get("tokens"),
                   "reported_model_tokens": sum(known_usage), "model_calls_missing_usage": len(models) - len(known_usage),
                   "exhausted": data.get("exhausted")},
        "model_attempts": attempts, "context_failures": failures,
        "tool_attempts": [{"id": c.get("request_id"), **request(c.get("request")),
                           "status": c.get("status"), **(c.get("result") or {})} for c in full["tool_attempts"]],
        "evidence": evidence,
        "omitted": {"evidence": cards.get("omitted_records", 0) + max(0, len(cards["cards"]) - 24),
                    "model_attempts": max(0, len(models) - len(attempts)),
                    "tool_attempts": max(0, sum(c.get("kind") == "tool" for c in calls) - len(full["tool_attempts"]))},
        "scope": "Saved records only; samples are partial. At most 24 cards (errors/tools first), 2 examples/card, 160 characters/message. Brief omits full decisions, bodies and selection digests. Missing data stays unknown; succeeded does not mean healthy.",
    }
    return json.loads(redact_output(report))
