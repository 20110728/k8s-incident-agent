# 确定性诊断约束：从当前证据计算资源状态、业务状态和配置漂移，再核对诊断。
# 证据不足分支可以直接生成保守报告；当前日志的存在本身不能证明下游根因。

"""Evidence consistency gates, not an LLM replacement or a recovery verifier."""
import json

from backend.app.agent.schemas import Diagnosis, CurrentDiagnosis
from backend.app.service_profiles.registry import ProfileUnavailable, matched_profile


class InvalidDiagnosisAssessment(ValueError):
    pass



def diagnostic_facts(state: dict) -> dict:
    evidence = [e for e in state.get('evidence', []) if not e.get('error')]
    request = state.get('request') or {}
    ns, service = request.get('namespace'), request.get('service_name')
    def items(kind, name=None):
        return [e for e in evidence if e.get('resource_type') == kind
                and (name is None or e.get('resource_name') == name)]

    facts = dict(resource_status='unknown', business_status='unknown',
                 selector_drift=False, readiness_drift=False,
                 configuration_evidence_ids=[], business_evidence_ids=[],
                 resource_evidence_ids=[], current_runtime_faults=[], configuration_evidence_complete=False,
                 current_log_evidence_ids=[], profile_matched=False)
    try:
        profile = matched_profile(state)
    except ProfileUnavailable:
        return facts
    facts['profile_matched'] = True
    dep_e = items('Deployment', profile.deployment_name)[0]
    dep = dep_e['data']
    services = items('Service', service)
    svc_e = services[0] if len(services) == 1 else None
    svc = svc_e['data'] if svc_e else {}
    service_valid = (svc.get('namespace') == ns and svc.get('name') == service
                     and isinstance(svc.get('selector'), dict))
    if service_valid:
        facts['selector_drift'] = (isinstance(svc.get('selector'), dict)
                                   and svc['selector'] != profile.expected_selector)
        facts['configuration_evidence_ids'].append(svc_e['evidence_id'])
    facts['configuration_evidence_ids'].append(dep_e['evidence_id'])
    container = next((c for c in dep.get('containers', [])
                      if c.get('name') == profile.container_name), {})
    probe = container.get('readiness_probe') or {}
    facts['configuration_evidence_complete'] = bool(service_valid and container and probe.get('path') and probe.get('port') is not None)
    expected = profile.readiness_probe
    facts['readiness_drift'] = bool(
        probe.get('path') and probe.get('port') is not None
        and (probe.get('scheme') or 'HTTP') == expected.scheme
        and (probe['path'], probe['port']) != (expected.path, expected.port))

    # 只评价登记 Deployment 拥有的 Pod，避免 selector 错误时混入其他工作负载。
    owned = {e['resource_name'] for e in items('OwnerChain')
             if e.get('data', {}).get('owner_chain', {}).get('deployment_name') == profile.deployment_name
             and e['data']['owner_chain'].get('namespace') == ns}
    pods = [e for e in items('PodStatus') if e['resource_name'] in owned
            and e['data'].get('namespace') == ns]
    endpoints = [e for e in items('EndpointSlice')
                 if e['data'].get('namespace') == ns and e['data'].get('service_name') == service]
    facts['resource_evidence_ids'] = [e['evidence_id'] for e in [dep_e, *pods, *endpoints]]
    for pod in pods:
        for c in pod['data'].get('containers', []):
            # Historical restart counts / last_terminated_reason are not current failures.
            reason = c.get('waiting_reason') if c.get('state') == 'waiting' else c.get('terminated_reason') if c.get('state') == 'terminated' else None
            category = {'CrashLoopBackOff': 'crash_loop_backoff', 'ImagePullBackOff': 'image_pull_backoff',
                        'ErrImagePull': 'image_pull_backoff', 'OOMKilled': 'oom_killed'}.get(reason)
            if category:
                facts['current_runtime_faults'].append(category)
    facts['current_log_evidence_ids'] = [e['evidence_id'] for e in items('PodLogs')
        if e['resource_name'] in owned and e['data'].get('previous') is False
        and bool(e['data'].get('content'))]
    desired = dep.get('desired_replicas')
    if isinstance(desired, int) and desired > 0:
        if (dep.get('ready_replicas', 0) < desired or dep.get('available_replicas', 0) < desired
                or facts['selector_drift'] or any(p['data'].get('ready') is False for p in pods)):
            facts['resource_status'] = 'not_ready'
        elif (service_valid and len(pods) >= desired and all(p['data'].get('ready') is True for p in pods)
              and any(ep.get('ready') is True and ep.get('target_name') in owned
                      for e in endpoints for ep in e['data'].get('endpoints', []))):
            facts['resource_status'] = 'ready'

    # Only complete, target-bound checks can support a passed business assessment.
    # Collector already validates the wire result; check binding again for persisted evidence.
    from backend.app.business_checks.collector import build_target
    checks = items('BusinessCheck')
    statuses = []
    for check in profile.business_checks:
        matches = [e for e in checks if e['data'].get('check_id') == check.check_id]
        if len(matches) != 1:
            statuses.append('unknown'); continue
        e = matches[0]; data = e['data']
        facts['business_evidence_ids'].append(e['evidence_id'])
        try:
            target = build_target(profile, check, svc)
            valid = (data.get('scope') == 'cluster_service_http' and data.get('request_id')
                     and e.get('source') == 'cluster_http_probe'
                     and json.dumps(data.get('target'), sort_keys=True) == json.dumps(target, sort_keys=True))
        except (ValueError, KeyError, TypeError):
            valid = False
        status = data.get('status') if valid else 'unknown'
        if status == 'passed' and not (data.get('http_status') == check.expected_status
                and data.get('content_matches') is True and data.get('error_code') is None):
            status = 'unknown'
        statuses.append(status)
    # 任一可信检查失败即为 failed；只有全部登记检查通过才能为 passed。
    if 'failed' in statuses:
        facts['business_status'] = 'failed'
    elif statuses and all(s == 'passed' for s in statuses):
        facts['business_status'] = 'passed'
    return facts


