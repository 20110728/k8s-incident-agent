"""Read-only stage-4 acceptance: evidence gates, optionally real RAG/LLM diagnosis. No planner/executor."""
import argparse
import json
import time
from uuid import uuid4

from backend.app.agent.collector_adapter import normalize_evidence
from backend.app.agent.dependencies import build_kubernetes_collector, build_diagnosis_service, build_runbook_retriever
from backend.app.agent.diagnosis_policy import diagnostic_facts
from backend.app.agent.nodes import make_diagnose_incident_node, make_retrieve_runbooks_node
from backend.app.agent.remediation_policy import get_allowed_remediation_actions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--llm', action='store_true', help='Calls configured embedding/RAG and diagnosis model; records actual usage')
    parser.add_argument('--description', default='检查订单服务当前资源和登记业务接口状态；历史异常不代表当前故障。')
    parser.add_argument('--expect-business', choices=['passed', 'failed', 'unknown'])
    parser.add_argument('--expect-resource', choices=['ready', 'not_ready', 'unknown'])
    args = parser.parse_args()
    started = time.monotonic()
    bundle = build_kubernetes_collector().collect('agent-demo', 'order-service')
    state = dict(incident_id=str(uuid4()), request=dict(namespace='agent-demo', service_name='order-service', description=args.description),
                 service_profile=bundle.get('service_profile'), errors=bundle.get('errors', []))
    state['evidence'] = normalize_evidence(incident_id=state['incident_id'], bundle=bundle)
    facts = diagnostic_facts(state)
    if args.llm:
        state.update(make_retrieve_runbooks_node(build_runbook_retriever())(state))
        if state.get('phase') == 'runbooks_retrieved':
            state.update(make_diagnose_incident_node(build_diagnosis_service())(state))
    precheck = any(e.get('step') == 'diagnosis_policy_precheck' for e in state.get('trace', []))
    controlled = any(e.get('step') == 'diagnosis_controlled_report' for e in state.get('trace', []))
    output = dict(diagnosis_origin=('rule_precheck' if precheck else 'llm_with_controlled_report' if controlled else 'llm') if state.get('diagnosis') else None,
                  diagnosis_model_output=state.get('diagnosis_model_output'),
                  trace=state.get('trace', []), policy_facts=facts, diagnosis=state.get('diagnosis'), phase=state.get('phase'),
                  allowed_actions=sorted(get_allowed_remediation_actions(state)) if args.llm else None,
                  llm_usage=state.get('llm_usage'), llm_model=state.get('llm_model'),
                  diagnosis_retry_count=state.get('diagnosis_retry_count'),
                  elapsed_ms=round((time.monotonic()-started)*1000), errors=state.get('errors'),
                  writes_executed=False)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if (args.expect_business and facts['business_status'] != args.expect_business
            or args.expect_resource and facts['resource_status'] != args.expect_resource
            or args.llm and state.get('phase') != 'diagnosis_completed'):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
