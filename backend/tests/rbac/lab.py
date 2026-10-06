"""Isolated test identities; bootstrap admin credentials never reach the executor."""
from copy import deepcopy
from inspect import signature
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
import yaml

from backend.app.tools.client import KubernetesClients

NS = "agent-demo"
CONTEXT = "kind-incident-agent"
RESOURCE_PATHS = {
    "ServiceAccount": ("/api/v1", "serviceaccounts"), "Service": ("/api/v1", "services"),
    "Secret": ("/api/v1", "secrets"), "Namespace": ("/api/v1", "namespaces"),
    "Deployment": ("/apis/apps/v1", "deployments"),
    "Role": ("/apis/rbac.authorization.k8s.io/v1", "roles"),
    "RoleBinding": ("/apis/rbac.authorization.k8s.io/v1", "rolebindings"),
    "ClusterRole": ("/apis/rbac.authorization.k8s.io/v1", "clusterroles"),
    "ClusterRoleBinding": ("/apis/rbac.authorization.k8s.io/v1", "clusterrolebindings"),
}


def manifest(path, kind):
    matches = [row for row in yaml.safe_load_all(Path(path).read_text(encoding="utf-8")) if row and row["kind"] == kind]
    assert len(matches) == 1, (path, kind)
    return matches[0]


def api_from_config(path=None):
    configuration = client.Configuration()
    config.load_kube_config(config_file=str(path) if path else None, context=CONTEXT,
                            client_configuration=configuration)
    configuration.retries = 0
    return client.ApiClient(configuration=configuration)


def call_json_api(api, path, method, *, body=None, query=None, content_type=None):
    """Select the SDK contract before sending; never retry a possibly sent write."""
    parameters = signature(api.call_api).parameters
    if "response_types_map" in parameters:
        response = {"response_types_map": {200: "object", 201: "object", 202: "object"}}
    elif "response_type" in parameters:
        response = {"response_type": "object"}
    else:
        raise RuntimeError("Unsupported Kubernetes ApiClient.call_api signature")
    return api.call_api(path, method, body=body, query_params=query or [],
        header_params={"Content-Type": content_type or "application/json"},
        auth_settings=["BearerToken"], _return_http_data_only=False,
        _request_timeout=(3, 10), **response)


