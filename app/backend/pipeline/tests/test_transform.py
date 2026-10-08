import json

from django.test import TestCase, override_settings
from django.utils import timezone

from clinical.models import Observation, Patient, PatientDuplicateCandidate, PatientIdentifier
from pipeline.models import ExportFile, ExportJob, Quarantine, RawResource
from pipeline.services.transform import TransformEngine, TransformError

BASE = "https://fhir.example.test/baseR4"


def patient(source_id, family="Synthetic", version="1", last_updated="2026-10-01T10:00:00.000-04:00", **extra):
    return {
        "resourceType": "Patient", "id": source_id,
        "meta": {"versionId": version, "lastUpdated": last_updated},
        "name": [{"use": "official", "given": ["Test"], "family": family}],
        "birthDate": "1980-01-01", "gender": "female", **extra,
    }


def observation(source_id, subject, version="1"):
    return {
        "resourceType": "Observation", "id": source_id,
        "meta": {"versionId": version, "lastUpdated": "2026-10-01T10:00:00.000-04:00"},
        "status": "final", "subject": {"reference": subject},
        "code": {"coding": [{"system": "http://loinc.org", "code": "8867-4"}]},
        "valueQuantity": {"value": 72, "unit": "/min"},
        "effectiveDateTime": "2026-01-02T10:00:00Z",
    }


@override_settings(FHIR_BASE_URL=BASE)
class TransformTests(TestCase):
    def setUp(self):
        self.line = 0
        self.job = ExportJob.objects.create(
            job_id="job-1", source_base_url=BASE, request_url="x", status=ExportJob.Status.COMPLETED,
            kicked_off_at=timezone.now(),
        )
        self.file = ExportFile.objects.create(job=self.job, resource_type="Any", url="x", status="done")

    def raw(self, resource):
        self.line += 1
        RawResource.objects.create(
            resource_type=resource["resourceType"], source_id=resource["id"],
            version_id=resource["meta"]["versionId"], last_updated=resource["meta"]["lastUpdated"],
            payload=json.dumps(resource), payload_sha256=str(self.line), file=self.file, line_no=self.line,
        )

    def test_requires_a_completed_export(self):
        ExportJob.objects.update(status=ExportJob.Status.IN_PROGRESS)
        with self.assertRaises(TransformError):
            TransformEngine().run()

    def test_patients_and_observations_are_loaded_or_quarantined(self):
        self.raw(patient("p1", identifier=[{"system": "urn:mrn", "value": "M1"}]))
        self.raw(patient("p2", birthDate="5032-04-12"))
        self.raw(observation("o1", "Patient/p1"))
        self.raw(observation("o2", "Location/9"))
        self.raw(observation("o3", "Patient/p2"))
        summary = TransformEngine().run()
        self.assertEqual(summary["patients"], {"inserted": 1, "quarantined": 1})
        self.assertEqual(summary["observations"], {"inserted": 1, "quarantined": 2})
        self.assertEqual(PatientIdentifier.objects.get().value, "M1")
        self.assertEqual(
            set(Quarantine.objects.values_list("source_id", "reason")),
            {("p2", "birthDate in the future"), ("o2", "subject is not a Patient (Location)"),
             ("o3", "subject patient not loaded")},
        )
        self.assertEqual(Observation.objects.get().patient.source_id, "p1")

    def test_rerun_is_idempotent(self):
        self.raw(patient("p1"))
        self.raw(observation("o1", "Patient/p1"))
        TransformEngine().run()
        summary = TransformEngine().run()
        self.assertEqual(summary["patients"], {"unchanged": 1})
        self.assertEqual(summary["observations"], {"unchanged": 1})
        self.assertEqual((Patient.objects.count(), Observation.objects.count()), (1, 1))

    def test_new_version_updates_the_same_row(self):
        self.raw(patient("p1", family="Silva"))
        TransformEngine().run()
        internal_id = Patient.objects.get().id
        self.raw(patient("p1", family="Souza", version="2", last_updated="2026-10-05T10:00:00.000-04:00"))
        summary = TransformEngine().run()
        updated = Patient.objects.get()
        self.assertEqual(summary["patients"], {"updated": 1})
        self.assertEqual((updated.id, updated.family_name, updated.source_version), (internal_id, "Souza", "2"))

    def test_older_version_never_overwrites_newer(self):
        self.raw(patient("p1", family="Souza", version="3", last_updated="2026-10-05T10:00:00.000-04:00"))
        TransformEngine().run()
        Patient.objects.update(source_version="stale-marker")
        RawResource.objects.all().delete()
        self.raw(patient("p1", family="Silva", version="2", last_updated="2026-10-01T10:00:00.000-04:00"))
        summary = TransformEngine().run()
        self.assertEqual(summary["patients"], {"stale_skipped": 1})
        self.assertEqual(Patient.objects.get().family_name, "Souza")

    def test_identifiers_are_replaced_on_update(self):
        self.raw(patient("p1", identifier=[{"system": "urn:a", "value": "1"}]))
        TransformEngine().run()
        self.raw(patient("p1", version="2", last_updated="2026-10-05T10:00:00.000-04:00",
                         identifier=[{"system": "urn:b", "value": "2"}]))
        TransformEngine().run()
        self.assertEqual(list(PatientIdentifier.objects.values_list("system", "value")), [("urn:b", "2")])

    def test_duplicate_candidates_are_reported_not_merged(self):
        self.raw(patient("p1", identifier=[{"system": "urn:mrn", "value": "SAME"}]))
        self.raw(patient("p2", identifier=[{"system": "urn:mrn", "value": "SAME"}]))
        summary = TransformEngine().run()
        self.assertEqual(summary["duplicate_candidates"],
                         {"shared identifier": 1, "same name + birth date + gender": 1})
        self.assertEqual(Patient.objects.count(), 2)
        self.assertEqual(PatientDuplicateCandidate.objects.filter(status="open").count(), 2)

    def test_rebuild_clears_and_reloads(self):
        self.raw(patient("p1"))
        TransformEngine().run()
        summary = TransformEngine().run(rebuild=True)
        self.assertEqual(summary["patients"], {"inserted": 1})
