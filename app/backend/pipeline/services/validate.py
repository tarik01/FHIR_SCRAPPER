import logging
import random
from dataclasses import asdict, dataclass
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, Sum
from django.utils import timezone

from clinical.models import Observation, Patient, PatientDuplicateCandidate
from pipeline.models import ExportFile, ExportJob, Quarantine, RunReport
from pipeline.services.mapping import ObservationMapper, PatientMapper, Rejected
from pipeline.services.transform import OBSERVATION_FIELDS, PATIENT_FIELDS, RawStore, in_batches

log = logging.getLogger("pipeline.validate")

RESOURCE_MODELS = {"Patient": Patient, "Observation": Observation}
NUMERIC_TOLERANCE = Decimal("1e-9")


class ValidationError(Exception):
    pass


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    expected: object
    actual: object
    detail: str = ""


@dataclass(frozen=True)
class Mismatch:
    resource_type: str
    source_id: str
    field: str
    expected: str
    actual: str


class SampleComparator:
    def __init__(self, raw_store, source_system, rate, seed):
        self.raw_store = raw_store
        self.source_system = source_system
        self.rate = rate
        self.random = random.Random(seed)
        self.patient_mapper = PatientMapper(today=timezone.now().date())
        self.patient_ids = dict(Patient.objects.filter(source_system=source_system).values_list("source_id", "id"))
        self.observation_mapper = ObservationMapper(known_patient_ids=self.patient_ids, base_url=source_system)
        self.checked = {"Patient": 0, "Observation": 0}
        self.mismatches = []

    def run(self):
        self.compare("Patient", self.patient_mapper, PATIENT_FIELDS, self.patient_expectation)
        self.compare("Observation", self.observation_mapper, OBSERVATION_FIELDS, self.observation_expectation)
        return self.checked, self.mismatches

    def pick(self, raw_ids):
        size = min(len(raw_ids), max(1, round(len(raw_ids) * self.rate))) if raw_ids else 0
        return sorted(self.random.sample(raw_ids, size))

    def compare(self, resource_type, mapper, fields, expectation):
        sampled = list(self.raw_store.load(self.pick(self.raw_store.latest_ids(resource_type))))
        source_ids = [raw.source_id for raw, _ in sampled]
        model = RESOURCE_MODELS[resource_type]
        stored, quarantined = {}, set()
        for chunk in in_batches(source_ids, 1000):
            stored.update(
                (obj.source_id, obj)
                for obj in model.objects.filter(source_system=self.source_system, source_id__in=chunk)
            )
            quarantined.update(
                Quarantine.objects.filter(resource_type=resource_type, source_id__in=chunk)
                .values_list("source_id", flat=True)
            )
        for raw, resource in sampled:
            self.checked[resource_type] += 1
            result = mapper.map(resource)
            if isinstance(result, Rejected):
                if raw.source_id not in quarantined:
                    self.mismatch(resource_type, raw.source_id, "quarantine", result.reason, "not quarantined")
                continue
            row = stored.get(raw.source_id)
            if row is None:
                self.mismatch(resource_type, raw.source_id, "row", "loaded", "missing")
                continue
            expected = expectation(result, raw)
            for field in fields:
                actual = getattr(row, "patient_id" if field == "patient" else field)
                if not self.same(field, expected[field], actual):
                    self.mismatch(resource_type, raw.source_id, field, expected[field], actual)

    def same(self, field, expected, actual):
        if field == "value_num" and expected is not None and actual is not None:
            return abs(expected - actual) <= NUMERIC_TOLERANCE * max(1, abs(expected))
        return expected == actual

    def patient_expectation(self, record, raw):
        return asdict(record)

    def observation_expectation(self, record, raw):
        fields = asdict(record)
        fields["patient"] = self.patient_ids[fields.pop("patient_source_id")]
        return fields

    def mismatch(self, resource_type, source_id, field, expected, actual):
        self.mismatches.append(Mismatch(resource_type, source_id, field, str(expected), str(actual)))


