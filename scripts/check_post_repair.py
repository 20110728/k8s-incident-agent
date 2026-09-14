"""只读查询已有事件的持久化恢复结果；不创建事件、不审批、不修改集群。"""
import argparse
import json
from urllib.request import urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--incident-id', required=True)
    parser.add_argument('--api-base', default='http://127.0.0.1:8000/api/v1')
    parser.add_argument('--expect-business', choices=['passed','failed','unknown','skipped'], required=True)
    args = parser.parse_args()
    if not args.incident_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-' for c in args.incident_id):
        parser.error('invalid incident ID')
    with urlopen(args.api_base.rstrip('/') + '/incidents/' + args.incident_id, timeout=15) as response:
        incident = json.load(response)
    result = incident.get('verification_result') or {}
    expected_success = args.expect_business == 'passed'
    assertions = {
        'new_verification_scope': result.get('verification_scope') == 'resources_and_registered_business',
        'expected_business_status': result.get('business_status') == args.expect_business,
        'no_false_success': result.get('status') != 'succeeded' or (
            result.get('resource_status') == 'ready' and result.get('business_status') == 'passed'),
        'expected_overall_status': result.get('status') == 'succeeded' if expected_success else result.get('status') != 'succeeded',
        'new_evidence_saved': bool(result.get('post_repair_evidence')) if expected_success else True,
        'scope_recorded': bool(result.get('unverified_scope')),
    }
    print(json.dumps({'incident_id': incident.get('incident_id'), 'phase': incident.get('phase'),
                     'verification_result': result, 'assertions': assertions,
                     'passed': all(assertions.values())}, ensure_ascii=False, indent=2))
    if not all(assertions.values()):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
