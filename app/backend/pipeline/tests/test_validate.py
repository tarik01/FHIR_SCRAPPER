import json

from django.test import TestCase, override_settings
from django.utils import timezone

from clinical.models import Observation, Patient
from pipeline.models import ExportFile, ExportJob, RawResource
from pipeline.services.transform import TransformEngine
from pipeline.services.validate import ValidationEngine, ValidationError

BASE = "https://fhir.example.test/baseR4"


def patient(source_id):
    return {
        "resourceType": "Patient", "id": source_id,
        "meta": {"versionId": "1", "lastUpdated": "2026-10-01T10:00:00.000-04:00"},
        "name": [{"given": ["Test"], "family": f"Synthetic{source_id}"}], "birthDate": "1980",
    }


def observation(source_id, subject):
    return {
        "resourceType": "Observation", "id": source_id,
        "meta": {"versionId": "1", "lastUpdated": "2026-10-01T10:00:00.000-04:00"},
        "status": "final", "subject": {"reference": subject},
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
        "valueQuantity": {"value": 0.41177063024789895, "unit": "score"},
        "effectiveDateTime": "2026-01-02",
    }


@override_settings(FHIR_BASE_URL=BASE)
class ValidationEngineTests(TestCase):
    def setUp(self):
        resources = [patient("p1"), patient("p2"), observation("o1", "Patient/p1"), observation("o2", "Location/1")]
        self.job = ExportJob.objects.create(
            job_id="job-1", source_base_url=BASE, request_url="x", status=ExportJob.Status.COMPLETED,
            kicked_off_at=timezone.now(), completed_at=timezone.now(),
            expected_counts={"Patient": 2, "Observation": 2}, transaction_time="2026-10-08T14:06:03.109-04:00",
        )
        for resource_type in ("Patient", "Observation"):
            batch = [r for r in resources if r["resourceType"] == resource_type]
            export_file = ExportFile.objects.create(
                job=self.job, resource_type=resource_type, url=resource_type, status="done", line_count=len(batch)
            )
            for line_no, resource in enumerate(batch, 1):
                RawResource.objects.create(
                    resource_type=resource_type, source_id=resource["id"], version_id="1",
                    last_updated=resource["meta"]["lastUpdated"], payload=json.dumps(resource),
                    payload_sha256=resource["id"], file=export_file, line_no=line_no,
                )
        TransformEngine().run()

    def checks(self, report):
        return {check["name"]: check["passed"] for check in report.checks}

    def test_clean_migration_passes_every_check(self):
        report = ValidationEngine(sample_rate=1.0, seed=1).run()
        self.assertTrue(report.passed, report.checks)
        self.assertEqual(report.totals["quarantined"], {"Patient": 0, "Observation": 1})
        self.assertEqual(report.sample["checked"], {"Patient": 2, "Observation": 2})

    def test_tampered_value_is_caught_by_the_sample(self):
        Observation.objects.update(value_text="99")
        report = ValidationEngine(sample_rate=1.0, seed=1).run()
        self.assertFalse(report.passed)
        self.assertFalse(self.checks(report)["sample_mismatches"])
        self.assertEqual(report.sample["mismatches"][0]["field"], "value_text")

    def test_missing_row_breaks_the_accounting(self):
        Patient.objects.get(source_id="p2").delete()
        report = ValidationEngine(sample_rate=1.0, seed=1).run()
        self.assertFalse(self.checks(report)["loaded + quarantined == extracted (Patient)"])

    def test_count_difference_with_the_source_fails(self):
        ExportJob.objects.update(expected_counts={"Patient": 3, "Observation": 2})
        report = ValidationEngine(sample_rate=1.0, seed=1).run()
        self.assertFalse(self.checks(report)["export_lines(Patient)"])

    def test_requires_a_completed_export(self):
        ExportJob.objects.update(status=ExportJob.Status.IN_PROGRESS)
        with self.assertRaises(ValidationError):
            ValidationEngine().run()