class Lab:
    def __init__(self, directory, private):
        self.directory, self.private = directory, private
        self.name = "stage3b-" + uuid4().hex[:12]
        self.created, self.results, self.apis = [], [], []
        self.admin = api_from_config()
        self.core, self.apps = client.CoreV1Api(self.admin), client.AppsV1Api(self.admin)

    def kubectl(self, *args, body=None, kubeconfig=None):
        command = ["kubectl", "--context", CONTEXT, "--request-timeout=15s"]
        if kubeconfig:
            command += ["--kubeconfig", str(kubeconfig)]
        result = subprocess.run(command + list(args), input=json.dumps(body) if body else None,
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("kubectl command failed: " + " ".join(args[:2]))
        return result.stdout

    def create(self, kind, name, *, namespace=NS, **fields):
        base, plural = RESOURCE_PATHS[kind]
        metadata = {"name": name, "labels": {"incident-agent-acceptance": "3b"}}
        if namespace:
            metadata["namespace"] = namespace
        obj = {"apiVersion": base.removeprefix("/api/").removeprefix("/apis/"),
               "kind": kind, "metadata": metadata, **fields}
        result = json.loads(self.kubectl("create", "-f", "-", "-o", "json", body=obj))
        path = base + (f"/namespaces/{namespace}" if namespace else "") + f"/{plural}/{name}"
        self.created.append((path, result["metadata"]["uid"]))
        self.save("created-resources", [{"path": resource, "uid": uid} for resource, uid in self.created])
        return result

    def save(self, name, value):
        (self.directory / (name + ".json")).write_text(json.dumps(value, default=str, indent=2), encoding="utf-8")

    def identity(self, mode):
        name = self.name + "-" + mode
        self.create("ServiceAccount", name, automountServiceAccountToken=False)
        reader = manifest("infra/rbac/reader.yaml", "Role")
        self.create("Role", name, rules=reader["rules"])
        subject = [{"kind": "ServiceAccount", "name": name, "namespace": NS}]
        self.create("RoleBinding", name, subjects=subject,
                    roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name})
        nodes = manifest("infra/rbac/reader.yaml", "ClusterRole")
        self.create("ClusterRole", name, namespace=None, rules=nodes["rules"])
        self.create("ClusterRoleBinding", name, namespace=None, subjects=subject,
                    roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": name})
        if mode == "remediator":
            writer = manifest("infra/rbac/remediator.yaml", "Role")
            rules = deepcopy(writer["rules"])
            for rule in rules:
                assert rule["resourceNames"] == ["order-service"]
                rule["resourceNames"] = [self.name]
            self.create("Role", name + "-write", rules=rules)
            self.create("RoleBinding", name + "-write", subjects=subject,
                        roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name + "-write"})
        else:
            rules = []
        token = self.kubectl("create", "token", name, "-n", NS, "--duration=10m").strip()
        admin_config = json.loads(self.kubectl("config", "view", "--raw", "--minify", "--flatten", "-o", "json"))
        cluster = deepcopy(admin_config["clusters"][0])
        if cluster["cluster"].get("insecure-skip-tls-verify"):
            raise RuntimeError("3B requires verified cluster TLS")
        restricted = {"apiVersion": "v1", "kind": "Config", "clusters": [cluster],
            "users": [{"name": name, "user": {"token": token}}],
            "contexts": [{"name": CONTEXT, "context": {"cluster": cluster["name"], "user": name, "namespace": NS}}],
            "current-context": CONTEXT}
        path = self.private / (mode + ".json")
        with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as output:
            json.dump(restricted, output)
        api = api_from_config(path)
        self.apis.append(api)
        username = "system:serviceaccount:" + NS + ":" + name
        who = json.loads(self.kubectl("auth", "whoami", "-o", "json", kubeconfig=path))
        assert who["status"]["userInfo"]["username"] == username
        self.save(mode + "-identity", {"username": username, "reader_rules": reader["rules"], "write_rules": rules})
        deadline = time.monotonic() + 20
        while True:
            try:
                client.CoreV1Api(api).list_namespaced_pod(NS, limit=1, _request_timeout=(3, 5))
                break
            except ApiException as error:
                if error.status != 403 or time.monotonic() >= deadline:
                    raise RuntimeError("test identity permissions did not become available") from None
                time.sleep(0.2)
        return api, path, username

    def request(self, api, subject, label, method, path, expected, *, body=None, query=None, content_type=None):
        status = None
        try:
            value, status, _ = call_json_api(api, path, method, body=body,
                query=query, content_type=content_type)
        except ApiException as error:
            status, value = error.status, None
            if status == 403:
                message = json.loads(error.body or "{}").get("message", "")
                assert subject in message, "403 was not attributed to the expected Kubernetes identity"
        self.results.append({"subject": subject, "case": label, "method": method, "path": path,
                             "expected": expected, "status": status, "layer": "kubernetes"})
        self.save("matrix", self.results)
        assert status == expected, f"{label}: expected HTTP {expected}, got {status}"
        return value

    def clients(self, api):
        return KubernetesClients(core=client.CoreV1Api(api), apps=client.AppsV1Api(api), discovery=client.DiscoveryV1Api(api))

    def close(self):
        failures = []
        for path, uid in reversed(self.created):
            try:
                call_json_api(self.admin, path, "DELETE", body={"apiVersion": "v1", "kind": "DeleteOptions",
                    "preconditions": {"uid": uid}})
            except ApiException as error:
                if error.status != 404:
                    failures.append({"path": path, "status": error.status})
            except Exception as error:
                failures.append({"path": path, "error_type": type(error).__name__})
        for api in self.apis:
            api.close()
        self.admin.close()
        self.save("cleanup", failures)
        assert not failures, "test resource cleanup failed; see cleanup.json"
