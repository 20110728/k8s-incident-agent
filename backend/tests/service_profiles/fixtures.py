from pathlib import Path
"""Explicit versioned contracts for the existing write-path test fixtures."""
import json

from backend.app.service_profiles.models import ServiceProfile
from backend.app.service_profiles.registry import make_snapshot


def profile_and_deployment():
    profile = ServiceProfile.model_validate(json.loads(
        Path(__file__).with_name('legacy-profile.json').read_text()))
    deployment = {
        'namespace': 'agent-demo', 'name': 'order-service', 'uid': 'fixture-uid',
        'generation': 1, 'resource_version': 'fixture-rv',
        'template_labels': {'app': 'order-service', 'app.kubernetes.io/version': 'day21-nginx-v1'},
        'containers': [{'name': 'order-service', 'image': 'nginx:1.30.4-alpine'}],
    }
    return profile, deployment


def with_profile(state):
    profile, deployment = profile_and_deployment()
    items = [e for e in state['evidence'] if e.get('resource_type') == 'Deployment'
             and e.get('resource_name') == 'order-service']
    if items:
        # Preserve probe fields under test while adding release identity.
        existing = items[0]['data']
        containers = existing.get('containers', deployment['containers'])
        deployment.update(existing)
        deployment['containers'] = containers
        for container in containers:
            container.setdefault('image', profile.application.images[container['name']])
        items[0]['data'] = deployment
    else:
        state['evidence'].append({'evidence_id':'ev-profile-deployment', 'resource_type':'Deployment',
                                 'resource_name':'order-service', 'data': deployment})
    state['service_profile'] = make_snapshot(profile, deployment)
    return state


def with_bundle_profile(bundle):
    profile, deployment = profile_and_deployment()
    bundle['deployments']['order-service'] = deployment
    bundle['service_profile'] = make_snapshot(profile, deployment)
    return bundle
