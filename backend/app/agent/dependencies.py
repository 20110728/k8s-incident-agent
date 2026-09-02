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

def build_kubernetes_collector() -> KubernetesCollectorAdapter:
    clients = create_clients()

    def collect_fn(
        namespace: str,
        service_name: str,
    ):
        return collect_service_evidence(
            clients=clients,
            namespace=namespace,
            service_name=service_name,
        )

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