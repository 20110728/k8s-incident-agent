import argparse
from pathlib import Path

from backend.app.diagnosis.engine import diagnose_service
from backend.app.tools.client import create_clients


def main() -> None:
    parser = argparse.ArgumentParser(
        description=("Diagnose a Kubernetes Service without an LLM.")
    )

    parser.add_argument(
        "--namespace",
        default="agent-demo",
    )

    parser.add_argument(
        "--service",
        required=True,
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )

    arguments = parser.parse_args()

    clients = create_clients()

    result = diagnose_service(
        clients=clients,
        namespace=arguments.namespace,
        service_name=arguments.service,
    )

    rendered = result.model_dump_json(indent=2)

    print(rendered)

    if arguments.output is not None:
        arguments.output.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        arguments.output.write_text(
            rendered + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
