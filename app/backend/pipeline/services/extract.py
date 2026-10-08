import asyncio
import hashlib
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from pipeline.models import ExportFile, ExportJob, RawResource
from pipeline.services.fhir_client import (
    CircuitOpen,
    FhirClient,
    FhirError,
    PermanentHttpError,
    RetriesExhausted,
    retry_after_seconds,
)

log = logging.getLogger("pipeline.extract")

FHIR_JSON = "application/fhir+json"
EXPIRED_STATUSES = {404, 410}
FAIL_FAST_HINTS = {
    400: "invalid export request: check _type / _since",
    401: "authentication failed: check credentials",
    403: "authentication failed: check credentials and scopes",
    404: "$export not supported by this server",
    405: "$export not supported by this server",
    501: "$export not supported by this server",
}


class ExtractError(Exception):
    pass


class InvalidFile(Exception):
    pass


@dataclass(frozen=True)
class ParsedLine:
    line_no: int
    source_id: str
    version_id: str
    last_updated: str
    payload: str
    payload_sha256: str


def sha256(text):
    return hashlib.sha256(text.encode()).hexdigest()


def job_id_from(poll_url):
    query = parse_qs(urlsplit(poll_url).query)
    return query.get("_jobId", [None])[0] or sha256(poll_url)[:32]


