# 操作者故障注入工具：用固定 kubectl 参数切换已登记的 Demo 场景。
# 包含真实集群写操作，但不暴露给 Agent；遇到非本轮已知配置变化即拒绝覆盖。

"""Operator-only Demo mutations using fixed kubectl arguments and JSON Patch tests."""
import argparse
import json
import subprocess
import time
import os

from backend.app.service_profiles.registry import load_profile

PREFIX=['kubectl','--context','kind-incident-agent','-n','agent-demo','--request-timeout=20s']
BAD_SELECTOR={'app':'stage5-wrong'}
BAD_PATH='/stage5-not-ready'


def kubectl(*args):
    return subprocess.check_output([*PREFIX,*args],text=True)


def verify_deployment(raw,profile):
    template=raw['spec']['template'];containers=template['spec']['containers']
    if (template.get('metadata',{}).get('labels',{}).get('app.kubernetes.io/version')!='order-demo-v0.2.0'
            or {c['name']:c['image'] for c in containers}!=profile.application.images):
        raise ValueError('refusing a different Demo release or image')
    if not all(template.get('metadata',{}).get('labels',{}).get(k)==v for k,v in profile.expected_selector.items()):
        raise ValueError('registered selector does not match the workload template')
    return next(i for i,c in enumerate(containers) if c['name']==profile.container_name)


def configuration_patch(raw,changes):
    metadata=raw['metadata']
    # JSON Patch 的前置测试与替换在同一次 API 请求中提交，防止读取后发生并发修改。
    return [dict(op='test',path='/metadata/uid',value=metadata['uid']),
            dict(op='test',path='/metadata/resourceVersion',value=metadata['resourceVersion']),*changes]


def change(kind,name,raw,path,old,new):
    if old==new:return
    body=configuration_patch(raw,[dict(op='test',path=path,value=old),dict(op='replace',path=path,value=new)])
    print(kubectl('patch',kind,name,'--type=json','-p',json.dumps(body)),end='')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['guard','selector_mismatch','readiness_path_error','reset_configuration','probe_down','probe_up','wait_unready'])
    args=parser.parse_args()
    p=load_profile('agent-demo','order-service')
    if (p.application.version!='order-demo-v0.2.0' or p.application.images!={'order-service':'k8s-incident-demo:0.2.0'}
            or p.readiness_probe.path!='/readyz' or p.readiness_probe.port!='http' or p.readiness_probe.scheme!='HTTP'):
        raise ValueError('stage5 operator only supports the registered order-demo-v0.2.0 contract')
    dep=json.loads(kubectl('get','deployment','order-service','-o','json'))
    index=verify_deployment(dep,p)
    service=json.loads(kubectl('get','service','order-service','-o','json'))
    selector=service['spec']['selector'];probe=dep['spec']['template']['spec']['containers'][index]['readinessProbe']['httpGet']
    if selector not in [p.expected_selector,BAD_SELECTOR] or probe.get('path') not in [p.readiness_probe.path,BAD_PATH] or probe.get('port')!=p.readiness_probe.port or probe.get('scheme','HTTP')!='HTTP':
        raise ValueError('unexpected configuration; refusing to overwrite a non-stage5 change')
    if args.action=='wait_unready':
        from backend.app.agent.dependencies import build_kubernetes_collector
        from backend.app.agent.collector_adapter import normalize_evidence
        from backend.app.agent.diagnosis_policy import diagnostic_facts
        os.environ['KUBERNETES_CONTEXT']='kind-incident-agent'
        collector=build_kubernetes_collector()
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            bundle=collector.collect('agent-demo','order-service')
            facts=diagnostic_facts(dict(request=dict(namespace='agent-demo',service_name='order-service'),
                service_profile=bundle.get('service_profile'),evidence=normalize_evidence(incident_id='stage5-wait',bundle=bundle)))
            if facts['readiness_drift'] and facts['resource_status']=='not_ready' and facts['business_status']=='passed':
                return
            time.sleep(1)
        raise TimeoutError('rolling-update probe case did not reach not_ready/passed; reset explicitly')
    if args.action=='selector_mismatch':
        change('service','order-service',service,'/spec/selector',selector,BAD_SELECTOR)
    if args.action=='readiness_path_error':
        change('deployment','order-service',dep,f'/spec/template/spec/containers/{index}/readinessProbe/httpGet/path',probe['path'],BAD_PATH)
    if args.action=='reset_configuration':
        change('service','order-service',service,'/spec/selector',selector,p.expected_selector)
        change('deployment','order-service',dep,f'/spec/template/spec/containers/{index}/readinessProbe/httpGet/path',probe['path'],p.readiness_probe.path)
    if args.action in {'probe_down','probe_up'}:
        helper=json.loads(kubectl('get','deployment','incident-agent-business-probe','-o','json'))
        if [c['image'] for c in helper['spec']['template']['spec']['containers']]!=['k8s-business-probe:0.2.0']:
            raise ValueError('unexpected checker image; refusing to scale')
        replicas=helper['spec']['replicas']
        if replicas not in [0,1]:raise ValueError('unexpected checker replica count')
        change('deployment','incident-agent-business-probe',helper,'/spec/replicas',replicas,0 if args.action=='probe_down' else 1)


if __name__=='__main__':main()
