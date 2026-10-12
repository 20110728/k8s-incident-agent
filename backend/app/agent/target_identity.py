"""Bind new plans to observed identities; retain legacy read compatibility."""
import hashlib
import json


def plan_payload(plan):
    value = plan.model_dump(mode="json")
    if value.get("target_uid") is None:
        value.pop("target_uid", None)  # Preserve pre-3A approval fingerprints.
    return value


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def observed_target_uid(state, plan):
    p = plan.parameters
    matches = [e.get("data", {}) for e in state.get("evidence", [])
               if e.get("resource_type") == p.resource_kind and e.get("resource_name") == p.resource_name
               and e.get("data", {}).get("namespace") == p.namespace and not e.get("error")]
    if (len(matches) != 1 or not isinstance(matches[0].get("uid"), str)
            or not 1 <= len(matches[0]["uid"]) <= 128):
        raise ValueError("TARGET_IDENTITY_MISSING_OR_AMBIGUOUS")
    return matches[0]["uid"]


def validate_relationships(state):
    """Reject contradictory UID links; missing legacy links are not invented."""
    evidence = state.get("evidence", [])
    resources = {}
    for e in evidence:
        key, data = (e.get("resource_type"), e.get("resource_name")), e.get("data", {})
        if key in resources and (resources[key].get("uid"), resources[key].get("namespace")) != (
                data.get("uid"), data.get("namespace")):
            raise ValueError("RESOURCE_IDENTITY_AMBIGUOUS")
        resources[key] = data
    for e in evidence:
        data = e.get("data", {})
        if e.get("resource_type") == "OwnerChain":
            chain = data.get("owner_chain", {})
            pod = resources.get(("PodStatus", e.get("resource_name")), {})
            deployment = resources.get(("Deployment", chain.get("deployment_name")), {})
            if pod.get("uid") and chain.get("deployment_name") and (
                    not chain.get("pod_uid") or not chain.get("deployment_uid")):
                raise ValueError("OWNER_IDENTITY_INCOMPLETE")
            for saved, current in ((chain.get("pod_uid"), pod.get("uid")),
                                   (chain.get("deployment_uid"), deployment.get("uid"))):
                if saved and current and saved != current:
                    raise ValueError("OWNER_IDENTITY_CHANGED_DURING_COLLECTION")
        if e.get("resource_type") == "EndpointSlice":
            service = resources.get(("Service", data.get("service_name")), {})
            if data.get("uid") and not data.get("service_uid"):
                raise ValueError("ENDPOINT_SERVICE_IDENTITY_MISSING")
            if data.get("service_uid") and service.get("uid") and data["service_uid"] != service["uid"]:
                raise ValueError("ENDPOINT_SERVICE_IDENTITY_MISMATCH")
            for endpoint in data.get("endpoints", []):
                if data.get("uid") and endpoint.get("target_kind") == "Pod" and not endpoint.get("target_uid"):
                    raise ValueError("ENDPOINT_POD_IDENTITY_MISSING")
                pod = resources.get(("PodStatus", endpoint.get("target_name")), {})
                if endpoint.get("target_kind") == "Pod" and pod:
                    if (endpoint.get("target_uid") and pod.get("uid") and endpoint["target_uid"] != pod["uid"]
                            or endpoint.get("target_namespace") and endpoint["target_namespace"] != pod.get("namespace")):
                        raise ValueError("ENDPOINT_POD_IDENTITY_MISMATCH")


def bind_target(plan, state):
    uid = observed_target_uid(state, plan)
    if plan.target_uid is not None and plan.target_uid != uid:
        raise ValueError("PLAN_TARGET_IDENTITY_MISMATCH")
    validate_relationships(state)
    return plan.model_copy(update={"target_uid": uid})
