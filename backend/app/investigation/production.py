"""Production adapters: lazy dependencies and deterministic plans, no second LLM."""
from backend.app.agent.diagnosis_policy import diagnostic_facts
from backend.app.agent.remediation_policy import get_allowed_remediation_actions, prepare_remediation_plan
from backend.app.agent.schemas import CurrentDiagnosis, RemediationPlan
from backend.app.investigation.entrypoint import prepare_baseline
from backend.app.service_profiles.registry import matched_profile


class LazyToolbox:
    def __init__(self, budget):
        self.budget, self.box = budget, None

    @property
    def state(self):
        return prepare_baseline(self.budget)

    def __getattr__(self, name):
        if self.box is None:
            from backend.app.tools.investigation import build_investigation_toolbox
            self.box = build_investigation_toolbox(self.budget, self.state)
        return getattr(self.box, name)


class LazyModel:
    def __init__(self):
        self.model = None

    def invoke(self, prompt):
        if self.model is None:
            from backend.app.investigation.model import InvestigationModel
            self.model = InvestigationModel()
        return self.model.invoke(prompt)


def deterministic_plan(state, candidate):
    if candidate not in {"patch_service_selector", "patch_readiness_probe"} or candidate not in get_allowed_remediation_actions(state):
        raise ValueError("REPAIR_CANDIDATE_NOT_ALLOWED")
    profile = matched_profile(state)
    params = dict(namespace=profile.namespace, resource_kind="Service", resource_name=profile.service_name,
        container_name=None, current_probe_path=None, proposed_probe_path=None, current_probe_port=None,
        proposed_probe_port=None, current_selector=[], proposed_selector=[], investigation_steps=[])
    if candidate == "patch_service_selector":
        service = next(e["data"] for e in state["evidence"] if e["resource_type"] == "Service" and e["resource_name"] == profile.service_name)
        params.update(current_selector=[{"key": k, "value": v} for k, v in sorted(service["selector"].items())],
                      proposed_selector=[{"key": k, "value": v} for k, v in sorted(profile.expected_selector.items())])
    else:
        deployment = next(e["data"] for e in state["evidence"] if e["resource_type"] == "Deployment" and e["resource_name"] == profile.deployment_name)
        container = next(c for c in deployment["containers"] if c["name"] == profile.container_name)
        probe = container["readiness_probe"]
        params.update(resource_kind="Deployment", resource_name=profile.deployment_name, container_name=profile.container_name,
            current_probe_path=probe["path"], current_probe_port=probe["port"],
            proposed_probe_path=profile.readiness_probe.path, proposed_probe_port=profile.readiness_probe.port)
    diagnosis = state["diagnosis"]
    plan = RemediationPlan(action=candidate, parameters=params, risk_level="medium", requires_approval=True,
        summary="程序依据当前证据和登记配置生成单项修复计划，需人工审批。",
        expected_result="恢复登记配置；执行后分别验证资源就绪及登记业务接口。",
        rollback_plan="如需回退，由负责人核对审批记录中的原配置并另行授权处理。",
        evidence_ids=diagnosis["evidence_ids"], runbook_ids=diagnosis["runbook_ids"])
    validated, _ = prepare_remediation_plan(plan=plan, state=state)
    return validated


def unknown_diagnosis(current, output):
    facts = diagnostic_facts(current)
    reason = output.get("stop_reason") or (output.get("decision") or {}).get("reason") or "INVESTIGATION_STOPPED"
    resource = {"ready": "就绪", "not_ready": "未就绪", "unknown": "未知"}[facts["resource_status"]]
    business = {"passed": "通过", "failed": "失败", "unknown": "未知"}[facts["business_status"]]
    summary = f"已保存 {len(current['evidence'])} 项证据；采样时资源状态：{resource}，业务检查：{business}。"
    summary += "这些状态不能单独确认根因或代表当前已恢复。程序交接：" + reason
    return CurrentDiagnosis(fault_category="unknown", confidence=0.0, root_cause="本轮调查已停止，尚不能确定根因。",
        reasoning_summary=summary, evidence_ids=[e["evidence_id"] for e in current["evidence"]], runbook_ids=[],
        assessment=dict(schema_version="v2", problem_domain="insufficient_evidence", symptoms=[], root_cause_hypotheses=[],
            missing_evidence=(output.get("decision") or {}).get("unknowns") or ["足以确认根因的当前证据"],
            next_investigation=["核对本轮记录，必要时明确发起新一轮完整调查"],
            resource_status=facts["resource_status"], business_status=facts["business_status"],
            unverified_scope=["根因、未登记接口及所有副本的业务覆盖"])).model_dump(mode="json")


def answer_for_investigation(payload, question):
    slot = question["questions"][0]["slot"]
    return {"question_id": payload["question_id"], "version": payload["version"], "message_id": payload["message_id"],
            "text": "" if payload["skip"] else payload["answers"][slot], "skip": payload["skip"],
            "changed_resource_refs": payload.get("changed_resource_refs", [])}