def validate_diagnosis_assessment(diagnosis: Diagnosis, state: dict) -> None:
    a = diagnosis.assessment
    if a is None:
        raise InvalidDiagnosisAssessment('new diagnosis requires assessment schema_version=v2')
    facts = diagnostic_facts(state)
    def require(condition, message):
        if not condition:
            raise InvalidDiagnosisAssessment(message)
    require(a.resource_status == facts['resource_status'], 'resource_status must equal policy_facts.resource_status')
    require(a.business_status == facts['business_status'], 'business_status must equal policy_facts.business_status')
    for finding in [*a.symptoms, *a.root_cause_hypotheses]:
        require(set(finding.evidence_ids) <= set(diagnosis.evidence_ids), 'finding references must be declared in diagnosis.evidence_ids')
    require(set(facts['business_evidence_ids']) <= set(diagnosis.evidence_ids), 'diagnosis must cite collected registered BusinessCheck results')
    category = diagnosis.fault_category
    domains = {'service_selector_mismatch': 'deployment_configuration',
               'readiness_probe_error': 'deployment_configuration', 'application_error': 'application_runtime',
               'dependency_error': 'dependency', 'unknown': 'insufficient_evidence', 'no_fault_detected': 'none',
               'crash_loop_backoff': 'application_runtime', 'oom_killed': 'application_runtime'}
    if category in domains:
        require(a.problem_domain == domains[category], 'problem_domain contradicts fault_category')
    if category == 'image_pull_backoff':
        require(a.problem_domain in {'deployment_configuration', 'dependency', 'insufficient_evidence'}, 'image pull failure does not establish application runtime failure')
    if category == 'no_fault_detected':
        require(facts['resource_status'] == 'ready' and facts['business_status'] == 'passed'
                and not facts['readiness_drift'] and not facts['current_runtime_faults'],
                'no_fault_detected requires ready resources and all registered business checks passed, without current drift/failure')
        require(set(facts['resource_evidence_ids']) <= set(diagnosis.evidence_ids), 'no_fault_detected must cite current resource evidence')
        require(not a.root_cause_hypotheses, 'no_fault_detected must not assert an active root cause')
    
    else:
        # 已有充分证据支持的配置故障，不强制虚构缺失证据或调查步骤。
        # 仅有模型分类不够：必须存在真实漂移、完整配置证据，
        # 且 supported 假设引用了必要配置证据。
        config_key = {
            'service_selector_mismatch': 'selector_drift',
            'readiness_probe_error': 'readiness_drift',
        }.get(category)

        grounded_configuration = bool(
            config_key
            and facts['configuration_evidence_complete']
            and facts[config_key]
            and facts['business_status'] != 'failed'
            and not facts['current_runtime_faults']
            and any(
                h.status == 'supported'
                and set(facts['configuration_evidence_ids'])
                <= set(h.evidence_ids)
                for h in a.root_cause_hypotheses
            )
        )

        # 其他故障、证据不足或仅有猜测的配置问题，仍保持原要求。
        if not grounded_configuration:
            require(
                bool(a.missing_evidence)
                and bool(a.next_investigation),
                'fault/unknown diagnosis must state missing evidence '
                'and next investigation',
            )   

    if category == 'unknown':
        require(diagnosis.confidence <= 0.6, 'unknown confidence must be <= 0.6')
    if category in {'service_selector_mismatch', 'readiness_probe_error'}:
        key = 'selector_drift' if category == 'service_selector_mismatch' else 'readiness_drift'
        require(facts[key], 'configuration diagnosis requires version-matched registered configuration drift')
        require(set(facts['configuration_evidence_ids']) <= set(diagnosis.evidence_ids), 'configuration diagnosis must cite Service and registered Deployment')
    if category == 'application_error':
        supported = facts['business_status'] == 'failed' or bool(facts['current_runtime_faults'])
        require(supported, 'application_error needs a current observed failure, not connection error alone')
    if category in {'crash_loop_backoff', 'image_pull_backoff', 'oom_killed'}:
        require(category in facts['current_runtime_faults'], 'historical logs/events/restarts cannot establish a current runtime failure')
    if category == 'dependency_error':
        require(facts['resource_status'] == 'not_ready' or facts['business_status'] == 'failed', 'dependency hypothesis needs current failure symptoms')
        require(bool(set(facts['current_log_evidence_ids']) & set(diagnosis.evidence_ids)), 'dependency hypothesis needs current workload log evidence; connection error alone is insufficient')
        require(bool(a.root_cause_hypotheses), 'dependency_error is a suspected cause and requires explicit hypotheses')
    # Stage 4 collects no independent downstream check. Log statements / HTTP status
    # establish symptoms but cannot establish the underlying code/dependency cause.
    if category not in {'service_selector_mismatch', 'readiness_probe_error'}:
        require(all(h.status == 'suspected' for h in a.root_cause_hypotheses), 'underlying runtime/dependency causes remain suspected in this evidence scope')


