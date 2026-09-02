from operator import add
from typing import Annotated, Any, TypedDict


class IncidentState(TypedDict, total=False):
    # 请求身份
    incident_id: str
    request: dict[str, str]

    # 工作流控制
    phase: str
    valid: bool
    error_count: int

    # 证据采集
    collection_plan: list[str]
    evidence: list[dict[str, Any]]

    # RAG
    retrieval_query: str
    retrieved_runbooks: list[dict[str, Any]]

    # 诊断和修复
    diagnosis: dict[str, Any] | None
    remediation_plan: dict[str, Any] | None
    risk_level: str | None

    # 审批
    requires_approval: bool
    approved: bool | None

    # 执行和验证
    action_result: dict[str, Any] | None
    verification_result: dict[str, Any] | None

    # 允许节点追加内容
    errors: Annotated[list[dict[str, Any]], add]
    trace: Annotated[list[dict[str, Any]], add]

    llm_model: str
    llm_usage: dict[str, int]