class ValidationEngine:
    def __init__(self, sample_rate=0.01, seed=None, source_system=None):
        self.sample_rate = sample_rate
        self.seed = seed if seed is not None else random.randrange(1_000_000)
        self.source_system = source_system or settings.FHIR_BASE_URL
        self.raw_store = RawStore()

    def run(self):
        job = self.latest_completed_job()
        checks = [
            *self.check_export_counts(job),
            *self.check_every_resource_accounted_for(),
            self.check_no_duplicate_records(),
            self.check_no_observation_without_patient(),
            self.check_no_pending_or_failed_files(job),
        ]
        checked, mismatches = SampleComparator(self.raw_store, self.source_system, self.sample_rate, self.seed).run()
        checks.append(CheckResult("sample_mismatches", not mismatches, 0, len(mismatches),
                                  f"{sum(checked.values())} records re-mapped from raw and compared"))
        report = RunReport.objects.create(
            job=job,
            passed=all(check.passed for check in checks),
            checks=[asdict(check) for check in checks],
            totals=self.totals(),
            sample={"rate": self.sample_rate, "seed": self.seed, "checked": checked,
                    "mismatches": [asdict(m) for m in mismatches[:100]]},
        )
        log.info("validation_completed", extra={
            "report_id": report.pk, "passed": report.passed,
            "failed_checks": [check.name for check in checks if not check.passed],
        })
        return report

    def latest_completed_job(self):
        job = ExportJob.objects.filter(status=ExportJob.Status.COMPLETED).order_by("-completed_at").first()
        if job is None:
            raise ValidationError("no completed export job: run `migrate_fhir extract` first")
        return job

    def check_export_counts(self, job):
        lines = dict(job.files.values_list("resource_type").annotate(total=Sum("line_count")))
        results = []
        for resource_type in settings.EXPORT_RESOURCE_TYPES:
            actual = lines.get(resource_type, 0)
            expected = job.expected_counts.get(resource_type)
            results.append(CheckResult(
                f"export_lines({resource_type})", actual == expected, expected, actual,
                f"_summary=count at transactionTime {job.transaction_time}"
                if actual == expected else f"difference: check _history?_since={job.transaction_time}",
            ))
        return results

    def check_every_resource_accounted_for(self):
        results = []
        for resource_type, model in RESOURCE_MODELS.items():
            extracted = len(self.raw_store.latest_ids(resource_type))
            loaded = model.objects.filter(source_system=self.source_system).count()
            quarantined = Quarantine.objects.filter(resource_type=resource_type).count()
            results.append(CheckResult(
                f"loaded + quarantined == extracted ({resource_type})",
                loaded + quarantined == extracted, extracted, loaded + quarantined,
                f"{loaded} loaded + {quarantined} quarantined",
            ))
        return results

    def check_no_duplicate_records(self):
        duplicates = sum(
            model.objects.values("source_system", "source_id").annotate(n=Count("id")).filter(n__gt=1).count()
            for model in RESOURCE_MODELS.values()
        )
        return CheckResult("duplicate_records", duplicates == 0, 0, duplicates)

    def check_no_observation_without_patient(self):
        orphans = Observation.objects.exclude(patient_id__in=Patient.objects.values("id")).count()
        return CheckResult("observations_without_patient", orphans == 0, 0, orphans)

    def check_no_pending_or_failed_files(self, job):
        pending = job.files.exclude(status=ExportFile.Status.DONE).count()
        return CheckResult("files_pending_or_failed", pending == 0, 0, pending, f"job {job.job_id}")

    def totals(self):
        reasons = Quarantine.objects.values("resource_type", "reason").annotate(n=Count("id")).order_by("-n")
        candidates = PatientDuplicateCandidate.objects.values("match_reason").annotate(n=Count("id"))
        return {
            "loaded": {name: model.objects.count() for name, model in RESOURCE_MODELS.items()},
            "quarantined": {
                name: Quarantine.objects.filter(resource_type=name).count() for name in RESOURCE_MODELS
            },
            "quarantine_reasons": [
                {"resource_type": row["resource_type"], "reason": row["reason"], "count": row["n"]}
                for row in reasons
            ],
            "duplicate_candidates": {row["match_reason"]: row["n"] for row in candidates},
        }
