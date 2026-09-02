from typing import Any

from pydantic import BaseModel, Field


class ServicePortInfo(BaseModel):
    name: str | None = None
    port: int
    target_port: str | int | None = None
    protocol: str = "TCP"


class ServiceInfo(BaseModel):
    namespace: str
    name: str
    service_type: str
    cluster_ip: str | None = None
    selector: dict[str, str] = Field(default_factory=dict)
    ports: list[ServicePortInfo] = Field(default_factory=list)


class EndpointInfo(BaseModel):
    addresses: list[str] = Field(default_factory=list)
    ready: bool | None = None
    serving: bool | None = None
    terminating: bool | None = None
    node_name: str | None = None
    target_kind: str | None = None
    target_name: str | None = None


class EndpointSliceInfo(BaseModel):
    namespace: str
    name: str
    service_name: str
    address_type: str
    endpoints: list[EndpointInfo] = Field(default_factory=list)


class ContainerStatusInfo(BaseModel):
    name: str
    ready: bool
    restart_count: int
    image: str
    state: str
    waiting_reason: str | None = None
    waiting_message: str | None = None
    terminated_reason: str | None = None
    terminated_exit_code: int | None = None
    last_terminated_reason: str | None = None
    last_terminated_exit_code: int | None = None


class PodInfo(BaseModel):
    namespace: str
    name: str
    phase: str
    ready: bool
    node_name: str | None = None
    pod_ip: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    containers: list[ContainerStatusInfo] = Field(default_factory=list)


class EventInfo(BaseModel):
    namespace: str
    resource_name: str
    event_type: str | None = None
    reason: str | None = None
    message: str | None = None
    count: int | None = None
    first_seen: str | None = None
    last_seen: str | None = None


class PodLogInfo(BaseModel):
    namespace: str
    pod_name: str
    container_name: str | None = None
    previous: bool
    truncated: bool
    content: str


class ToolErrorInfo(BaseModel):
    operation: str
    error_type: str
    message: str
    status_code: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class HttpProbeInfo(BaseModel):
    path: str | None = None
    port: str | int | None = None
    scheme: str | None = None


class ContainerConfigInfo(BaseModel):
    name: str
    image: str
    command: list[str] = Field(default_factory=list)
    args: list[str] = Field(default_factory=list)
    requests: dict[str, str] = Field(default_factory=dict)
    limits: dict[str, str] = Field(default_factory=dict)
    readiness_probe: HttpProbeInfo | None = None
    liveness_probe: HttpProbeInfo | None = None


class DeploymentInfo(BaseModel):
    namespace: str
    name: str
    desired_replicas: int
    ready_replicas: int
    available_replicas: int
    unavailable_replicas: int
    selector: dict[str, str] = Field(default_factory=dict)
    template_labels: dict[str, str] = Field(default_factory=dict)
    containers: list[ContainerConfigInfo] = Field(default_factory=list)


class OwnerChainInfo(BaseModel):
    namespace: str
    pod_name: str
    direct_owner_kind: str | None = None
    direct_owner_name: str | None = None
    replica_set_name: str | None = None
    deployment_name: str | None = None


class NodeConditionInfo(BaseModel):
    condition_type: str
    status: str
    reason: str | None = None
    message: str | None = None


class NodeInfo(BaseModel):
    name: str
    ready: bool
    capacity: dict[str, str] = Field(default_factory=dict)
    allocatable: dict[str, str] = Field(default_factory=dict)
    conditions: list[NodeConditionInfo] = Field(default_factory=list)


class CollectionErrorInfo(BaseModel):
    operation: str
    resource_kind: str
    resource_name: str
    message: str
    status_code: int | None = None


class ServiceEvidenceBundle(BaseModel):
    namespace: str
    service_name: str
    service: ServiceInfo | None = None
    service_pod_names: list[str] = Field(default_factory=list)
    namespace_pod_names: list[str] = Field(default_factory=list)
    endpoint_slices: list[EndpointSliceInfo] = Field(default_factory=list)
    pod_statuses: dict[str, PodInfo] = Field(default_factory=dict)
    pod_events: dict[str, list[EventInfo]] = Field(default_factory=dict)
    pod_logs: list[PodLogInfo] = Field(default_factory=list)
    owner_chains: dict[str, OwnerChainInfo] = Field(default_factory=dict)
    deployments: dict[str, DeploymentInfo] = Field(default_factory=dict)
    nodes: dict[str, NodeInfo] = Field(default_factory=dict)
    errors: list[CollectionErrorInfo] = Field(default_factory=list)
