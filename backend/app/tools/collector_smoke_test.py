import argparse

from backend.app.tools.client import create_clients
from backend.app.tools.evidence_collector import (
    collect_service_evidence,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Kubernetes service evidence.")

    parser.add_argument(
        "--namespace",
        default="agent-demo",
    )

    parser.add_argument(
        "--service",
        default="order-service",
    )

    arguments = parser.parse_args()

    clients = create_clients()

    bundle = collect_service_evidence(
        clients=clients,
        namespace=arguments.namespace,
        service_name=arguments.service,
    )

    print(bundle.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
