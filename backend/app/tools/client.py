import os
from dataclasses import dataclass

from kubernetes import client as k8s_client
from kubernetes import config
from kubernetes.config.config_exception import ConfigException

DEFAULT_CONTEXT = "kind-incident-agent"
DEFAULT_NAMESPACE = "agent-demo"
REQUEST_TIMEOUT = (3, 10)


@dataclass(frozen=True, slots=True)
class KubernetesClients:
    core: k8s_client.CoreV1Api
    apps: k8s_client.AppsV1Api
    discovery: k8s_client.DiscoveryV1Api


def create_clients(
    context: str | None = None,
) -> KubernetesClients:
    """Create Kubernetes API clients.

    Inside Kubernetes, use the Pod's ServiceAccount credentials.
    Outside Kubernetes, use the specified kubeconfig context.
    """
    try:
        config.load_incluster_config()
    except ConfigException:
        selected_context = context or os.getenv("KUBERNETES_CONTEXT") or DEFAULT_CONTEXT
        config.load_kube_config(context=selected_context)

    return KubernetesClients(
        core=k8s_client.CoreV1Api(),
        apps=k8s_client.AppsV1Api(),
        discovery=k8s_client.DiscoveryV1Api(),
    )
