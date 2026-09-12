# 固定案例运行入口：区分采集事实、合成回归和真实模型诊断/规划。
# 结果按次保存输入、输出和断言；此入口不执行真实审批和 Agent 资源修改。

"""Read-only capture/replay of registered cases; no Agent approval or remediation execution."""
import argparse
import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from scripts.fault_cases.suite import (
    catalog,case_definition,load_fixture,digest,diagnose_fixture,check_approval_conflict,check_log_attack,evaluate,
)
from backend.app.agent.collector_adapter import normalize_evidence
from backend.app.agent.nodes import make_diagnose_incident_node,make_retrieve_runbooks_node,make_plan_remediation_node
from backend.app.agent.remediation_policy import get_allowed_remediation_actions


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=[c['case_id'] for c in catalog()['cases']],required=True)
    parser.add_argument('--source',choices=['replay','live'],default='replay')
    parser.add_argument('--mode',choices=['facts','regression','agent'],default='facts',
        help='facts: no diagnosis; regression: deterministic fixture, not model evaluation; agent: real diagnosis/planning model')
    parser.add_argument('--output-dir',type=Path,default=Path('evals/results/stage5'))
    args=parser.parse_args()
    definition=case_definition(args.case);fixture=load_fixture(args.case)
    if args.source=='live' and (not definition['live_supported'] or args.mode=='regression'):
        parser.error('this case/mode only supports explicitly labelled replay')
    if args.case=='changed_after_approval' and args.mode!='regression':
        parser.error('approval-change case is a deterministic regression replay, not a real approval acceptance')
    started=time.monotonic()
    if args.source=='live':
        os.environ['KUBERNETES_CONTEXT']='kind-incident-agent'
        from backend.app.agent.dependencies import build_kubernetes_collector
        bundle=build_kubernetes_collector().collect('agent-demo','order-service')
        state=dict(incident_id=uuid4().hex,request=fixture['state']['request'],service_profile=bundle.get('service_profile'),
                   errors=bundle.get('errors',[]))
        state['evidence']=normalize_evidence(incident_id=state['incident_id'],bundle=bundle)
    else:
        state=copy.deepcopy(fixture['state'])
    # 保存诊断前输入，后续检索或模型节点更新不会改变这份回放基准。
    original=copy.deepcopy(state)
    checked=False;planned=False;special=None
    if args.mode=='regression':
        state.update(diagnose_fixture(state,fixture));checked=True
        if args.case=='changed_after_approval':special=check_approval_conflict(fixture)
        if args.case=='log_instruction':special=check_log_attack(state)
    if args.mode=='agent':
        from backend.app.agent.dependencies import build_diagnosis_service,build_runbook_retriever,build_remediation_planner
        if args.source=='live':state.update(make_retrieve_runbooks_node(build_runbook_retriever())(state))
        # Replay uses frozen actual Runbook text; it does not evaluate live retrieval.
        if args.source=='replay' or state.get('phase')=='runbooks_retrieved':
            state.update(make_diagnose_incident_node(build_diagnosis_service())(state))
            if state.get('phase')=='diagnosis_completed' and get_allowed_remediation_actions(state):
                state.update(make_plan_remediation_node(build_remediation_planner())(state));planned=True
        checked=True
    result=evaluate(definition,state,diagnosis_checked=checked,plan_checked=planned,special=special)
    steps={t.get('step') for t in state.get('trace',[])}
    origin=('not_run' if args.mode=='facts' else 'fixture_pipeline' if args.mode=='regression'
            else 'rule_precheck' if 'diagnosis_policy_precheck' in steps
            else 'llm_with_controlled_report' if 'diagnosis_controlled_report' in steps
            else 'llm' if state.get('diagnosis') else 'failed')
    result.update(diagnosis_origin=origin,llm_model=state.get('llm_model') if args.mode=='agent' else None,case_id=args.case,suite_version=catalog()['suite_version'],source=args.source,mode=args.mode,
        fixture_provenance=fixture['provenance'] if args.source=='replay' else None,
        catalog_digest=digest(catalog()),input_digest=digest(original),
        elapsed_ms=round((time.monotonic()-started)*1000),
        diagnosis_model_usage=state.get('llm_usage') if args.mode=='agent' else None,
        planning_model_usage=state.get('remediation_llm_usage') if args.mode=='agent' else None,
        phase=state.get('phase'),trace=state.get('trace',[]),
        live_retrieval_evaluated=args.source=='live' and args.mode=='agent',
        real_approval_evaluated=False,agent_writes_executed=False,
        unchecked=['post-remediation resource/business recovery','restricted Kubernetes identity'],
        diagnosis=state.get('diagnosis'),remediation_plan=state.get('remediation_plan'),errors=state.get('errors',[]))
    # Unique per-run folder; never overwrite another run's input or result.
    folder=args.output_dir/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+args.case+'-'+uuid4().hex[:8])
    folder.mkdir(parents=True,exist_ok=False)
    for name,value in [('input.json',original),('output-state.json',state),('result.json',result)]:
        (folder/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')
    print(json.dumps({**result,'saved_to':str(folder)},ensure_ascii=False,indent=2))
    if not result['passed']:raise SystemExit(2)


if __name__=='__main__':main()
