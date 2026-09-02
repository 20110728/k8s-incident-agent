from pprint import pprint

from backend.app.agent.dependencies import (
    build_kubernetes_collector,
    build_runbook_retriever,
)
from backend.app.agent.graph import build_incident_graph


collector = build_kubernetes_collector()
retriever = build_runbook_retriever()

graph = build_incident_graph(
    collector=collector,
    retriever=retriever,
)

result = graph.invoke(
    {
        "request": {
            "namespace": "agent-demo",
            "service_name": "order-service",
            "description": (
                "order-service无法访问，请检索相关处理手册"
            ),
        }
    }
)

pprint(
    {
        "incident_id": result.get("incident_id"),
        "phase": result.get("phase"),
        "evidence_count": len(
            result.get("evidence", [])
        ),
        "retrieval_query": result.get(
            "retrieval_query"
        ),
        "retrieved_runbooks": result.get(
            "retrieved_runbooks"
        ),
        "errors": result.get("errors", []),
        "trace": result.get("trace", []),
    }
)