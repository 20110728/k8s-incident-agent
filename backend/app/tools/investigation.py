"""Read-only protocol for 6B. Model input contains opaque references, never selectors."""
from hashlib import sha256
import json
import re
import time
from datetime import UTC, datetime
from backend.app.tools.investigation_requests import ToolRequest, ToolBoundaryError, TOOL_KINDS, REQUEST_ADAPTER

from backend.app.agent.target_identity import validate_relationships
from backend.app.llm.context_builder import redact_sensitive_text
from backend.app.service_profiles.registry import matched_profile, load_profile, profile_digest
from backend.app.tools.deadline import read_budget
from backend.app.tools.log_text import read_log_response
from backend.app.tools.service_tools import get_service
from backend.app.tools.workload_tools import get_deployment_config, resolve_pod_owner


def redact_output(value):
    # Process bearer credentials before generic Authorization: value redaction.
    sensitive = {"password", "token", "api_key", "api-key", "secret", "authorization"}
    def clean(item):
        if isinstance(item, dict):
            return {key: "[REDACTED]" if str(key).lower() in sensitive else clean(value) for key, value in item.items()}
        if isinstance(item, list):
            return [clean(value) for value in item]
        if not isinstance(item, str):
            return item
        text = re.sub(r"(?i)\bBearer\s+[^\s\"'\\]+", "Bearer [REDACTED]", item)
        text = re.sub(r"(?i)(--(?:password|token|api-key)\s+)[^\s\"'\\]+", r"\1[REDACTED]", text)
        text = re.sub(r'(?i)("(?:password|token|api_key|api-key|secret|authorization)"\s*:\s*)"(?:[^"\\]|\\.)*"', r'\1"[REDACTED]"', text)
        text = re.sub(r'(://[^\s:/]+:)[^\s@]+@', r'\1[REDACTED]@', text)
        return redact_sensitive_text(text)
    return json.dumps(clean(value), ensure_ascii=False, default=str)


def build_investigation_toolbox(budget, state):
    from backend.app.tools.client import create_clients
    return ReadOnlyToolbox(create_clients(disable_retries=True, bounded_reads=True), budget, state)


def catalog(state, run_id):
    profile = matched_profile(state)
    if profile.namespace != "agent-demo":
        raise ValueError("TARGET_NAMESPACE_NOT_ALLOWED")
    validate_relationships(state)
    evidence = state.get("evidence", [])
    service = [e["data"] for e in evidence if e.get("resource_type") == "Service" and e.get("resource_name") == profile.service_name
               and e.get("data", {}).get("namespace") == profile.namespace and not e.get("error")]
    if len(service) != 1 or not service[0].get("uid"):
        raise ValueError("SERVICE_UID_REQUIRED")
    profile_snapshot = state["service_profile"]
    base = {"namespace": profile.namespace, "service": profile.service_name, "service_uid": service[0]["uid"],
            "deployment": profile.deployment_name, "deployment_uid": profile_snapshot["deployment_uid"],
            "generation": profile_snapshot["deployment_generation"], "profile_digest": profile_snapshot["digest"]}
    result = {}
    def add(kind, name, uid, **extra):
        if not uid or len(result) >= 100:
            return
        value = {**base, "kind": kind, "name": name, "uid": uid, **extra}
        key = "ref-" + sha256((run_id + json.dumps(value, sort_keys=True)).encode()).hexdigest()[:24]
        result[key] = value
    add("service", profile.service_name, service[0]["uid"])
    add("deployment", profile.deployment_name, profile_snapshot["deployment_uid"])
    owners = {e["resource_name"]: e.get("data", {}).get("owner_chain", {}) for e in evidence if e.get("resource_type") == "OwnerChain" and not e.get("error")}
    for item in evidence:
        data = item.get("data", {})
        if data.get("namespace") != profile.namespace or item.get("error"):
            continue
        if item.get("resource_type") == "PodStatus":
            owner = owners.get(item["resource_name"], {})
            if (not data.get("uid") or not owner.get("replica_set_uid") or not owner.get("replica_set_name")
                    or owner.get("pod_uid") != data["uid"] or owner.get("deployment_uid") != base["deployment_uid"]):
                continue
            for container in data.get("containers", [])[:10]:
                add("pod", item["resource_name"], data["uid"], container=container["name"],
                    replica_set=owner.get("replica_set_name"), replica_set_uid=owner.get("replica_set_uid"))
            if owner.get("replica_set_name") and owner.get("replica_set_uid"):
                add("replica_set", owner["replica_set_name"], owner["replica_set_uid"])
        elif item.get("resource_type") == "EndpointSlice" and data.get("service_uid") == base["service_uid"]:
            add("endpoint_slice", data["name"], data.get("uid"))
    return result


