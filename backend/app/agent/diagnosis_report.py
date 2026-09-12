# 受控报告：对已通过校验的业务断言失败诊断，用结构化事实生成正式说明。
# 原始模型输出另行留存用于审计；这里不将响应正文或模型自由文本写入正式结论。

"""Controlled narrative for observed business assertion failures.

The model classification must pass existing validation first. Response bodies and
model prose are never interpolated into the authoritative report.
"""
from backend.app.agent.diagnosis_policy import diagnostic_facts
from backend.app.agent.schemas import CurrentDiagnosis, Diagnosis


def build_controlled_report(diagnosis: Diagnosis, state: dict) -> Diagnosis:
    facts = diagnostic_facts(state)
    if diagnosis.fault_category != 'application_error' or facts['business_status'] != 'failed':
        return diagnosis
    checks = [e for e in state.get('evidence', [])
              if e.get('evidence_id') in facts['business_evidence_ids']
              and e.get('data', {}).get('status') == 'failed']
    symptoms = []
    for e in checks:
        data = e['data']
        status = data.get('http_status')
        http = f'HTTP {status}' if type(status) is int and 100 <= status <= 599 else 'HTTP 状态未确认'
        code = data.get('error_code')
        explanation = {
            'JSON_CONTENT_MISMATCH': 'JSON 内容未满足登记的字段断言',
            'HTTP_STATUS_MISMATCH': '响应状态码不符合登记约定',
            'INVALID_JSON': '响应未通过 JSON 格式校验',
        }.get(code, '业务响应未通过登记断言')
        symptoms.append(dict(summary=f'本次登记业务检查返回 {http}；{explanation}。',
                             evidence_ids=[e['evidence_id']]))
    # Preserve already validated citations and include the evidence used by this report.
    refs = list(dict.fromkeys(diagnosis.evidence_ids + facts['business_evidence_ids'] + facts['resource_evidence_ids']
                             + facts['configuration_evidence_ids']))
    resource_text = {
        'ready': '当前资源采样满足本工具的就绪检查条件',
        'not_ready': '当前资源采样未满足就绪检查条件',
        'unknown': '当前资源证据不足以判定就绪状态',
    }[facts['resource_status']]
    config_text = ('登记 selector/readiness 未发现漂移' if facts['configuration_evidence_complete']
                   and not facts['selector_drift'] and not facts['readiness_drift']
                   else '登记配置存在差异或检查范围不完整，需单独核对配置证据')
    return CurrentDiagnosis(
        fault_category='application_error', confidence=diagnosis.confidence,
        evidence_ids=refs, runbook_ids=[],
        root_cause='程序生成的证据摘要：已观察到登记业务响应断言失败；具体根因尚未确定。'
                   + ' '.join(item['summary'] for item in symptoms),
        reasoning_summary=f'{resource_text}；{config_text}。'
            '这些事实不能排除其他配置、网络或基础设施因素。application_error 在此表示已观察到业务响应异常，'
            '不等于已证明应用代码错误；置信度沿用模型的分类值，不代表具体根因已确认。',
        assessment=dict(schema_version='v2', problem_domain='application_runtime', symptoms=symptoms,
            root_cause_hypotheses=[dict(status='suspected', evidence_ids=facts['business_evidence_ids'],
                summary='可能与请求处理逻辑或运行配置有关；是否涉及数据来源或下游依赖尚待确认。')],
            missing_evidence=['与本次请求对应的应用处理日志或追踪记录',
                              '该接口实际响应生成方式、配置来源及是否存在下游依赖的说明'],
            next_investigation=['由登记负责人核对该接口约定与当前响应生成逻辑，并定位对应请求日志',
                                '确认响应数据来源及实际依赖关系；仅对已经确认存在的依赖开展只读检查',
                                '人工处理后分别重查资源就绪状态与登记业务断言'],
            resource_status=facts['resource_status'], business_status='failed',
            unverified_scope=['具体代码、运行配置或依赖根因',
                              '登记 selector/readiness 以外的配置与网络因素',
                              '集群外入口、未登记接口及所有副本逐一业务验证']),
    )
