"""Operator-only export of registered targets and current Service ClusterIP."""
import argparse
import json
from pathlib import Path

from backend.app.business_checks.collector import build_target
from backend.app.service_profiles.registry import load_profile
from backend.app.tools.client import create_clients
from backend.app.tools.service_tools import get_service


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    profile = load_profile('agent-demo', 'order-service')
    clients = create_clients(context='kind-incident-agent')
    service = get_service(clients, profile.namespace, profile.service_name).model_dump(mode='json')
    targets = [build_target(profile, check, service) for check in profile.business_checks]
    if not targets:
        raise SystemExit('No registered checks; refusing to deploy an empty checker configuration.')
    Path(args.output).write_text(json.dumps({'targets': targets}, ensure_ascii=False, indent=2) + '\n')
    print(f'Exported {len(targets)} registered check(s).')


if __name__ == '__main__':
    main()
