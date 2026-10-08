import json
import logging
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from clinical.models import Observation, Patient, PatientDuplicateCandidate, PatientIdentifier
from pipeline.models import ExportJob, Quarantine, RawResource
from pipeline.services.mapping import ObservationMapper, PatientMapper, Rejected, parse_fhir_datetime

log = logging.getLogger("pipeline.transform")

PATIENT_FIELDS = [
    "given_name", "family_name", "birth_date", "birth_date_precision", "gender", "deceased", "deceased_at",
    "deceased_precision",
]
OBSERVATION_FIELDS = [
    "patient", "status", "category", "code_system", "code", "code_display", "code_text", "value_type",
    "value_num", "value_unit", "value_text", "data_absent_reason", "effective_start", "effective_end",
    "effective_precision", "components_json",
]
TRACKING_FIELDS = ["source_version", "source_last_updated", "mapping_version", "migrated_at"]
OLDEST = datetime.min.replace(tzinfo=UTC)


class TransformError(Exception):
    pass


def parse_last_updated(value):
    try:
        return parse_fhir_datetime(value)[0] if value else None
    except ValueError:
        return None


def in_batches(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def source_fields(raw):
    return {
        "source_id": raw.source_id,
        "source_version": raw.version_id,
        "source_last_updated": parse_last_updated(raw.last_updated),
    }


class RawStore:
    def latest_ids(self, resource_type):
        newest = {}
        rows = RawResource.objects.filter(
            resource_type=resource_type, file__job__status=ExportJob.Status.COMPLETED
        ).values_list("id", "source_id", "last_updated")
        for raw_id, source_id, last_updated in rows.iterator(chunk_size=5000):
            key = (parse_last_updated(last_updated) or OLDEST, raw_id)
            if source_id not in newest or key > newest[source_id]:
                newest[source_id] = key
        return sorted(raw_id for _, raw_id in newest.values())

    def load(self, raw_ids):
        raws = RawResource.objects.in_bulk(raw_ids)
        for raw_id in raw_ids:
            raw = raws[raw_id]
            yield raw, json.loads(raw.payload, parse_float=Decimal)


class Upserter:
    def __init__(self, model, fields, stats, source_system, mapping_version):
        self.model = model
        self.fields = fields
        self.stats = stats
        self.source_system = source_system
        self.mapping_version = mapping_version

    def write(self, rows):
        stored = self.stored_versions([row["source_id"] for row in rows])
        now = timezone.now()
        to_write = [
            self.model(source_system=self.source_system, mapping_version=self.mapping_version, migrated_at=now, **row)
            for row in rows
            if self.should_write(row, stored.get(row["source_id"]))
        ]
        self.model.objects.bulk_create(
            to_write,
            update_conflicts=True,
            unique_fields=["source_system", "source_id"],
            update_fields=self.fields + TRACKING_FIELDS,
        )
        return [obj.source_id for obj in to_write]

    def stored_versions(self, source_ids):
        rows = self.model.objects.filter(source_system=self.source_system, source_id__in=source_ids).values(
            "source_id", "source_version", "source_last_updated", "mapping_version"
        )
        return {row["source_id"]: row for row in rows}

    def should_write(self, incoming, stored):
        if stored is None:
            self.stats["inserted"] += 1
            return True
        if stored["source_version"] == incoming["source_version"] and stored["mapping_version"] == self.mapping_version:
            self.stats["unchanged"] += 1
            return False
        if self.is_older(incoming["source_last_updated"], stored["source_last_updated"]):
            self.stats["stale_skipped"] += 1
            return False
        self.stats["updated"] += 1
        return True

    def is_older(self, incoming, stored):
        return bool(incoming and stored and incoming < stored)


class QuarantineRecorder:
    def __init__(self, model, resource_type, stats, source_system, mapping_version):
        self.model = model
        self.resource_type = resource_type
        self.stats = stats
        self.source_system = source_system
        self.mapping_version = mapping_version

    def record(self, rejected, mapped_source_ids):
        rejected_ids = [raw.source_id for raw, _ in rejected]
        self.model.objects.filter(source_system=self.source_system, source_id__in=rejected_ids).delete()
        touched = list(mapped_source_ids) + rejected_ids
        Quarantine.objects.filter(resource_type=self.resource_type, source_id__in=touched).delete()
        Quarantine.objects.bulk_create([
            Quarantine(raw_resource=raw, resource_type=self.resource_type, source_id=raw.source_id,
                       reason=reason, mapping_version=self.mapping_version)
            for raw, reason in rejected
        ])
        self.stats["quarantined"] += len(rejected)


class PatientTransformer:
    model = Patient
    resource_type = "Patient"

    def __init__(self, engine):
        self.engine = engine
        self.stats = Counter()
        self.mapper = PatientMapper(today=timezone.now().date())
        self.upserter = engine.upserter(Patient, PATIENT_FIELDS, self.stats)
        self.quarantine = engine.quarantine(self.model, self.resource_type, self.stats)

    def run(self):
        for raw_ids in in_batches(self.engine.raw_store.latest_ids(self.resource_type), self.engine.batch_size):
            self.transform_batch(raw_ids)
        return self.stats

    @transaction.atomic
    def transform_batch(self, raw_ids):
        rows, identifiers, rejected = [], {}, []
        for raw, resource in self.engine.raw_store.load(raw_ids):
            result = self.mapper.map(resource)
            if isinstance(result, Rejected):
                rejected.append((raw, result.reason))
                continue
            fields = asdict(result)
            fields.pop("identifiers")
            identifiers[raw.source_id] = result.identifiers
            rows.append({**source_fields(raw), **fields})
        written = self.upserter.write(rows)
        self.replace_identifiers(written, identifiers)
        self.quarantine.record(rejected, [row["source_id"] for row in rows])

    def replace_identifiers(self, source_ids, identifiers):
        patient_ids = dict(
            Patient.objects.filter(source_system=self.engine.source_system, source_id__in=source_ids)
            .values_list("source_id", "id")
        )
        PatientIdentifier.objects.filter(patient_id__in=patient_ids.values()).delete()
        PatientIdentifier.objects.bulk_create([
            PatientIdentifier(patient_id=patient_ids[source_id], system=item.system,
                              value=item.value, type_code=item.type_code)
            for source_id in source_ids
            for item in identifiers[source_id]
        ])


class ObservationTransformer:
    model = Observation
    resource_type = "Observation"

    def __init__(self, engine):
        self.engine = engine
        self.stats = Counter()
        self.patient_ids = dict(
            Patient.objects.filter(source_system=engine.source_system).values_list("source_id", "id")
        )
        self.mapper = ObservationMapper(known_patient_ids=self.patient_ids, base_url=engine.source_system)
        self.upserter = engine.upserter(Observation, OBSERVATION_FIELDS, self.stats)
        self.quarantine = engine.quarantine(self.model, self.resource_type, self.stats)

    def run(self):
        for raw_ids in in_batches(self.engine.raw_store.latest_ids(self.resource_type), self.engine.batch_size):
            self.transform_batch(raw_ids)
        return self.stats

    @transaction.atomic
    def transform_batch(self, raw_ids):
        rows, rejected = [], []
        for raw, resource in self.engine.raw_store.load(raw_ids):
            result = self.mapper.map(resource)
            if isinstance(result, Rejected):
                rejected.append((raw, result.reason))
                continue
            fields = asdict(result)
            fields["patient_id"] = self.patient_ids[fields.pop("patient_source_id")]
            rows.append({**source_fields(raw), **fields})
        self.upserter.write(rows)
        self.quarantine.record(rejected, [row["source_id"] for row in rows])


class DuplicateDetector:
    SHARED_IDENTIFIER = "shared identifier"
    SAME_DEMOGRAPHICS = "same name + birth date + gender"

    @transaction.atomic
    def run(self):
        PatientDuplicateCandidate.objects.filter(status=PatientDuplicateCandidate.Status.OPEN).delete()
        candidates = self.pairs(self.groups())
        PatientDuplicateCandidate.objects.bulk_create(candidates.values(), batch_size=1000, ignore_conflicts=True)
        return dict(Counter(reason for _, _, reason in candidates))

    def groups(self):
        groups = defaultdict(set)
        for patient_id, system, value in PatientIdentifier.objects.values_list("patient_id", "system", "value"):
            groups[(self.SHARED_IDENTIFIER, system, value)].add(patient_id)
        people = Patient.objects.exclude(family_name="").exclude(birth_date="").values_list(
            "id", "family_name", "given_name", "birth_date", "gender"
        )
        for patient_id, family, given, birth_date, gender in people:
            groups[(self.SAME_DEMOGRAPHICS, family.lower(), given.lower(), birth_date, gender)].add(patient_id)
        return groups

    def pairs(self, groups):
        candidates = {}
        for (reason, *_), members in groups.items():
            first, *others = sorted(members, key=str)
            for other in others:
                candidates.setdefault((first, other, reason), PatientDuplicateCandidate(
                    patient_a_id=first, patient_b_id=other, match_reason=reason
                ))
        return candidates


class TransformEngine:
    def __init__(self, source_system=None, batch_size=None, mapping_version=None):
        self.source_system = source_system or settings.FHIR_BASE_URL
        self.batch_size = batch_size or settings.TRANSFORM_BATCH_SIZE
        self.mapping_version = mapping_version or settings.MAPPING_VERSION
        self.raw_store = RawStore()

    def run(self, rebuild=False):
        self.ensure_export_completed()
        if rebuild:
            self.clear_internal_model()
        patients = PatientTransformer(self).run()
        observations = ObservationTransformer(self).run()
        duplicates = DuplicateDetector().run()
        summary = self.summary(patients, observations, duplicates)
        log.info("transform_completed", extra=summary)
        return summary

    def upserter(self, model, fields, stats):
        return Upserter(model, fields, stats, self.source_system, self.mapping_version)

    def quarantine(self, model, resource_type, stats):
        return QuarantineRecorder(model, resource_type, stats, self.source_system, self.mapping_version)

    def ensure_export_completed(self):
        if not ExportJob.objects.filter(status=ExportJob.Status.COMPLETED).exists():
            raise TransformError("no completed export job: run `migrate_fhir extract` first")

    @transaction.atomic
    def clear_internal_model(self):
        Observation.objects.all().delete()
        PatientDuplicateCandidate.objects.all().delete()
        PatientIdentifier.objects.all().delete()
        Patient.objects.all().delete()
        Quarantine.objects.all().delete()
        log.info("transform_rebuild", extra={"cleared": ["patients", "observations", "quarantine"]})

    def summary(self, patients, observations, duplicates):
        reasons = Quarantine.objects.values("resource_type", "reason").annotate(n=Count("id")).order_by("-n")
        return {
            "patients": dict(+patients),
            "observations": dict(+observations),
            "quarantine_reasons": {f"{row['resource_type']}: {row['reason']}": row["n"] for row in reasons},
            "duplicate_candidates": duplicates,
            "mapping_version": self.mapping_version,
        }