def parse_ndjson(body, expected_type):
    rows = []
    for line_no, line in enumerate(body.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            resource = json.loads(line)
        except ValueError:
            raise InvalidFile(f"line {line_no}: not valid JSON")
        if resource.get("resourceType") != expected_type:
            raise InvalidFile(
                f"line {line_no}: resourceType {resource.get('resourceType')!r} != {expected_type!r}"
            )
        if not resource.get("id"):
            raise InvalidFile(f"line {line_no}: missing id")
        meta = resource.get("meta") or {}
        digest = sha256(line)
        rows.append(ParsedLine(
            line_no=line_no,
            source_id=resource["id"],
            version_id=meta.get("versionId") or digest,
            last_updated=meta.get("lastUpdated", ""),
            payload=line,
            payload_sha256=digest,
        ))
    return rows


class ExportStore:
    async def find_resumable_job(self):
        return await (
            ExportJob.objects.filter(status=ExportJob.Status.IN_PROGRESS)
            .exclude(poll_url="")
            .order_by("-kicked_off_at")
            .afirst()
        )

    async def create_job(self, poll_url, base_url, request_url):
        return await ExportJob.objects.acreate(
            job_id=job_id_from(poll_url),
            source_base_url=base_url,
            request_url=request_url,
            poll_url=poll_url,
            kicked_off_at=timezone.now(),
        )

    async def mark_job(self, job, status, error=""):
        job.status = status
        job.last_error = error
        await job.asave(update_fields=["status", "last_error"])

    async def complete_job(self, job):
        job.status = ExportJob.Status.COMPLETED
        job.completed_at = timezone.now()
        await job.asave(update_fields=["status", "completed_at"])

    async def save_expected_counts(self, job, counts):
        job.expected_counts = counts
        await job.asave(update_fields=["expected_counts"])

    @sync_to_async
    @transaction.atomic
    def save_manifest(self, job, manifest):
        outputs = manifest.get("output") or []
        job.transaction_time = manifest.get("transactionTime")
        job.manifest_json = manifest
        job.save(update_fields=["transaction_time", "manifest_json"])
        ExportFile.objects.bulk_create(
            [ExportFile(job=job, resource_type=item["type"], url=item["url"]) for item in outputs],
            ignore_conflicts=True,
        )

    async def files_to_download(self, job):
        files = job.files.filter(
            status__in=[ExportFile.Status.PENDING, ExportFile.Status.FAILED],
            attempts__lt=settings.FILE_MAX_RUNS,
        ).order_by("id")
        return [export_file async for export_file in files]

    @sync_to_async
    @transaction.atomic
    def save_file(self, export_file, rows, file_sha256, http_status):
        RawResource.objects.bulk_create(
            [
                RawResource(
                    resource_type=export_file.resource_type,
                    source_id=row.source_id,
                    version_id=row.version_id,
                    last_updated=row.last_updated,
                    payload=row.payload,
                    payload_sha256=row.payload_sha256,
                    file=export_file,
                    line_no=row.line_no,
                )
                for row in rows
            ],
            batch_size=1000,
            ignore_conflicts=True,
        )
        inserted = RawResource.objects.filter(file=export_file).count()
        export_file.status = ExportFile.Status.DONE
        export_file.http_status = http_status
        export_file.line_count = len(rows)
        export_file.inserted_count = inserted
        export_file.duplicate_count = len(rows) - inserted
        export_file.sha256 = file_sha256
        export_file.last_error = ""
        export_file.downloaded_at = timezone.now()
        export_file.save()
        return inserted

    async def mark_file_failed(self, export_file, error, http_status=None):
        export_file.status = ExportFile.Status.FAILED
        export_file.last_error = error
        export_file.http_status = http_status
        await export_file.asave(update_fields=["status", "last_error", "http_status", "attempts"])

    async def progress(self, job):
        lines = {
            row["resource_type"]: row["lines"]
            async for row in job.files.values("resource_type").annotate(lines=Sum("line_count"))
        }
        return {
            "job_id": job.job_id,
            "files_done": await job.files.filter(status=ExportFile.Status.DONE).acount(),
            "files_total": await job.files.acount(),
            "files_at_retry_limit": await job.files.exclude(status=ExportFile.Status.DONE)
            .filter(attempts__gte=settings.FILE_MAX_RUNS).acount(),
            "lines": lines,
            "expected_counts": job.expected_counts,
        }


class Extractor:
    def __init__(self, workers=None, store=None):
        self.workers = workers or settings.EXPORT_WORKERS
        self.store = store or ExportStore()

    async def fetch(self, export_file):
        raise NotImplementedError

    def has_expected_counts(self, job):
        return True

    async def download_pending_files(self, job):
        semaphore = asyncio.Semaphore(self.workers)

        async def download(export_file):
            async with semaphore:
                await self.process_file(job, export_file)

        files = await self.store.files_to_download(job)
        results = await asyncio.gather(*(download(f) for f in files), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def process_file(self, job, export_file):
        started = time.monotonic()
        export_file.attempts += 1
        context = {"job_id": job.job_id, "file_id": export_file.pk, "resource_type": export_file.resource_type}
        try:
            body, http_status = await self.fetch(export_file)
            rows = parse_ndjson(body, export_file.resource_type)
        except PermanentHttpError as error:
            await self.store.mark_file_failed(export_file, str(error), error.status)
            if error.status in EXPIRED_STATUSES:
                await self.store.mark_job(job, ExportJob.Status.EXPIRED, f"file {export_file.pk}: HTTP {error.status}")
                raise ExtractError("export files expired: run again to start a new export")
            log.error("file_failed", extra={**context, "http_status": error.status, "error": str(error)})
            return
        except RetriesExhausted as error:
            await self.store.mark_file_failed(export_file, str(error), error.status)
            log.error("file_failed", extra={**context, "http_status": error.status, "error": error.error})
            return
        except InvalidFile as error:
            await self.store.mark_file_failed(export_file, f"invalid file: {error}")
            log.error("file_failed", extra={**context, "error": f"invalid file: {error}"})
            return
        except CircuitOpen:
            await self.store.mark_file_failed(export_file, "circuit open")
            raise
        inserted = await self.store.save_file(export_file, rows, hashlib.sha256(body).hexdigest(), http_status)
        log.info("file_done", extra={
            **context, "lines": len(rows), "inserted": inserted, "duplicates": len(rows) - inserted,
            "duration_s": round(time.monotonic() - started, 2),
        })

    async def finish(self, job):
        summary = await self.store.progress(job)
        all_done = summary["files_done"] == summary["files_total"]
        if all_done and self.has_expected_counts(job):
            await self.store.complete_job(job)
            log.info("export_completed", extra=summary)
        elif summary["files_at_retry_limit"]:
            reason = f"{summary['files_at_retry_limit']} files reached the retry limit"
            await self.store.mark_job(job, ExportJob.Status.FAILED, reason)
            log.error("migration_halted", extra={**summary, "reason": "retry_limit_reached"})
            raise ExtractError(f"{reason}: human follow-up needed")
        else:
            log.warning("export_incomplete", extra=summary)
        return {**summary, "status": job.status}


class BulkExportExtractor(Extractor):
    def __init__(self, client=None, workers=None, store=None):
        super().__init__(workers, store)
        self.client = client or FhirClient()

    async def run(self):
        async with self.client as fhir:
            self.fhir = fhir
            try:
                job = await self.resume_or_kick_off()
                if job.manifest_json is None:
                    await self.poll_until_ready(job)
                if job.expected_counts is None:
                    await self.record_expected_counts(job)
                await self.download_pending_files(job)
            except CircuitOpen as error:
                log.error("migration_halted", extra={"reason": "circuit_open", "error": str(error)})
                raise ExtractError(f"{error}: the source looks unavailable, try again later")
        return await self.finish(job)

    def has_expected_counts(self, job):
        return job.expected_counts is not None

    async def fetch(self, export_file):
        response = await self.fhir.get(
            export_file.url, step=f"download:{export_file.pk}", headers={"Accept": "application/fhir+ndjson"}
        )
        return response.content, response.status_code

    async def resume_or_kick_off(self):
        job = await self.store.find_resumable_job()
        if job:
            log.info("export_resumed", extra={"job_id": job.job_id})
            return job
        return await self.kick_off()

    async def kick_off(self):
        request_url = f"{self.fhir.base_url}/$export?_type={','.join(settings.EXPORT_RESOURCE_TYPES)}"
        try:
            response = await self.fhir.get(
                request_url, step="kick_off", headers={"Accept": FHIR_JSON, "Prefer": "respond-async"}
            )
        except PermanentHttpError as error:
            hint = FAIL_FAST_HINTS.get(error.status, "unexpected response")
            log.error("export_failed_fast", extra={"step": "kick_off", "http_status": error.status, "hint": hint})
            raise ExtractError(f"kick-off failed with HTTP {error.status}: {hint}")
        poll_url = response.headers.get("Content-Location")
        if response.status_code != 202 or not poll_url:
            raise ExtractError(f"kick-off returned {response.status_code} without Content-Location")
        self.fhir.check_origin(poll_url)
        job = await self.store.create_job(poll_url, self.fhir.base_url, request_url)
        log.info("export_kicked_off", extra={"job_id": job.job_id})
        return job

    async def poll_until_ready(self, job):
        while True:
            response = await self.poll(job)
            if response.status_code != 202:
                break
            wait = min(retry_after_seconds(response) or settings.EXPORT_POLL_MAX_WAIT, settings.EXPORT_POLL_MAX_WAIT)
            log.info("export_polling", extra={
                "job_id": job.job_id, "progress": response.headers.get("X-Progress"), "wait_s": wait,
            })
            await self.fhir.sleep(wait)
        await self.accept_manifest(job, response.json())

    async def poll(self, job):
        try:
            return await self.fhir.get(job.poll_url, step="poll", headers={"Accept": "application/json"})
        except PermanentHttpError as error:
            if error.status in EXPIRED_STATUSES:
                await self.store.mark_job(job, ExportJob.Status.EXPIRED, f"poll returned HTTP {error.status}")
                raise ExtractError("export job expired: run again to start a new export")
            await self.store.mark_job(job, ExportJob.Status.FAILED, str(error))
            raise ExtractError(f"export job failed: {error}")

    async def accept_manifest(self, job, manifest):
        if manifest.get("error"):
            await self.store.mark_job(job, ExportJob.Status.FAILED, f"manifest reported {len(manifest['error'])} errors")
            raise ExtractError("export job finished with errors in the manifest")
        outputs = manifest.get("output") or []
        for item in outputs:
            self.fhir.check_origin(item["url"])
        await self.store.save_manifest(job, manifest)
        log.info("export_manifest_received", extra={
            "job_id": job.job_id,
            "files": dict(Counter(item["type"] for item in outputs)),
            "transaction_time": job.transaction_time,
        })

    async def record_expected_counts(self, job):
        try:
            counts = {
                resource_type: await self.count_at_cut_off(resource_type, job.transaction_time)
                for resource_type in settings.EXPORT_RESOURCE_TYPES
            }
        except CircuitOpen:
            raise
        except FhirError as error:
            log.warning("expected_counts_unavailable", extra={"job_id": job.job_id, "error": str(error)})
            return
        await self.store.save_expected_counts(job, counts)
        log.info("expected_counts", extra={"job_id": job.job_id, "counts": counts})

    async def count_at_cut_off(self, resource_type, transaction_time):
        response = await self.fhir.get(
            resource_type,
            step="expected_count",
            headers={"Accept": FHIR_JSON},
            params={"_lastUpdated": f"le{transaction_time}", "_summary": "count"},
        )
        return response.json().get("total")

