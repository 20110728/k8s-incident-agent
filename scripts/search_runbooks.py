import argparse
from pprint import pprint

from backend.app.agent.dependencies import (
    build_runbook_retriever,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query")
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    retriever = build_runbook_retriever()
    results = retriever.retrieve(
        query=args.query,
        k=args.top_k,
    )

    pprint(results)


if __name__ == "__main__":
    main()