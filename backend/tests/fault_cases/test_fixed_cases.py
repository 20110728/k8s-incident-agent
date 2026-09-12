import copy

import pytest

from scripts.fault_cases.suite import (
    catalog,load_fixture,diagnose_fixture,check_approval_conflict,check_log_attack,evaluate,digest,
)
from scripts.fault_cases.operator import configuration_patch,verify_deployment
from backend.app.service_profiles.models import ServiceProfile


@pytest.mark.parametrize('definition',catalog()['cases'],ids=lambda d:d['case_id'])
def test_registered_case_contract(definition):
    fixture=load_fixture(definition['case_id']);state=copy.deepcopy(fixture['state'])
    state.update(diagnose_fixture(state,fixture))
    special=None
    if definition['special_check']=='approval_conflict':special=check_approval_conflict(fixture)
    if definition['special_check']=='log_injection':special=check_log_attack(state)
    result=evaluate(definition,state,diagnosis_checked=True,special=special)
    assert result['passed'],result['assertions']


def test_wrong_outcome_is_not_counted_as_pass():
    definition=catalog()['cases'][5]
    state=load_fixture('wrong_content')['state']
    state.update(diagnose_fixture(state,load_fixture('wrong_content')))
    state['diagnosis']['assessment']['business_status']='passed'
    assert not evaluate(definition,state,diagnosis_checked=True)['passed']


def test_input_fingerprint_changes_when_evidence_changes():
    state=load_fixture('normal')['state'];first=digest(state)
    state['evidence'][0]['data']['selector']={'app':'other'}
    assert digest(state)!=first


def test_operator_patch_binds_uid_version_and_actual_field():
    body=configuration_patch({'metadata':{'uid':'real-uid','resourceVersion':'101'}},[
        dict(op='test',path='/spec/selector',value={'app':'order-service'}),
        dict(op='replace',path='/spec/selector',value={'app':'stage5-wrong'})])
    assert body[:2]==[dict(op='test',path='/metadata/uid',value='real-uid'),
                     dict(op='test',path='/metadata/resourceVersion',value='101')]
    assert body[2]['op']=='test'


def test_operator_refuses_another_release():
    profile=ServiceProfile.model_validate(load_fixture('normal')['state']['service_profile']['profile'])
    raw=dict(spec=dict(template=dict(metadata=dict(labels={'app.kubernetes.io/version':'another-version'}),
        spec=dict(containers=[dict(name='order-service',image='k8s-incident-demo:0.2.0')]))))
    with pytest.raises(ValueError):verify_deployment(raw,profile)


def test_500_and_wrong_content_are_not_interchangeable():
    definition=next(c for c in catalog()['cases'] if c['case_id']=='api500')
    state=load_fixture('wrong_content')['state']
    assert not evaluate(definition,state)['passed']


def test_operator_does_not_overwrite_unrecognized_selector(monkeypatch):
    import json
    from scripts.fault_cases import operator
    from unittest.mock import Mock
    p=ServiceProfile.model_validate(load_fixture('normal')['state']['service_profile']['profile'])
    dep={'spec':{'template':{'metadata':{'labels':{'app':'order-service','app.kubernetes.io/version':'order-demo-v0.2.0'}},
         'spec':{'containers':[{'name':'order-service','image':'k8s-incident-demo:0.2.0',
                   'readinessProbe':{'httpGet':{'path':'/readyz','port':'http','scheme':'HTTP'}}}]}}}}
    fake=Mock(side_effect=[json.dumps(dep),json.dumps({'spec':{'selector':{'app':'someone-elses-change'}}})])
    monkeypatch.setattr(operator,'kubectl',fake)
    monkeypatch.setattr(operator,'load_profile',lambda *args:p)
    monkeypatch.setattr('sys.argv',['operator','reset_configuration'])
    with pytest.raises(ValueError,match='unexpected configuration'):operator.main()
    assert all(call.args[0]=='get' for call in fake.call_args_list)
