# LangGraph 共享状态契约：节点、审批恢复和持久化共同使用这些字段。
# errors 与 trace 使用追加合并；其余字段由后续节点更新。

from operator import add
from typing import Annotated, Any, TypedDict

from backend.app.agent.schemas import (
    ActionExecutionResult,
    ApprovalRecord,
    ApprovalRequest,
    ApprovalStatus,
    RecoveryVerificationResult,
)

class IncidentState(TypedDict, total=False):
    llm_debug: dict[str, Any]
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
    service_profile: dict[str, Any] | None

    # RAG
    retrieval_query: str
    retrieved_runbooks: list[dict[str, Any]]

    # 诊断
    # 用于审计的模型原文结构；页面正式诊断读取下方 diagnosis。
    diagnosis_model_output: dict[str, Any] | None
    diagnosis: dict[str, Any] | None
    llm_model: str
    llm_usage: dict[str, int]
    diagnosis_retry_count: int
    diagnosis_validation_feedback: str

    # 处置计划
    remediation_plan: dict[str, Any] | None
    risk_level: str | None
    remediation_llm_model: str
    remediation_llm_usage: dict[str, int]

    # 审批
    requires_approval: bool
    approved: bool | None
    approval_status: ApprovalStatus
    approval_request: ApprovalRequest | None
    approval_record: ApprovalRecord | None

    # 执行和验证
    action_result: ActionExecutionResult | None
    verification_result: (
        RecoveryVerificationResult | None
    )

    # 允许节点追加内容
    errors: Annotated[
        list[dict[str, Any]],
        add,
    ]
    trace: Annotated[
        list[dict[str, Any]],
        add,
    ]