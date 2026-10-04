"""Statically validate frozen 0B files. No app imports, model, DB or cluster I/O.

Exit 0: static contract checks passed. Exit 1: invalid contract/files.
Exit 2: invalid CLI arguments. This is not an end-to-end scenario runner.
"""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "evals/cases/complex-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def suite_file(name, directory):
    path = (SUITE / name).resolve()
    require(path.parent == (SUITE / directory).resolve(), f"Out-of-scope path: {name}")
    require(path.is_file(), f"Missing file: {name}")
    return path


def validate():
    catalog = read(SUITE / "catalog.json")
    require(catalog["suite_version"] == "complex-v1", "Unexpected suite")
    require(catalog["suite_revision"] == 1, "Review validator when revising suite")
    cases = catalog["cases"]
    ids = [c["case_id"] for c in cases]
    expected_ids = [f"C{i:02}" for i in range(1, 13)] + [f"R{i:02}" for i in range(1, 7)]
    require(sorted(ids) == expected_ids, "Missing/duplicate case IDs")
    payload_paths = set()
    snapshots = 0
    for case in cases:
        cid = case["case_id"]
        require(case["split"] == "development", f"{cid}: no holdout data delivered")
        require(case["full_scenario_runnable"] is False, f"{cid}: runner not implemented")
        require(bool(case["requires"]), f"{cid}: missing implementation prerequisites")
        require(case["levels"] and set(case["levels"]) <= {f"L{i}" for i in range(6)},
                f"{cid}: invalid acceptance levels")
        input_path = suite_file(case["input_file"], "inputs")
        expected_path = suite_file(case["expected_file"], "expected")
        require(input_path not in payload_paths and expected_path not in payload_paths,
                f"{cid}: reused input/expected path")
        payload_paths.update((input_path, expected_path))
        payload = read(input_path)
        require(set(payload) == {"request"}, f"{cid}: input must contain only request")
        request = payload["request"]
        require(set(request) == {"namespace", "service_name", "description"},
                f"{cid}: unexpected request fields")
        require(request["namespace"] == "agent-demo" and request["service_name"] == "order-service",
                f"{cid}: input target outside registered scene")
        require(isinstance(request["description"], str) and 1 <= len(request["description"]) <= 1000,
                f"{cid}: missing/oversized user input")
        expected = read(expected_path)
        require(expected["case_id"] == cid, f"{cid}: wrong expected file")
        require(all(expected.get(k) for k in ("required_outcome", "forbidden_behaviors", "required_evidence")),
                f"{cid}: incomplete scoring contract")
        require(set(expected["global_blockers"]) == {
            "unauthorized_write", "stale_approval_used", "history_overwritten", "false_recovery_claim",
        }, f"{cid}: missing blocking safety criterion")
        if cid.startswith("C"):
            require(case["reserved_variants"] == [f"{cid}-dev-01", f"{cid}-holdout-01", f"{cid}-holdout-02"],
                    f"{cid}: variant allocation changed")
        else:
            require(case["minimum_repeats_per_window"] == 3, f"{cid}: recovery repeat contract changed")
        if "observation_file" in case:
            require(case["availability"] == "initial_snapshot_only", f"{cid}: invalid availability")
            snapshot = read(suite_file(case["observation_file"], "observations"))
            require(set(snapshot) == {"request", "service_profile", "evidence", "retrieved_runbooks"},
                    f"{cid}: unexpected observation fields (truth/stubs must stay out)")
            require(isinstance(snapshot["evidence"], list) and snapshot["evidence"],
                    f"{cid}: empty observation")
            source = (ROOT / case["source_fixture"]).resolve()
            require(source.parent == (ROOT / "evals/cases/v02-stage5").resolve(),
                    f"{cid}: unexpected source")
            require(digest(source) == case["source_sha256_lf"], f"{cid}: baseline source changed")
            original = read(source)["state"]
            require(snapshot == {k: original[k] for k in snapshot}, f"{cid}: snapshot differs from declared source")
            snapshots += 1
        else:
            require(case["availability"] == "request_only", f"{cid}: missing snapshot")
    require(snapshots == 4, "Expected four development initial snapshots")
    budget = read(SUITE / "budget.json")
    require(budget["real_model_enabled"] is False and budget["max_cost_usd"] == 0,
            "0B must not enable paid model execution")
    require(all(type(v) is int and v > 0 for v in budget["per_run"].values()), "Invalid budget ceiling")
    checksums = read(SUITE / "SHA256SUMS.json")
    actual = {p.relative_to(SUITE).as_posix(): digest(p)
              for p in SUITE.rglob("*.json") if p.name != "SHA256SUMS.json"}
    require(actual == checksums, "Frozen manifest mismatch: missing, extra or changed JSON file")
    return len(cases), snapshots, len(checksums)


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        cases, snapshots, files = validate()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"FAIL: {error}")
        return 1
    print(f"PASS: {cases} case contracts; {snapshots} initial snapshots; {files} frozen JSON files")
    print("Scope: static only; full scenario runner, holdout inputs and live acceptance are pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
