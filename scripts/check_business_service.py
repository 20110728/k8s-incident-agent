"""Read-only acceptance through the production collector, without LLM calls."""
import argparse
import json
from uuid import uuid4

from backend.app.agent.dependencies import build_kubernetes_collector
from backend.app.agent.collector_adapter import normalize_evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect', choices=['passed', 'failed', 'unknown', 'skipped'], required=True)
    args = parser.parse_args()
    bundle = build_kubernetes_collector().collect('agent-demo', 'order-service')
    evidence = [e for e in normalize_evidence(incident_id=str(uuid4()), bundle=bundle)
                if e['resource_type'] == 'BusinessCheck']
    print(json.dumps({'profile_status': bundle.get('service_profile', {}).get('status'),
                      'business_evidence': evidence}, ensure_ascii=False, indent=2))
    if not evidence or any(e['data']['status'] != args.expect for e in evidence):
        raise SystemExit(2)


if __name__ == '__main__':
    main()
