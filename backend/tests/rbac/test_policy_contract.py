"""Manifest checks, also included in ECS acceptance before testing identities."""
from backend.tests.rbac.lab import manifest


def test_reader_and_remediator_have_distinct_write_boundaries():
    reader = manifest("infra/rbac/reader.yaml", "Role")
    assert all(set(rule["verbs"]) <= {"get", "list", "watch"} for rule in reader["rules"])
    assert all("*" not in rule["resources"] and "secrets" not in rule["resources"] for rule in reader["rules"])
    proxy = next(rule for rule in reader["rules"] if rule["resources"] == ["services/proxy"])
    assert proxy["resourceNames"] == ["incident-agent-business-probe", "incident-agent-business-probe:80"]
    assert proxy["verbs"] == ["get"]
    writer = manifest("infra/rbac/remediator.yaml", "Role")
    assert len(writer["rules"]) == 2
    assert {tuple(rule["resources"]) for rule in writer["rules"]} == {("services",), ("deployments",)}
    for rule in writer["rules"]:
        assert rule["resourceNames"] == ["order-service"]
        assert set(rule["verbs"]) == {"get", "patch"}
    nodes = manifest("infra/rbac/reader.yaml", "ClusterRole")
    assert nodes["rules"] == [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get"]}]


def test_existing_checker_manifest_agrees_with_reader_proxy_scope():
    checker = manifest("infra/business-probe/baseline.yaml", "Role")
    reader = manifest("infra/rbac/reader.yaml", "Role")
    proxy = next(rule for rule in reader["rules"] if rule["resources"] == ["services/proxy"])
    assert checker["rules"] == [proxy]
