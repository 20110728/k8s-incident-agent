import argparse

from pprint import pprint

from backend.app.agent.dependencies import (
    build_diagnosis_service,
    build_kubernetes_collector,
    build_runbook_retriever,
)
from backend.app.agent.graph import (
    build_incident_graph,
)


def main() -> None:
    
    parser = argparse.ArgumentParser(
        description="Run Kubernetes incident diagnosis."
    )
    parser.add_argument(
        "--namespace",
        default="agent-demo",
    )
    parser.add_argument(
        "--service",
        default="order-service",
    )
    parser.add_argument(
        "--description",
        required=True,
    )
    args = parser.parse_args()

    graph = build_incident_graph(
        collector=build_kubernetes_collector(),
        retriever=build_runbook_retriever(),
        diagnoser=build_diagnosis_service(),
    )

    result = graph.invoke(
        {
            "request": {
                "namespace": args.namespace,
                "service_name": args.service,
                "description": args.description,
            }
        }
    )

    pprint(
        {
            "incident_id": result.get(
                "incident_id"
            ),
            "phase": result.get("phase"),
            "diagnosis": result.get(
                "diagnosis"
            ),
            "llm_model": result.get(
                "llm_model"
            ),
            "llm_usage": result.get(
                "llm_usage"
            ),
            "errors": result.get(
                "errors",
                [],
            ),
            "trace": result.get(
                "trace",
                [],
            ),
        }
    )


if __name__ == "__main__":
    main()