# 仅覆盖这组明确事实，不作为所有模型校验失败的通用兜底。
def can_report_insufficient_evidence(facts: dict) -> bool:
    return bool(facts['profile_matched']
                and facts['configuration_evidence_complete']
                and facts['resource_status'] == 'not_ready'
                and facts['business_status'] == 'unknown'
                and not facts['selector_drift'] and not facts['readiness_drift']
                and not facts['current_runtime_faults']
                and facts['business_evidence_ids'] and facts['resource_evidence_ids'])


def insufficient_evidence_diagnosis(state: dict) -> CurrentDiagnosis:
    """Build a rule-generated report from evidence without calling a diagnosis model."""
    facts = diagnostic_facts(state)
    if not can_report_insufficient_evidence(facts):
        raise InvalidDiagnosisAssessment('insufficient-evidence precheck is not applicable')
    refs = list(dict.fromkeys(facts['resource_evidence_ids'] + facts['business_evidence_ids']))
    return CurrentDiagnosis(
        fault_category='unknown', confidence=0.0, evidence_ids=refs, runbook_ids=[],
        root_cause='本地证据规则：当前资源未就绪、登记业务检查结果未知。现有证据不足以确定应用或依赖根因，本次未调用诊断模型。',
        reasoning_summary='本结论由程序依据当前结构化证据生成。资源未就绪与业务检查未知是症状，不能单独证明探针配置、应用逻辑或下游依赖故障。',
        assessment=dict(schema_version='v2', problem_domain='insufficient_evidence',
            symptoms=[
                dict(summary='当前采样显示登记工作负载及关联资源未就绪。', evidence_ids=facts['resource_evidence_ids']),
                dict(summary='登记业务检查结果未知，未证明接口正常，也未取得可用于确定业务根因的结果。', evidence_ids=facts['business_evidence_ids']),
            ], root_cause_hypotheses=[],
            missing_evidence=['与本次故障时间对应的应用诊断信息及就绪失败原因', '实际依赖关系及独立下游检查结果'],
            next_investigation=['由登记负责人核对应用就绪检查的失败原因，并结合当前日志定位所缺证据',
                                '先确认应用实际依赖关系，再对已确认的下游进行只读检查；不假定存在数据库',
                                '人工处理后重新检查资源状态与登记业务接口，不通过放宽探针消除症状'],
            resource_status='not_ready', business_status='unknown',
            unverified_scope=['应用与下游具体根因', '集群外入口、未登记接口及所有副本逐一业务验证']),
    )
