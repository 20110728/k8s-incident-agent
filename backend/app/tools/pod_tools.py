from datetime import datetime
from typing import Any

from backend.app.schemas.kubernetes import (
    ContainerStatusInfo,
    EventInfo,
    PodInfo,
    PodLogInfo,
)
from backend.app.tools.client import (
    REQUEST_TIMEOUT,
    KubernetesClients,
)

MAX_LOG_LINES = 500
MAX_LOG_CHARACTERS = 20_000


def _to_isoformat(
    value: datetime | None,
) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _get_container_state(
    state: Any,
) -> tuple[
    str,
    str | None,
    str | None,
    str | None,
    int | None,
]:
    if state is None:
        return "unknown", None, None, None, None

    if state.running is not None:
        return "running", None, None, None, None

    if state.waiting is not None:
        return (
            "waiting",
            state.waiting.reason,
            state.waiting.message,
            None,
            None,
        )

    if state.terminated is not None:
        return (
            "terminated",
            None,
            None,
            state.terminated.reason,
            state.terminated.exit_code,
        )

    return "unknown", None, None, None, None


def _pod_is_ready(pod: Any) -> bool:
    for condition in pod.status.conditions or []:
        if condition.type == "Ready":
            return condition.status == "True"
    return False


def get_pod_status(
    clients: KubernetesClients,
    namespace: str,
    pod_name: str,
) -> PodInfo:
    pod = clients.core.read_namespaced_pod(
        name=pod_name,
        namespace=namespace,
        _request_timeout=REQUEST_TIMEOUT,
    )

    containers: list[ContainerStatusInfo] = []

    for status in pod.status.container_statuses or []:
        (
            state_name,
            waiting_reason,
            waiting_message,
            terminated_reason,
            terminated_exit_code,
        ) = _get_container_state(status.state)

        last_terminated_reason = None
        last_terminated_exit_code = None

        if status.last_state is not None and status.last_state.terminated is not None:
            last_terminated_reason = status.last_state.terminated.reason
            last_terminated_exit_code = status.last_state.terminated.exit_code

        containers.append(
            ContainerStatusInfo(
                name=status.name,
                ready=status.ready,
                restart_count=status.restart_count,
                image=status.image,
                state=state_name,
                waiting_reason=waiting_reason,
                waiting_message=waiting_message,
                terminated_reason=terminated_reason,
                terminated_exit_code=terminated_exit_code,
                last_terminated_reason=last_terminated_reason,
                last_terminated_exit_code=(last_terminated_exit_code),
            )
        )

    return PodInfo(
        namespace=namespace,
        name=pod.metadata.name,
        phase=pod.status.phase or "Unknown",
        ready=_pod_is_ready(pod),
        node_name=pod.spec.node_name,
        pod_ip=pod.status.pod_ip,
        labels=pod.metadata.labels or {},
        containers=containers,
    )


def get_pod_events(
    clients: KubernetesClients,
    namespace: str,
    pod_name: str,
) -> list[EventInfo]:
    events = clients.core.list_namespaced_event(
        namespace=namespace,
        field_selector=(f"involvedObject.kind=Pod,involvedObject.name={pod_name}"),
        _request_timeout=REQUEST_TIMEOUT,
    )

    result: list[EventInfo] = []

    for event in events.items:
        first_seen = event.first_timestamp or event.metadata.creation_timestamp
        last_seen = (
            event.last_timestamp
            or event.event_time
            or event.metadata.creation_timestamp
        )

        result.append(
            EventInfo(
                namespace=namespace,
                resource_name=pod_name,
                event_type=event.type,
                reason=event.reason,
                message=event.message,
                count=event.count,
                first_seen=_to_isoformat(first_seen),
                last_seen=_to_isoformat(last_seen),
            )
        )

    return sorted(
        result,
        key=lambda event: event.last_seen or "",
    )


def get_pod_logs(
    clients: KubernetesClients,
    namespace: str,
    pod_name: str,
    container_name: str | None = None,
    previous: bool = False,
    tail_lines: int = 200,
) -> PodLogInfo:
    safe_tail_lines = min(
        max(tail_lines, 1),
        MAX_LOG_LINES,
    )

    content = clients.core.read_namespaced_pod_log(
        name=pod_name,
        namespace=namespace,
        container=container_name,
        previous=previous,
        tail_lines=safe_tail_lines,
        timestamps=True,
        _request_timeout=REQUEST_TIMEOUT,
    )

    truncated = len(content) > MAX_LOG_CHARACTERS

    if truncated:
        content = content[-MAX_LOG_CHARACTERS:]

    return PodLogInfo(
        namespace=namespace,
        pod_name=pod_name,
        container_name=container_name,
        previous=previous,
        truncated=truncated,
        content=content,
    )
