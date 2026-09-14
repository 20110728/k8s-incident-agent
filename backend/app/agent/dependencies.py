# 生产依赖装配：将 Kubernetes、服务配置、业务检查、RAG 和模型接入工作流。
# 业务检查采集异常以证据缺失保留，不能据此宣告业务正常。

from backend.app.business_checks.collector import collect_business_checks
from backend.app.service_profiles.registry import collect_profile

from backend.app.agent.collector_adapter import (
    KubernetesCollectorAdapter,
)
from backend.app.tools.client import create_clients
from backend.app.tools.evidence_collector import (
    collect_service_evidence,
)

from backend.app.rag.retriever import (
    PGVectorRunbookRetriever,
)
from backend.app.rag.settings import get_rag_settings
from backend.app.rag.vector_store import (
    build_vector_store,
)

from backend.app.llm.client import (
    build_chat_model,
)
from backend.app.llm.diagnoser import (
    ChatDiagnosisService,
)

from backend.app.llm.remediation_planner import (
    ChatRemediationPlanner,
)

from backend.app.agent.executor import (
    KubernetesRemediationExecutor,
)
from backend.app.agent.verification import (
    KubernetesRecoveryVerifier,
)

def build_kubernetes_collector() -> KubernetesCollectorAdapter:
    clients = create_clients()

    def collect_fn(
        namespace: str,
        service_name: str,
    ):
        bundle = collect_service_evidence(
            clients=clients,
            namespace=namespace,
            service_name=service_name,
        ).model_dump(mode="json")
        bundle["service_profile"] = collect_profile(clients, bundle)
        bundle["business_checks"] = collect_business_checks(clients, bundle)
        for check in bundle["business_checks"]:
            if check["status"] in {"unknown", "skipped"}:
                bundle["errors"].append({
                    "operation": "business_check", "resource_kind": "Service",
                    "resource_name": service_name, "message": check["error_code"],
                    "status_code": None,
                })
        return bundle

    return KubernetesCollectorAdapter(collect_fn)

def build_runbook_retriever() -> (
    PGVectorRunbookRetriever
):
    settings = get_rag_settings()
    vector_store = build_vector_store(settings)

    return PGVectorRunbookRetriever(vector_store)

def build_diagnosis_service() -> (
    ChatDiagnosisService
):
    settings = get_rag_settings()
    model = build_chat_model(settings)

    return ChatDiagnosisService(
        model=model,
        model_name=settings.llm_model,
    )

def build_remediation_planner() -> (
    ChatRemediationPlanner
):
    settings = get_rag_settings()
    model = build_chat_model(settings)

    return ChatRemediationPlanner(
        model=model,
        model_name=settings.llm_model,
    )

def build_remediation_executor() -> (
    KubernetesRemediationExecutor
):
    return KubernetesRemediationExecutor(
        clients=create_clients(),
    )


def build_recovery_verifier():
    from backend.app.agent.business_recovery import BusinessRecoveryVerifier

    return BusinessRecoveryVerifier(
        resource_verifier=KubernetesRecoveryVerifier(clients=create_clients()),
        collector=build_kubernetes_collector(),
    )
