"""Read-only stage-1 acceptance command; does not call an LLM or mutate Kubernetes."""
import argparse
import json

from backend.app.agent.dependencies import build_kubernetes_collector


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="agent-demo")
    parser.add_argument("--service", default="order-service")
    parser.add_argument("--expect", choices=["matched", "mismatch", "unavailable"])
    args = parser.parse_args()
    bundle = build_kubernetes_collector().collect(args.namespace, args.service)
    snapshot = bundle.get("service_profile") or {}
    print(json.dumps({"service_profile": snapshot,
                      "business_checks": bundle.get("business_checks", []),
                      "collection_errors": bundle.get("errors", [])}, ensure_ascii=False, indent=2))
    if args.expect and snapshot.get("status") != args.expect:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
