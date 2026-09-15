"""处置后验证：先等待资源恢复，再独立采集登记业务检查。

保留原诊断和审批证据；新观察单独持久化。未知不等于恢复成功，
验证失败也不会触发自动回滚或第二次写操作。
"""

from datetime import UTC, datetime
from uuid import uuid4

from backend.app.agent.collector_adapter import normalize_evidence
from backend.app.agent.diagnosis_policy import diagnostic_facts
from backend.app.agent.schemas import RecoveryVerificationResult, VerificationCheck
from backend.app.service_profiles.registry import ProfileUnavailable, matched_profile

UNVERIFIED_SCOPE = [
    "集群外入口流量",
    "未登记业务接口",
    "所有副本逐一业务验证",
    "本次采样之后的持续可用性",
]


class BusinessRecoveryVerifier:
    def __init__(self, resource_verifier, collector):
        self.resource_verifier = resource_verifier
        self.collector = collector

    def verify(self, state) -> RecoveryVerificationResult:
        # The first verifier polls resources. The collector below takes one fresh
        # resource/business sample; attempts remains the resource-poll count.
        resource = self.resource_verifier.verify(state)
        data = resource.model_dump(mode="json")
        data.update(
            verification_scope="resources_and_registered_business",
            resource_verification_status=resource.status,
            resource_status="unknown",
            business_status="skipped",
            unverified_scope=list(UNVERIFIED_SCOPE),
            post_repair_evidence=[],
            post_repair_profile=None,
            post_repair_collection_errors=[],
        )
        # 资源验证未成功时不继续检查，也不能把旧业务结果当作恢复依据。
        if resource.status != "succeeded":
            data["unverified_scope"].append("处置后的登记业务检查（资源验证未通过）")
            return RecoveryVerificationResult.model_validate(data)

        try:
            request = state["request"]
            bundle = self.collector.collect(
                request["namespace"], request["service_name"]
            )
            # 单独生成观察编号；不覆盖用于诊断、计划和审批的 evidence。
            evidence = normalize_evidence(incident_id=str(uuid4()), bundle=bundle)
            snapshot = bundle.get("service_profile") or {}
            data.update(
                post_repair_evidence=evidence,
                post_repair_profile=snapshot,
                post_repair_collection_errors=bundle.get("errors", []),
            )
            original = state.get("service_profile") or {}
            # Unlike manual rechecks, this path evaluates a specific Agent action
            # and conservatively rejects changes it cannot bind to that action.
            action = state["action_result"]
            if hasattr(action, "model_dump"):
                action = action.model_dump(mode="json")
            # 本次 readiness 写操作通常只使 generation 增加一次。
            # 额外规格变更、同名重建或配置登记变化均拒绝归因于本次修复。
            generation = original.get("deployment_generation")
            expected_generation = generation
            if action.get("action") == "patch_readiness_probe" and action.get(
                "applied_patch"
            ):
                expected_generation = (
                    generation + 1 if type(generation) is int else None
                )
            if (
                snapshot.get("status") != "matched"
                or not original.get("digest")
                or not original.get("deployment_uid")
                or snapshot.get("digest") != original["digest"]
                or snapshot.get("deployment_uid") != original["deployment_uid"]
                or type(expected_generation) is not int
                or snapshot.get("deployment_generation") != expected_generation
            ):
                raise ValueError("RECOVERY_TARGET_CHANGED_OR_UNBOUND")
            fresh_state = {
                "request": request,
                "service_profile": snapshot,
                "evidence": evidence,
            }
            resource_view, terminating_pods = recovery_resource_view(fresh_state)
            facts = diagnostic_facts(resource_view)

            if terminating_pods:
                data["unverified_scope"].append(
                    "退出中 Pod 的最终删除与清理：" + ", ".join(terminating_pods)
                )
                data["checks"].append(
                    VerificationCheck(
                        name="terminating_pods_classified",
                        passed=True,
                        observed=terminating_pods,
                        expected=(
                            "matching endpoints: "
                            "terminating=true, ready=false, serving=false"
                        ),
                        message=(
                            "已识别退出中且不服务的 Pod；"
                            "原始证据保留，最终清理未验证。"
                        ),
                    ).model_dump(mode="json")
                )
            data["resource_status"] = facts["resource_status"]
            data["business_status"] = facts["business_status"]
            if facts["expected_replicas"] is not None:
                data["checks"].append(
                    VerificationCheck(
                        name="registered_replica_count",
                        passed=facts["replica_count_matches"] is True,
                        observed=facts["observed_desired_replicas"],
                        expected=facts["expected_replicas"],
                        message="现场期望副本数须符合登记契约；本工具不自动扩缩容。",
                    ).model_dump(mode="json")
                )
            data["checks"].append(
                VerificationCheck(
                    name="registered_readiness_configuration",
                    passed=facts["readiness_configuration_status"] == "matched",
                    observed=facts["readiness_configuration_status"],
                    expected="matched",
                    message="核对登记 HTTP 探针的 path、port、scheme；证据不足不视为一致。",
                ).model_dump(mode="json")
            )
            # 资源等待成功与业务采集之间可能再次出现故障；重新检查当前事实。
            resource_ok = (
                facts["resource_status"] == "ready"
                and facts["readiness_configuration_status"] == "matched"
                and not facts["selector_drift"]
                and not facts["readiness_drift"]
                and not facts["current_runtime_faults"]
            )
            business_ok = facts["business_status"] == "passed"
            data["checks"].extend(
                [
                    VerificationCheck(
                        name="post_repair_resources",
                        passed=resource_ok,
                        observed=facts["resource_status"],
                        expected="ready without current drift/fault",
                        message="处置后重新采集的登记资源状态。",
                    ).model_dump(mode="json"),
                    VerificationCheck(
                        name="registered_business_checks",
                        passed=business_ok,
                        observed=facts["business_status"],
                        expected="passed",
                        message="仅评价本次登记 Service 接口检查，不覆盖全部业务。",
                    ).model_dump(mode="json"),
                ]
            )
            success = resource_ok and business_ok
            data["status"] = "succeeded" if success else "failed"
            data["error_code"] = (
                None
                if success
                else (
                    "POST_REPAIR_RESOURCES_NOT_READY"
                    if not resource_ok
                    else (
                        "POST_REPAIR_BUSINESS_FAILED"
                        if facts["business_status"] == "failed"
                        else "POST_REPAIR_BUSINESS_UNKNOWN"
                    )
                )
            )
            data["message"] = (
                "资源复查及登记业务检查通过；仅限本次采样范围。"
                if success
                else "未确认恢复：请分别查看资源复查和登记业务检查结果。"
            )
            data["error_message"] = None if success else data["message"]
        except Exception as error:
            # 包括检查器异常、资源变化或无法绑定目标；失败关闭但保留已采集证据。
            data.update(
                status="failed",
                resource_status="unknown",
                business_status="unknown",
                error_code="POST_REPAIR_OBSERVATION_INVALID",
                error_message=type(error).__name__ + ": " + str(error)[:300],
                message="无法取得与本次处置目标一致的有效恢复观察；未确认业务恢复。",
            )
        data["finished_at"] = datetime.now(UTC).isoformat()
        return RecoveryVerificationResult.model_validate(data)