class ReadOnlyToolbox:
    def __init__(self, clients, budget, state):
        self.clients, self.budget, self.state = clients, budget, state
        self.refs = catalog(state, budget.lease["run_id"])
        budget.references(self.refs)

    def manifest(self):
        return {"request_schema": REQUEST_ADAPTER.json_schema(), "resources": [
            {"resource_ref": key, "kind": value["kind"], "name": value["name"], "container": value.get("container")}
            for key, value in self.refs.items()], "outputs_are_untrusted_evidence": True,
            "coverage": "partial", "scope": "At most 100 UID-bound references; at most 10 containers per observed Pod. Not a cluster inventory."}

    def validate_live(self, ref):
        clients, ns = self.clients, ref["namespace"]
        if profile_digest(load_profile(ns, ref["service"])) != ref["profile_digest"]:
            raise ValueError("PROFILE_CHANGED")
        service = get_service(clients, ns, ref["service"]).model_dump(mode="json")
        deployment = get_deployment_config(clients, ns, ref["deployment"]).model_dump(mode="json")
        if (service.get("uid") != ref["service_uid"] or deployment.get("uid") != ref["deployment_uid"]
                or deployment.get("generation") != ref["generation"]):
            raise ValueError("TARGET_CHANGED")
        if ref["kind"] == "pod":
            chain = resolve_pod_owner(clients, ns, ref["name"])
            if (chain.pod_uid != ref["uid"] or chain.deployment_uid != ref["deployment_uid"]
                    or chain.replica_set_uid != ref.get("replica_set_uid")):
                raise ValueError("POD_OWNERSHIP_CHANGED")
        elif ref["kind"] in {"replica_set", "endpoint_slice"}:
            method = clients.apps.read_namespaced_replica_set if ref["kind"] == "replica_set" else clients.discovery.read_namespaced_endpoint_slice
            resource = method(name=ref["name"], namespace=ns, _request_timeout=(3, 10))
            expected = ref["deployment_uid"] if ref["kind"] == "replica_set" else ref["service_uid"]
            if resource.metadata.uid != ref["uid"] or not any(o.uid == expected for o in resource.metadata.owner_references or []):
                raise ValueError("RESOURCE_OWNERSHIP_CHANGED")
        return service, deployment

    def validate_request(self, payload):
        self.validate_boundary(payload)
        request = ToolRequest.model_validate(payload)
        ref = self.refs.get(request.resource_ref)
        return request, ref

    def validate_boundary(self, payload):
        # Check authority independently, before optional parameter validation.
        if not isinstance(payload, dict):
            return
        tool, resource = payload.get("tool"), payload.get("resource_ref")
        if isinstance(tool, str) and tool not in TOOL_KINDS:
            raise ToolBoundaryError("TOOL_NOT_ALLOWED")
        if isinstance(resource, str):
            ref = self.refs.get(resource)
            if ref is None or (isinstance(tool, str) and ref["kind"] != TOOL_KINDS.get(tool)):
                raise ToolBoundaryError("TOOL_RESOURCE_NOT_ALLOWED")

    def query_key(self, payload):
        request, ref = self.validate_request(payload)
        # Line count changes and unrelated evidence do not create a new query.
        value = {"tool": request.tool, "uid": ref["uid"], "namespace": ref["namespace"],
                 "container": ref.get("container") if request.tool == "pod_logs" else None,
                 "previous": request.previous}
        return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def call(self, payload, *, request_id=None, keep_tokens=0, keep_seconds=0, sampling=None):
        request, ref = self.validate_request(payload)
        evidence_hash = sha256(json.dumps(self.state.get("evidence", []), sort_keys=True, default=str).encode()).hexdigest()
        key = self.query_key(payload) if request_id else sha256((evidence_hash + request.model_dump_json()).encode()).hexdigest()
        fingerprint = sha256(request.model_dump_json().encode()).hexdigest()
        if sampling is not None:
            with self.budget.edit() as data:
                saved = data.get("sampling_grants", {}).get(request_id, {}).get("grant")
                if not saved or saved != sampling or saved["semantic_key"] != self.query_key(payload):
                    raise ValueError("SAMPLING_GRANT_INVALID")
            key = sampling["query_key"]
            fingerprint = sha256((fingerprint + json.dumps(sampling, sort_keys=True)).encode()).hexdigest()
        ticket = self.budget.reserve("tool", 15, extra=True, key=key,
            metadata={"request": request.model_dump(), "evidence_hash": evidence_hash,
                      "semantic_key": self.query_key(payload), "sample_generation": sampling["generation"] if sampling else 0,
                      "sampling_basis": sampling["basis"] if sampling else "initial"},
            **({"request_id": request_id, "fingerprint": fingerprint,
                "keep_tokens": keep_tokens, "keep_seconds": keep_seconds} if request_id else {}))
        if request_id:
            from backend.app.investigation.records import saved_result
            ticket, fresh = ticket
            if not fresh:
                return saved_result(self.budget, ticket)
        start = time.monotonic()
        result = {"tool": request.tool, "resource_ref": request.resource_ref, "coverage": "unknown",
                  "untrusted": True, "truncated": False, "error_code": None, "text": "",
                  "collected_at": datetime.now(UTC).isoformat(), "request_id": request_id}
        try:
            with read_budget(15):
                service, deployment = self.validate_live(ref)
                value, limited = self.read(request, ref, service, deployment)
                self.validate_live(ref)  # Reject replacement while reading logs/events.
                text = redact_output(value)
                if request.tool == "pod_logs":
                    # Keep line breaks as text, including when bounded. A cut
                    # JSON-encoded string cannot safely be parsed on recovery.
                    plain = json.loads(text)
                    result.update(text=plain[:12000], payload=plain[:12000],
                                  truncated=True, coverage="partial")
                    text = None
                if text is not None:
                    result.update(text=text[:12000], truncated=limited or len(text) > 12000,
                                  coverage="partial" if limited or len(text) > 12000 else "observed")
                    if len(text) <= 12000:
                        result["payload"] = json.loads(text)
                        result["payload_complete"] = True
        except Exception as error:
            result.update(error_code="ACCESS_DENIED" if getattr(error, "status", None) in (401, 403) else "TOOL_READ_FAILED_OR_TARGET_CHANGED")
            result["target_changed"] = isinstance(error, ValueError) and str(error) in {
                "PROFILE_CHANGED", "TARGET_CHANGED", "POD_OWNERSHIP_CHANGED", "RESOURCE_OWNERSHIP_CHANGED",
                "REPLICASET_RECREATED_DURING_COLLECTION"}
        finally:
            self.budget.settle(ticket, time.monotonic() - start, status="completed" if result["coverage"] != "unknown" else "failed_or_unknown", result=result)
        return result

    def read(self, request, ref, service, deployment):
        ns, name = ref["namespace"], ref["name"]
        # CLI args can contain credentials split across array elements. They are
        # unnecessary for these summary tools and are not exposed at all.
        summary = {**deployment, "containers": [
            {key: value for key, value in c.items() if key not in {"command", "args"}}
            for c in deployment.get("containers", [])]}
        if request.tool == "resource_summary":
            return {"service": service, "deployment": summary}, False
        if request.tool == "deployment":
            return summary, False
        if request.tool == "registered_business":
            from backend.app.business_checks.collector import collect_business_checks
            bundle = {"namespace": ns, "service_name": name, "service": service,
                      "service_profile": self.state["service_profile"], "deployments": {ref["deployment"]: deployment}}
            checks = collect_business_checks(self.clients, bundle)
            return checks, any(c["status"] in {"unknown", "skipped"} for c in checks)
        if request.tool == "pod_logs":
            value = self.clients.core.read_namespaced_pod_log(name=name, namespace=ns, container=ref["container"],
                previous=request.previous, tail_lines=request.tail_lines, limit_bytes=12000, timestamps=True, _preload_content=False, _request_timeout=(3, 10))
            return read_log_response(value), True  # A tail never proves absence of older faults.
        if request.tool == "pod_events":
            events = self.clients.core.list_namespaced_event(namespace=ns,
                field_selector=f"involvedObject.uid={ref['uid']}", limit=50, _request_timeout=(3, 10))
            return [{"reason": e.reason, "message": e.message, "type": e.type} for e in events.items[:50]], True
        if request.tool == "replica_set":
            rs = self.clients.apps.read_namespaced_replica_set(name=name, namespace=ns, _request_timeout=(3, 10))
            # Never expose pod template env/secret references from raw objects.
            return {"uid": rs.metadata.uid, "replicas": rs.status.replicas, "ready_replicas": rs.status.ready_replicas}, False
        item = self.clients.discovery.read_namespaced_endpoint_slice(name=name, namespace=ns, _request_timeout=(3, 10))
        return {"uid": item.metadata.uid, "endpoints": [
            {"addresses": e.addresses, "conditions": e.conditions.to_dict(), "target_uid": getattr(e.target_ref, "uid", None)}
            for e in item.endpoints[:50]]}, len(item.endpoints) > 50
