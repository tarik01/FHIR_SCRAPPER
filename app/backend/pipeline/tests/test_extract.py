import json

import httpx
from django.test import TransactionTestCase, override_settings

from pipeline.models import ExportFile, ExportJob, RawResource
from pipeline.services.extract import BulkExportExtractor
from pipeline.services.fhir_client import FhirClient

BASE = "https://fhir.example.test/baseR4"
POLL_URL = f"{BASE}/$export-poll-status?_jobId=job-1"


def poll_url(job_number):
    return f"{BASE}/$export-poll-status?_jobId=job-{job_number}"


def patient(source_id, version="1"):
    return {
        "resourceType": "Patient",
        "id": source_id,
        "meta": {"versionId": version, "lastUpdated": "2026-10-01T10:00:00Z"},
        "name": [{"family": "Synthetic", "given": ["Test"]}],
    }


def observation(source_id, patient_id):
    return {
        "resourceType": "Observation",
        "id": source_id,
        "meta": {"versionId": "1", "lastUpdated": "2026-10-01T10:00:00Z"},
        "status": "final",
        "subject": {"reference": f"Patient/{patient_id}"},
    }


def ndjson(*resources):
    return "\n".join(json.dumps(r) for r in resources) + "\n"


class FakeSleep:
    async def __call__(self, seconds):
        return None


class FakeExportServer:
    def __init__(self, files):
        self.files = files
        self.polls_before_ready = 1
        self.kick_offs = 0

    def handler(self, request):
        path = request.url.path
        if path.endswith("/$export"):
            self.kick_offs += 1
            return httpx.Response(202, headers={"Content-Location": poll_url(self.kick_offs)})
        if path.endswith("/$export-poll-status"):
            if self.polls_before_ready:
                self.polls_before_ready -= 1
                return httpx.Response(202, headers={"X-Progress": "IN_PROGRESS", "Retry-After": "120"})
            return httpx.Response(200, json={
                "transactionTime": "2026-10-08T14:06:03.109-04:00",
                "request": f"{BASE}/$export?_type=Patient,Observation",
                "requiresAccessToken": True,
                "output": [{"type": t, "url": f"{BASE}/Binary/{name}"} for name, (t, _) in self.files.items()],
                "error": [],
            })
        if path.startswith("/baseR4/Binary/"):
            _, body = self.files[path.rsplit("/", 1)[1]]
            return httpx.Response(200, content=body.encode(), headers={"Content-Type": "application/fhir+ndjson"})
        if path in ("/baseR4/Patient", "/baseR4/Observation"):
            resource_type = path.rsplit("/", 1)[1]
            total = sum(body.count("\n") for t, body in self.files.values() if t == resource_type)
            return httpx.Response(200, json={"resourceType": "Bundle", "total": total})
        return httpx.Response(404)

    def client(self):
        transport = httpx.MockTransport(self.handler)
        return FhirClient(BASE, http=httpx.AsyncClient(transport=transport), sleep=FakeSleep())


@override_settings(FHIR_BASE_URL=BASE)
class RemoteExtractTests(TransactionTestCase):
    def server(self):
        return FakeExportServer({
            "p1": ("Patient", ndjson(patient("1"), patient("2"))),
            "o1": ("Observation", ndjson(observation("10", "1"), observation("11", "2"), observation("12", "2"))),
        })

    async def test_full_export_lands_in_raw_store(self):
        server = self.server()
        summary = await BulkExportExtractor(client=server.client()).run()
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["lines"], {"Patient": 2, "Observation": 3})
        self.assertEqual(summary["expected_counts"], {"Patient": 2, "Observation": 3})
        self.assertEqual(await RawResource.objects.acount(), 5)
        job = await ExportJob.objects.aget()
        self.assertEqual(job.job_id, "job-1")
        self.assertEqual(job.transaction_time, "2026-10-08T14:06:03.109-04:00")

    async def test_interrupted_job_is_resumed_without_new_kick_off(self):
        server = self.server()
        await ExportJob.objects.acreate(
            job_id="job-1", source_base_url=BASE, request_url=f"{BASE}/$export",
            poll_url=POLL_URL, kicked_off_at="2026-10-08T18:00:00Z",
        )
        summary = await BulkExportExtractor(client=server.client()).run()
        self.assertEqual(server.kick_offs, 0)
        self.assertEqual(summary["status"], "completed")

    async def test_new_export_does_not_duplicate_resources(self):
        server = self.server()
        await BulkExportExtractor(client=server.client()).run()
        await BulkExportExtractor(client=server.client()).run()
        self.assertEqual(server.kick_offs, 2)
        self.assertEqual(await RawResource.objects.acount(), 5)
        second_job = ExportFile.objects.filter(job__job_id="job-2")
        self.assertEqual(await second_job.filter(inserted_count=0).acount(), 2)
        self.assertEqual(await second_job.exclude(duplicate_count=0).acount(), 2)

    async def test_invalid_file_fails_alone(self):
        server = FakeExportServer({
            "p1": ("Patient", ndjson(patient("1"))),
            "p2": ("Patient", ndjson(patient("2")) + "{not json\n"),
        })
        summary = await BulkExportExtractor(client=server.client()).run()
        self.assertEqual(summary["files_done"], 1)
        self.assertEqual(summary["status"], "in_progress")
        broken = await ExportFile.objects.aget(status=ExportFile.Status.FAILED)
        self.assertIn("not valid JSON", broken.last_error)
        self.assertEqual(await RawResource.objects.acount(), 1)