def recovery_resource_view(state):
    """处置后复查：排除明确正在退出且不服务的 Pod，保留原始证据。"""
    request = state["request"]
    namespace = request["namespace"]
    service_name = request["service_name"]
    evidence = state.get("evidence", [])
    try:
        profile = matched_profile(state)
    except ProfileUnavailable:
        # Unbound evidence must not authorize a lifecycle exclusion.
        return dict(state), []
    owned = {
        item.get("resource_name")
        for item in evidence
        if item.get("resource_type") == "OwnerChain"
        and not item.get("error")
        and item.get("data", {}).get("owner_chain", {}).get("namespace") == namespace
        and item["data"]["owner_chain"].get("deployment_name")
        == profile.deployment_name
    }

    endpoint_records = {}
    for item in evidence:
        data = item.get("data", {})
        if (
            item.get("resource_type") == "EndpointSlice"
            and not item.get("error")
            and data.get("namespace") == namespace
            and data.get("service_name") == service_name
        ):
            for endpoint in data.get("endpoints", []):
                name = endpoint.get("target_name")
                if name:
                    endpoint_records.setdefault(name, []).append(endpoint)

    excluded = set()
    for item in evidence:
        data = item.get("data", {})
        name = item.get("resource_name")
        if (
            item.get("resource_type") != "PodStatus"
            or item.get("error")
            or data.get("namespace") != namespace
            or name not in owned
            or data.get("ready") is not False
        ):
            continue

        matching_statuses = [
            other
            for other in evidence
            if other.get("resource_type") == "PodStatus"
            and other.get("resource_name") == name
            and other.get("data", {}).get("namespace") == namespace
        ]
        if len(matching_statuses) != 1:
            continue

        records = endpoint_records.get(name, [])
        pod_ip = data.get("pod_ip")

        # 所有同名端点记录都必须与 Pod IP 匹配，且明确处于退出状态。
        # 仅仅 Ready=False、缺少端点或退出状态未知，都不能排除。
        if (
            pod_ip
            and records
            and all(
                endpoint.get("target_kind") == "Pod"
                and pod_ip in endpoint.get("addresses", [])
                and endpoint.get("terminating") is True
                and endpoint.get("ready") is False
                and endpoint.get("serving") is False
                for endpoint in records
            )
        ):
            excluded.add(name)

    # 创建判定视图，不修改保存给用户和审计的 post_repair_evidence。
    view = dict(state)
    view["evidence"] = [
        item
        for item in evidence
        if not (
            item.get("resource_type") == "PodStatus"
            and item.get("resource_name") in excluded
            and item.get("data", {}).get("namespace") == namespace
            and not item.get("error")
        )
    ]
    return view, sorted(excluded)
