# 服务配置加载与一致性校验：关联登记服务和 Deployment，生成可比对的配置摘要。
# 采集与执行前均重新读取配置；应用版本、镜像、资源身份不匹配时拒绝据此修复。

import hashlib
import json
import os
from pathlib import Path

from backend.app.service_profiles.models import ServiceProfile

DEFAULT_PROFILE_DIR = Path(__file__).resolve().parents[3] / "config" / "service-profiles"


class ProfileUnavailable(ValueError):
    pass


def profile_digest(profile: ServiceProfile) -> str:
    canonical = json.dumps(profile.model_dump(mode="json"), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def load_profile(namespace: str, service_name: str) -> ServiceProfile:
    # Read fresh at collection and before execution; no process-lifetime cache.
    root = Path(os.environ.get("INCIDENT_AGENT_SERVICE_PROFILE_DIR", DEFAULT_PROFILE_DIR))
    try:
        paths = sorted(root.glob("*.json"))
        if not paths:
            raise ValueError("profile directory is missing or empty")
        profiles = []
        for path in paths:
            if path.stat().st_size > 65536:
                raise ValueError("profile exceeds 64 KiB")
            profiles.append(ServiceProfile.model_validate(
                json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)))
        keys = [(p.namespace, p.service_name) for p in profiles]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate namespace/service registration")
        matches = [p for p in profiles if (p.namespace, p.service_name) == (namespace, service_name)]
        if len(matches) != 1:
            raise ValueError("service is not registered")
        return matches[0]
    except (OSError, ValueError) as exc:
        raise ProfileUnavailable(f"SERVICE_PROFILE_UNAVAILABLE: {exc}") from exc


def assess_profile(profile: ServiceProfile, deployment: dict) -> list[str]:
    reasons = []
    if (deployment.get("namespace"), deployment.get("name")) != (profile.namespace, profile.deployment_name):
        reasons.append("DEPLOYMENT_ASSOCIATION_MISMATCH")
    if not deployment.get("uid") or deployment.get("generation") is None:
        reasons.append("DEPLOYMENT_IDENTITY_MISSING")
    labels = deployment.get("template_labels") or {}
    if labels.get(profile.application.version_label) != profile.application.version:
        reasons.append("APPLICATION_VERSION_MISMATCH")
    containers = deployment.get("containers") or []
    images = {c.get("name"): c.get("image") for c in containers}
    # 这里比较镜像声明字符串，不等同于镜像签名或运行中镜像内容的可信证明。
    if images != profile.application.images or len(images) != len(containers):
        reasons.append("APPLICATION_IMAGES_MISMATCH")
    if not all(labels.get(k) == v for k, v in profile.expected_selector.items()):
        reasons.append("EXPECTED_SELECTOR_WORKLOAD_MISMATCH")
    return reasons


def make_snapshot(profile: ServiceProfile, deployment: dict) -> dict:
    reasons = assess_profile(profile, deployment)
    return {
        "status": "matched" if not reasons else "mismatch",
        "reasons": reasons,
        "profile": profile.model_dump(mode="json"),
        "digest": profile_digest(profile),
        "deployment_uid": deployment.get("uid"),
        "deployment_generation": deployment.get("generation"),
    }


def collect_profile(clients, bundle: dict) -> dict:
    from backend.app.tools.workload_tools import get_deployment_config

    try:
        profile = load_profile(bundle["namespace"], bundle["service_name"])
    except ProfileUnavailable as exc:
        return {"status": "unavailable", "reasons": [str(exc)]}
    try:
        # Explicit association still works when the Service selects the wrong pods.
        deployment = get_deployment_config(
            clients, profile.namespace, profile.deployment_name).model_dump(mode="json")
        bundle.setdefault("deployments", {})[profile.deployment_name] = deployment
        return make_snapshot(profile, deployment)
    except Exception as exc:
        return {"status": "unavailable", "reasons": ["REGISTERED_DEPLOYMENT_UNAVAILABLE"],
                "error_type": type(exc).__name__, "profile": profile.model_dump(mode="json"),
                "digest": profile_digest(profile)}


def matched_profile(state: dict) -> ServiceProfile:
    snapshot = state.get("service_profile") or {}
    if snapshot.get("status") != "matched":
        raise ProfileUnavailable("SERVICE_PROFILE_NOT_MATCHED")
    try:
        profile = ServiceProfile.model_validate(snapshot["profile"])
    except (KeyError, ValueError) as exc:
        raise ProfileUnavailable("SERVICE_PROFILE_INVALID") from exc
    request = state.get("request") or {}
    if (profile.namespace, profile.service_name) != (request.get("namespace"), request.get("service_name")):
        raise ProfileUnavailable("SERVICE_PROFILE_TARGET_MISMATCH")
    if snapshot.get("digest") != profile_digest(profile):
        raise ProfileUnavailable("SERVICE_PROFILE_DIGEST_MISMATCH")
    deployments = [e["data"] for e in state.get("evidence", [])
                   if e.get("resource_type") == "Deployment"
                   and e.get("resource_name") == profile.deployment_name]
    if len(deployments) != 1 or assess_profile(profile, deployments[0]):
        raise ProfileUnavailable("SERVICE_PROFILE_EVIDENCE_MISMATCH")
    if (snapshot.get("deployment_uid"), snapshot.get("deployment_generation")) != (
            deployments[0].get("uid"), deployments[0].get("generation")):
        raise ProfileUnavailable("SERVICE_PROFILE_OBSERVATION_MISMATCH")
    return profile


def validate_profile_action(profile: ServiceProfile, plan) -> None:
    p = plan.parameters
    if p.namespace != profile.namespace:
        raise ProfileUnavailable("SERVICE_PROFILE_TARGET_MISMATCH")
    if plan.action == "patch_service_selector":
        pairs = p.proposed_selector
        proposed = {pair.key: pair.value for pair in pairs}
        if (p.resource_kind != "Service" or p.resource_name != profile.service_name
                or len(proposed) != len(pairs) or proposed != profile.expected_selector):
            raise ProfileUnavailable("SELECTOR_NOT_REGISTERED")
    elif plan.action == "patch_readiness_probe":
        if (p.resource_kind != "Deployment" or p.resource_name != profile.deployment_name
                or p.container_name != profile.container_name
                or p.proposed_probe_path != profile.readiness_probe.path
                or p.proposed_probe_port != profile.readiness_probe.port):
            raise ProfileUnavailable("READINESS_NOT_REGISTERED")


def revalidate_live_profile(state: dict, clients, plan) -> dict:
    from backend.app.tools.workload_tools import get_deployment_config

    profile = matched_profile(state)
    current = load_profile(profile.namespace, profile.service_name)
    if profile_digest(current) != state["service_profile"]["digest"]:
        raise ProfileUnavailable("SERVICE_PROFILE_CHANGED_AFTER_APPROVAL")
    validate_profile_action(current, plan)
    live = get_deployment_config(clients, current.namespace, current.deployment_name).model_dump(mode="json")
    if assess_profile(current, live):
        raise ProfileUnavailable("APPLICATION_CHANGED_AFTER_APPROVAL")
    snapshot = state["service_profile"]
    # UID 检测资源重建，generation 检测审批后的规格变更。
    if (live.get("uid"), live.get("generation")) != (
            snapshot["deployment_uid"], snapshot["deployment_generation"]):
        raise ProfileUnavailable("DEPLOYMENT_CHANGED_AFTER_APPROVAL")
    if not live.get("resource_version"):
        raise ProfileUnavailable("DEPLOYMENT_RESOURCE_VERSION_MISSING")
    return live
