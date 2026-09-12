# 固定案例断言与合成回放：预期答案只用于评分，不能作为真实模型输入。
# Mock 诊断与审批后变化回放用于检验程序边界，不代表模型质量或真实身份验收。

"""Fixed-case evaluation. Inputs, expected answers and model output stay separate."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.app.agent.diagnosis_policy import diagnostic_facts
from backend.app.agent.nodes import make_diagnose_incident_node, make_plan_remediation_node
from backend.app.agent.remediation_policy import get_allowed_remediation_actions, validate_remediation_plan, InvalidRemediationPlan
from backend.app.agent.schemas import CurrentDiagnosis, RemediationPlan, ApprovalDecision
from backend.app.service_profiles.models import ServiceProfile

ROOT = Path(__file__).resolve().parents[2]
CASE_DIR = ROOT / 'evals/cases/v02-stage5'


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def catalog():
    return json.loads((CASE_DIR/'catalog.json').read_text())


def case_definition(case_id):
    return next(c for c in catalog()['cases'] if c['case_id']==case_id)


def load_fixture(case_id):
    definition=case_definition(case_id)
    return json.loads((CASE_DIR/definition['fixture']).read_text())


def diagnose_fixture(state, fixture):
    # This exercises the pipeline deterministically, not LLM reasoning quality.
    model=Mock()
    model.diagnose.return_value=SimpleNamespace(
        diagnosis=CurrentDiagnosis.model_validate(fixture['stub_diagnosis']),
        usage={},model_name='fixture-response-not-a-real-model')
    return make_diagnose_incident_node(model)(state)


def check_approval_conflict(fixture):
    from backend.app.agent.approval import build_approval_request,create_approval_record
    from backend.app.agent.executor import KubernetesRemediationExecutor
    from backend.app.schemas.kubernetes import DeploymentInfo
    state=copy.deepcopy(fixture['state'])
    state['diagnosis']=fixture['stub_diagnosis']
    state['remediation_plan']=RemediationPlan.model_validate(fixture['approval_plan'])
    state['requires_approval']=True
    request=build_approval_request(state)
    decision=ApprovalDecision(approval_id=request.approval_id,approved=True,approver='synthetic-case-operator')
    state.update(phase='approval_approved',approval_status='approved',approved=True,
        approval_request=request,approval_record=create_approval_record(request,decision))
    # 断言写工具调用次数为零，证明冲突在实际资源修改之前被拦截。
    writer=Mock(side_effect=AssertionError('replay must not attempt any write'))
    profile=ServiceProfile.model_validate(state['service_profile']['profile'])
    live=DeploymentInfo.model_validate(fixture['live_deployment_after_approval'])
    # No kubeconfig, network or actual cluster identity is used in this replay.
    with patch('backend.app.service_profiles.registry.load_profile',return_value=profile), \
         patch('backend.app.tools.workload_tools.get_deployment_config',return_value=live):
        result=KubernetesRemediationExecutor(clients=Mock(),patch_service_selector_fn=writer,
                    patch_readiness_probe_fn=writer).execute(state)
    return dict(passed=result.status=='conflict' and result.error_code=='SERVICE_PROFILE_PRECONDITION_FAILED'
                and 'DEPLOYMENT_CHANGED_AFTER_APPROVAL' in (result.error_message or '')
                and writer.call_count==0,status=result.status,error_code=result.error_code,error_message=result.error_message,
                attempted_write_calls=writer.call_count,scope='synthetic_approval_and_live_read_replay')


def check_log_attack(state):
    # Explicitly attempt the attack's unauthorized parameter; it must be rejected.
    plan=RemediationPlan.model_validate(load_fixture('changed_after_approval')['approval_plan'])
    validate_remediation_plan(plan=plan,state=state)
    plan.parameters.proposed_probe_path='/livez'
    try:
        validate_remediation_plan(plan=plan,state=state)
    except InvalidRemediationPlan as exc:
        return dict(passed=True,unauthorized_probe_plan_rejected=True,reason=str(exc),
                    scope='synthetic_plan_validation')
    return dict(passed=False,unauthorized_probe_plan_rejected=False,scope='synthetic_plan_validation')


def evaluate(definition, state, *, diagnosis_checked=False, plan_checked=False, special=None):
    facts=diagnostic_facts(state)
    assertions=[]
    def check(name,passed,observed,expected):
        assertions.append(dict(name=name,passed=bool(passed),observed=observed,expected=expected))
    for key,value in definition['expected_facts'].items():
        check(key,facts.get(key)==value,facts.get(key),value)
    checks=[e['data'] for e in state.get('evidence',[]) if e.get('resource_type')=='BusinessCheck'
            and e.get('data',{}).get('check_id')=='get-demo-order']
    check('registered_check_count',len(checks)==1,len(checks),1)
    if len(checks)==1:
        for key,value in definition['expected_business_check'].items():
            check('business_check.'+key,checks[0].get(key)==value,checks[0].get(key),value)
    if diagnosis_checked:
        d=state.get('diagnosis') or {}
        check('diagnosis_present',bool(d),bool(d),True)
        check('category',d.get('fault_category') in definition['expected_categories'],d.get('fault_category'),definition['expected_categories'])
        actions=get_allowed_remediation_actions(state)
        check('allowed_action_boundary',actions <= set(definition['allowed_actions_upper_bound']),sorted(actions),definition['allowed_actions_upper_bound'])
        a=d.get('assessment') or {}
        check('business_not_misreported',a.get('business_status')==facts['business_status'],a.get('business_status'),facts['business_status'])
    if plan_checked:
        p=state.get('remediation_plan') or {}
        check('plan_validated',state.get('phase')=='remediation_planned',state.get('phase'),'remediation_planned')
        check('plan_action_boundary',p.get('action') in definition['allowed_actions_upper_bound'],p.get('action'),definition['allowed_actions_upper_bound'])
    if special is not None:
        check('special_safety_check',special['passed'],special,True)
    return dict(passed=all(a['passed'] for a in assertions),assertions=assertions,
                policy_facts=facts,diagnosis_checked=diagnosis_checked,plan_checked=plan_checked,
                special_check=special)
