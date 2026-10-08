import uuid

from django.test import TestCase
from django.utils import timezone

from clinical.models import Observation, Patient, PatientIdentifier
from pipeline.models import RunReport


def make_patient(source_id, family, given="Test"):
    return Patient.objects.create(
        source_system="https://fhir.example.test/baseR4", source_id=source_id, source_version="1",
        given_name=given, family_name=family, birth_date="1980", birth_date_precision="year",
        gender=None, mapping_version="2", migrated_at=timezone.now(),
    )


def make_observation(patient, source_id, status="final", value="72"):
    return Observation.objects.create(
        source_system=patient.source_system, source_id=source_id, source_version="1", patient=patient,
        status=status, code="8867-4", code_display="Heart rate", value_type="Quantity", value_text=value,
        value_unit="/min", effective_start=timezone.now(), effective_precision="second",
        mapping_version="2", migrated_at=timezone.now(),
    )


class PatientApiTests(TestCase):
    def setUp(self):
        self.alice = make_patient("p1", "Synthetic", "Alice")
        self.bruno = make_patient("p2", "Example", "Bruno")
        PatientIdentifier.objects.create(patient=self.alice, system="urn:mrn", value="M1", type_code="MR")
        make_observation(self.alice, "o1")
        make_observation(self.alice, "o2", status="entered-in-error")

    def test_list_returns_every_patient_named_first(self):
        Patient.objects.create(
            source_system=self.alice.source_system, source_id="p3", source_version="1",
            mapping_version="2", migrated_at=timezone.now(),
        )
        body = self.client.get("/api/patients").json()
        self.assertEqual(body["count"], 3)
        self.assertEqual([p["source_id"] for p in body["results"]], ["p2", "p1", "p3"])

    def test_search_by_name(self):
        body = self.client.get("/api/patients?q=alice").json()
        self.assertEqual([p["source_id"] for p in body["results"]], ["p1"])
        self.assertEqual(body["results"][0]["observation_count"], 2)

    def test_detail_includes_identifiers(self):
        body = self.client.get(f"/api/patients/{self.alice.id}").json()
        self.assertEqual(body["identifiers"], [{"system": "urn:mrn", "value": "M1", "type_code": "MR"}])
        self.assertIsNone(body["gender"])

    def test_unknown_patient_is_404(self):
        self.assertEqual(self.client.get(f"/api/patients/{uuid.uuid4()}").status_code, 404)

    def test_invalid_ids_and_unknown_endpoints_return_json_404(self):
        for url in ("/api/patients/not-a-uuid", "/api/nothing-here"):
            response = self.client.get(url)
            self.assertEqual((response.status_code, response["Content-Type"]), (404, "application/json"))

    def test_write_methods_are_not_allowed(self):
        self.assertEqual(self.client.post("/api/patients").status_code, 405)

    def test_entered_in_error_is_hidden_by_default(self):
        body = self.client.get(f"/api/patients/{self.alice.id}/observations").json()
        self.assertEqual((body["count"], body["entered_in_error"]), (1, 1))
        everything = self.client.get(f"/api/patients/{self.alice.id}/observations?include_errors=1").json()
        self.assertEqual(everything["count"], 2)

    def test_latest_report(self):
        self.assertEqual(self.client.get("/api/runs/latest").status_code, 404)
        RunReport.objects.create(passed=True, checks=[], totals={}, sample={})
        self.assertTrue(self.client.get("/api/runs/latest").json()["passed"])
