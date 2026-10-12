"""Frozen synthetic baseline contracts; no graph invocation or external I/O.

Also runnable without pytest: python -m unittest
backend.tests.compatibility.test_stage0a_samples -v
"""
import copy
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from backend.app.agent.approval import build_approval_request
from backend.app.agent.diagnosis_policy import diagnostic_facts
from backend.app.api.schemas import IncidentStatusResponse
from backend.app.services.recheck_service import RecheckResult

SAMPLES = Path(__file__).resolve().parents[3] / "evals/compatibility/stage0a"


def load(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


class LegacyContracts(unittest.TestCase):
    def test_ten_legacy_fixture_facts_without_graph_dependencies(self):
        cases = SAMPLES.parents[1] / "cases/v02-stage5"
        catalog = json.loads((cases / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(len(catalog["cases"]), 10)
        for case in catalog["cases"]:
            with self.subTest(case=case["case_id"]):
                fixture = json.loads((cases / case["fixture"]).read_text(encoding="utf-8"))
                facts = diagnostic_facts(fixture["state"])
                for field, expected in case["expected_facts"].items():
                    self.assertEqual(facts.get(field), expected, field)

    def test_pending_approval_binding_and_read_only_roundtrip(self):
        raw = load("awaiting_approval.json")
        before = copy.deepcopy(raw)
        response = IncidentStatusResponse.model_validate(raw)
        self.assertTrue(response.waiting_for_approval)
        self.assertEqual(response.incident_id, response.thread_id)
        self.assertIsNone(response.approval_record)
        self.assertIsNone(response.approved)
        self.assertEqual(build_approval_request(raw), response.approval_request)
        self.assertEqual(raw, before)
        self.assertEqual(IncidentStatusResponse.model_validate_json(
            response.model_dump_json()), response)

    def test_old_resource_success_does_not_imply_business_success(self):
        response = IncidentStatusResponse.model_validate(load("terminal.json"))
        self.assertEqual(response.phase, "verification_succeeded")
        self.assertFalse(response.waiting_for_approval)
        self.assertEqual(response.verification_result.verification_scope, "resource_only")
        self.assertEqual(response.verification_result.business_status, "skipped")
        self.assertTrue(response.verification_result.unverified_scope)

    def test_saved_recheck_is_independent_and_unattributed(self):
        raw = load("recheck.json")
        before = copy.deepcopy(raw)
        result = RecheckResult.model_validate(raw)
        terminal = load("terminal.json")
        self.assertEqual(result.incident_id, terminal["incident_id"])
        self.assertNotEqual(result.recheck_id, result.incident_id)
        self.assertEqual(result.recovery_attribution, "not_established")
        self.assertFalse(result.cluster_writes_executed)
        self.assertFalse(result.model_called)
        self.assertEqual(raw, before)
        self.assertEqual(RecheckResult.model_validate_json(result.model_dump_json()), result)

    def test_changed_plan_cannot_reuse_frozen_approval(self):
        raw = load("awaiting_approval.json")
        frozen_id = raw["approval_request"]["approval_id"]
        raw["remediation_plan"]["parameters"]["proposed_probe_path"] = "/changed"
        self.assertNotEqual(build_approval_request(raw).approval_id, frozen_id)

    def test_recheck_rejects_claimed_write_or_model_call(self):
        for field in ("cluster_writes_executed", "model_called"):
            with self.subTest(field=field):
                raw = load("recheck.json")
                raw[field] = True
                with self.assertRaises(ValidationError):
                    RecheckResult.model_validate(raw)


if __name__ == "__main__":
    unittest.main()
