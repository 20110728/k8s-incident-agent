"""Manual app behavior acceptance via Pod loopback, NOT Service routing evidence."""
import argparse
import json
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener

PATHS = ("/livez", "/readyz", "/api/orders/demo-001")
EXPECTED_STATUSES = {
    "normal": [200, 200, 200],
    "api500": [200, 200, 500],
    "wrong_content": [200, 200, 200],
    "dependency_unavailable": [200, 503, 503],
}


def inspect(base_url="http://127.0.0.1:8080"):
    opener = build_opener(ProxyHandler({}))
    results = []
    for path in PATHS:
        try:
            response = opener.open(base_url + path, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            results.append({"path": path, "http_status": response.code,
                            "body": json.loads(response.read(16384))})
    return results


def matches(results, scenario):
    if [item["http_status"] for item in results] != EXPECTED_STATUSES[scenario]:
        return False
    body = results[2]["body"]
    business_matches = (body.get("order_id") == "demo-001"
                        and body.get("status") == "confirmed"
                        and body.get("dependency_status") == "available")
    if scenario == "normal":
        return business_matches
    if scenario == "wrong_content":
        return not business_matches and body.get("order_id") == "wrong-order"
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect", choices=list(EXPECTED_STATUSES), required=True)
    args = parser.parse_args()
    results = inspect()
    passed = matches(results, args.expect)
    print(json.dumps({"verification_scope": "pod_loopback_only",
                      "service_routing_verified": False,
                      "scenario": args.expect, "scenario_matches": passed,
                      "responses": results}, ensure_ascii=False, indent=2))
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
